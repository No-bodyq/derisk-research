import pandas as pd
import asyncio
from decimal import Decimal
from copy import deepcopy

from handlers.state import State
from db.crud import DBConnector
from db.models import LiquidableDebt

from handlers.liquidable_debt.bases import Collector
from handlers.liquidable_debt.collectors import GoogleCloudDataCollector
from handlers.liquidable_debt.values import (GS_BUCKET_URL, GS_BUCKET_NAME, LendingProtocolNames,
                                             LOCAL_STORAGE_PATH, COLLATERAL_FIELD_NAME, PROTOCOL_FIELD_NAME,
                                             DEBT_FIELD_NAME, USER_FIELD_NAME, RISK_ADJUSTED_COLLATERAL_USD_FIELD_NAME,
                                             HEALTH_FACTOR_FIELD_NAME, DEBT_USD_FIELD_NAME, FIELDS_TO_VALIDATE,
                                             ALL_NEEDED_FIELDS, LIQUIDABLE_DEBT_FIELD_NAME, PRICE_FIELD_NAME,
                                             MYSWAP_VALUE, JEDISWAP_VALUE, POOL_SPLIT_VALUE, ROW_ID_FIELD_NAME)
from handlers.loan_states.zklend.events import ZkLendState
from handlers.loan_states.hashtack_v0.events import HashstackV0State
from handlers.loan_states.hashtack_v1.events import HashstackV1State
from handlers.helpers import TokenValues
from handlers.settings import TOKEN_PAIRS
from handlers.liquidable_debt.utils import Prices


class GCloudLiquidableDebtDataHandler:
    """
    A handler that collects data from Google Cloud Storage bucket,
    parses it and stores it in the database.

    :cvar AVAILABLE_PROTOCOLS: A list of all available protocols.
    :method: `update_data` -> Updates the data stored in the database.:
    """
    AVAILABLE_PROTOCOLS = [item.value for item in LendingProtocolNames]
    CONNECTOR = DBConnector()

    def __init__(
            self,
            loan_state_class: State,
            connection_url: str = GS_BUCKET_URL,
            bucket_name: str = GS_BUCKET_NAME,
            collector: Collector = GoogleCloudDataCollector
    ):
        self.collector = collector
        self.connection_url = connection_url
        self.bucket_name = bucket_name
        self.state_class = loan_state_class
        self.token_pairs = dict()

    def prepare_data(self, protocol_name: str, path: str = LOCAL_STORAGE_PATH) -> dict:
        uploaded_file_path = self.collector.collect_data(
            protocol_name=protocol_name,
            available_protocols=self.AVAILABLE_PROTOCOLS,
            bucket_name=self.bucket_name,
            path=path,
            url=self.connection_url
        )
        parsed_data = self._parse_file(uploaded_file_path)
        max_token_prices = self._get_all_max_token_values(parsed_data)
        max_token_prices = self._sort_by_token_pair_correspondence(parsed_data, max_token_prices)

        return self._calculate_liquidable_debt(parsed_data, max_token_prices)

    @staticmethod
    def _get_all_max_token_values(data: dict = None) -> dict:
        """
        Returns a dict of all token max values.
        :param data: A dict of data.
        :return: A dict of all token max values.
        """
        max_token_prices = dict()

        for index, row in data.items():
            for token, price in row[DEBT_FIELD_NAME].items():
                if not max_token_prices.get(token):
                    max_token_prices.update({token: {
                        ROW_ID_FIELD_NAME: index,
                        PRICE_FIELD_NAME: Decimal(price)
                    }})

                    continue

                if price > max_token_prices[token][PRICE_FIELD_NAME]:
                    max_token_prices[token][ROW_ID_FIELD_NAME] = index
                    max_token_prices[token][PRICE_FIELD_NAME] = price

                    continue

        return max_token_prices

    def _sort_by_token_pair_correspondence(
            self,
            parsed_data: dict[int, dict[str, str | Decimal | dict[str, Decimal]]] = None,
            max_token_prices: dict[str, dict[str, Decimal | int]] = None
    ) -> dict:
        """
        Sorts a given data by token pair correspondence.
        Args: parsed_data( dict[
            int - Row number,
            dict[
                str - Field name,
                str - Token value
                | Decimal - Field value (the price as example)
                | dict[
                    str - Token name,
                    Decimal - Token value
                ]
            ]
        ])
        :Args: max_token_prices( dict[
            str - Token name,
            dict[
                str - Field name,
                Decimal - Token price
                | int - row id
                ]
            ])
        :return: A dictionary of a sorted data.
        """
        result = deepcopy(max_token_prices)
        for debt_token, token_info in max_token_prices.items():
            for pair in TOKEN_PAIRS:
                if debt_token == pair[1]:
                    collateral_token_index = 0
                    collateral_keys = parsed_data[token_info[ROW_ID_FIELD_NAME]][
                        COLLATERAL_FIELD_NAME
                    ].keys()

                    if pair[collateral_token_index] not in collateral_keys:
                        if result.get(debt_token) and debt_token not in self.token_pairs.keys():
                            del result[debt_token]
                            break

                        continue

                    self.token_pairs.update({debt_token: pair[collateral_token_index]})

        return result

    def _calculate_liquidable_debt(
            self, data: dict[int, dict[str, str | Decimal | dict[str, Decimal]]] = None,
            max_token_prices: dict[str, dict[str, Decimal | int]] = None
    ) -> dict:
        """
        Calculates liquidable debt based on data provided and updates an existing data.
        Data to calculate liquidable debt for:
        Args: data( dict[
            int - Row number,
            dict[
                str - Field name,
                str - Token value
                | Decimal - Field value (the price as example)
                | dict[
                    str - Token name,
                    Decimal - Token value
                ]
            ]
        ])
        :Args: max_token_prices( dict[
            str - Token name,
            dict[
                str - Field name,
                Decimal - Token price
                | int - row id
                ]
            ])
        :return: A dictionary of the ready liquidable debt data.
        """
        result_data = dict()
        prices = Prices()
        asyncio.run(prices.get_lp_token_prices())

        for debt_token, token_info in max_token_prices.items():
            # even if it isn't per-user data, we need to provide a user ID
            # so like that we're able to provide debt and collateral values
            user_wallet_id = data[token_info[ROW_ID_FIELD_NAME]][USER_FIELD_NAME]
            if not self.token_pairs.get(debt_token):
                continue
            collateral_token_symbol = self.token_pairs[debt_token]
            state = self.state_class(verbose_user=user_wallet_id)

            if not isinstance(state, HashstackV0State) \
               and not isinstance(state, HashstackV1State):
                state.loan_entities[user_wallet_id].debt.values = {
                    debt_token: token_info[PRICE_FIELD_NAME]
                }
                state.loan_entities[user_wallet_id].collateral.values = {
                    collateral_token_symbol: data[token_info[ROW_ID_FIELD_NAME]][
                        COLLATERAL_FIELD_NAME
                    ][collateral_token_symbol]
                }

            if isinstance(state, ZkLendState):
                result = state.compute_liquidable_debt_at_price(
                    prices=TokenValues(init_value=prices.prices.values.get(debt_token)),
                    collateral_token=collateral_token_symbol,
                    collateral_token_price=data[token_info[ROW_ID_FIELD_NAME]][
                        COLLATERAL_FIELD_NAME
                    ][collateral_token_symbol],
                    debt_token=debt_token,
                    risk_adjusted_collateral_usd=data[token_info[
                        ROW_ID_FIELD_NAME
                    ]][RISK_ADJUSTED_COLLATERAL_USD_FIELD_NAME],
                    debt_usd=data[token_info[ROW_ID_FIELD_NAME]][DEBT_USD_FIELD_NAME],
                    health_factor=data[token_info[ROW_ID_FIELD_NAME]][HEALTH_FACTOR_FIELD_NAME],
                )

            else:
                result = state.compute_liquidable_debt_at_price(
                    prices=TokenValues(init_value=prices.prices.values.get(debt_token)),
                    collateral_token=collateral_token_symbol,
                    collateral_token_price=data[token_info[ROW_ID_FIELD_NAME]][
                        COLLATERAL_FIELD_NAME
                    ][collateral_token_symbol],
                    debt_token=debt_token,
                    debt_usd=data[token_info[ROW_ID_FIELD_NAME]][DEBT_USD_FIELD_NAME],
                    health_factor=data[token_info[ROW_ID_FIELD_NAME]][HEALTH_FACTOR_FIELD_NAME],
                )

            if result > Decimal("0"):
                result_data.update({
                    debt_token: {
                        LIQUIDABLE_DEBT_FIELD_NAME: result,
                        PRICE_FIELD_NAME: data[token_info[ROW_ID_FIELD_NAME]][COLLATERAL_FIELD_NAME][
                            collateral_token_symbol],
                        COLLATERAL_FIELD_NAME: collateral_token_symbol,
                        PROTOCOL_FIELD_NAME: data[token_info[ROW_ID_FIELD_NAME]][PROTOCOL_FIELD_NAME]
                    }
                })

        return result_data

    @classmethod
    def _parse_file(cls, path: str = None) -> dict:
        """
        Parse a parquet file into a dictionary.
        :param path: The path to the parquet file.
        :return: A dictionary of the parsed data.
        """
        data = pd.read_parquet(path=path).to_dict()
        arranged_data = cls._arrange_data_by_row(data)

        for row_number in arranged_data:
            arranged_data[row_number][DEBT_FIELD_NAME] = cls._transform_str_into_dict(
                arranged_data[row_number][DEBT_FIELD_NAME]
            )
            arranged_data[row_number][COLLATERAL_FIELD_NAME] = cls._transform_str_into_dict(
                arranged_data[row_number][COLLATERAL_FIELD_NAME]
            )

        return arranged_data

    @classmethod
    def _arrange_data_by_row(cls, data: dict = None) -> dict:
        """
        Arranges the dictionary data by rows.
        :param data: The dictionary to arrange.
        :return: A dictionary of the arranged data.
        """
        result = dict()

        for row_number in data[DEBT_FIELD_NAME]:
            arranged_row = {
                USER_FIELD_NAME: data[USER_FIELD_NAME][row_number],
                PROTOCOL_FIELD_NAME: data[PROTOCOL_FIELD_NAME][row_number],
                RISK_ADJUSTED_COLLATERAL_USD_FIELD_NAME: data[RISK_ADJUSTED_COLLATERAL_USD_FIELD_NAME][row_number],
                DEBT_USD_FIELD_NAME: data[DEBT_USD_FIELD_NAME][row_number],
                HEALTH_FACTOR_FIELD_NAME: data[HEALTH_FACTOR_FIELD_NAME][row_number],
                COLLATERAL_FIELD_NAME: data[COLLATERAL_FIELD_NAME][row_number],
                DEBT_FIELD_NAME: data[DEBT_FIELD_NAME][row_number],
            }
            if cls._is_valid(arranged_row):
                result.update({row_number: arranged_row})

        return result

    @staticmethod
    def _is_valid(data: dict = None) -> bool:
        """
        Checks if the dictionary data is valid.
        :param data: The dictionary to check.
        :return: True if the dictionary data is valid, False otherwise.
        """
        for field in FIELDS_TO_VALIDATE:
            if not data[field]:
                return False

            if not isinstance(data[field], str):
                if data[field] <= Decimal("0") \
                        or data[field] == Decimal("inf"):
                    return False

        return True

    @classmethod
    def _transform_str_into_dict(cls, tokens: str) -> dict:
        """
        Transforms a string into a dictionary.
        :param tokens: The string to transform.
        :return: A dictionary of the transformed data.
        """
        result = dict()
        separeted_tokens = tokens.split(', ')

        for token in separeted_tokens:
            if not token:
                continue

            if MYSWAP_VALUE in token:
                current_token, value = cls.split_collateral(
                    token, f"{MYSWAP_VALUE}: "
                ).split(POOL_SPLIT_VALUE)
                result.update({current_token: Decimal(value)})

                continue

            if JEDISWAP_VALUE in token:
                current_token, value = cls.split_collateral(
                    token, f"{JEDISWAP_VALUE}: "
                ).split(POOL_SPLIT_VALUE)
                result.update({current_token: Decimal(value)})

                continue

            token, value = token.split(': ')
            result.update({token: Decimal(value)})

        return result

    @staticmethod
    def split_collateral(collateral_string: str, platform_name: str) -> list:
        """
        Removes a platform name from the collateral string.
        :param collateral_string: The collateral string to split.
        :param platform_name: The platform name.
        :return: A collateral string without the platform name.
        """
        result = collateral_string.split(platform_name)
        result.remove("")
        return next(iter(result))

    @classmethod
    def _write_to_db(cls, data: dict = None) -> None:
        """
        Writes the data into the database.
        :param data: A dictionary of the parsed data.
        :return: None
        """
        cls.CONNECTOR.write_to_db(LiquidableDebt(**data))


class DBLiquidableDebtDataHandler:
    # TODO write logic when it will be needed
    pass
