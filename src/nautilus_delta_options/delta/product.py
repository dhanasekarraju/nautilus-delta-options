from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Self, cast

from nautilus_delta_options.delta.models import DeltaOptionContractType


@dataclass(frozen=True, slots=True)
class DeltaOptionProduct:
    product_id: int
    symbol: str
    contract_type: DeltaOptionContractType
    underlying: str
    underlying_precision: int
    quote_currency: str
    quote_precision: int
    settlement_currency: str
    settlement_precision: int
    contract_unit_currency: str
    strike_price: Decimal
    contract_value: Decimal
    tick_size: Decimal
    launch_time: datetime
    settlement_time: datetime
    maker_fee: Decimal
    taker_fee: Decimal
    premium_cap_rate: Decimal
    position_size_limit: Decimal
    is_quanto: bool
    notional_type: str
    trading_status: str
    state: str

    @classmethod
    def from_api(cls, raw: Mapping[str, object]) -> Self:
        underlying = _mapping(raw.get("underlying_asset"), "underlying_asset")
        quoting = _mapping(raw.get("quoting_asset"), "quoting_asset")
        settling = _mapping(raw.get("settling_asset"), "settling_asset")
        product_specs = _mapping(raw.get("product_specs"), "product_specs")

        contract_type_value = _string(raw.get("contract_type"), "contract_type")
        if contract_type_value not in ("call_options", "put_options"):
            raise ValueError(f"Unsupported contract_type: {contract_type_value}")
        contract_type = cast(DeltaOptionContractType, contract_type_value)

        underlying_symbol = _string(underlying.get("symbol"), "underlying_asset.symbol")
        contract_unit_currency = _string(
            raw.get("contract_unit_currency"),
            "contract_unit_currency",
        )

        if contract_unit_currency != underlying_symbol:
            raise ValueError(
                "contract_unit_currency must match underlying asset",
            )

        return cls(
            product_id=_integer(raw.get("id"), "id"),
            symbol=_string(raw.get("symbol"), "symbol"),
            contract_type=contract_type,
            underlying=underlying_symbol,
            underlying_precision=_integer(
                underlying.get("precision"),
                "underlying_asset.precision",
            ),
            quote_currency=_string(
                quoting.get("symbol"),
                "quoting_asset.symbol",
            ),
            quote_precision=_integer(
                quoting.get("precision"),
                "quoting_asset.precision",
            ),
            settlement_currency=_string(
                settling.get("symbol"),
                "settling_asset.symbol",
            ),
            settlement_precision=_integer(
                settling.get("precision"),
                "settling_asset.precision",
            ),
            contract_unit_currency=contract_unit_currency,
            strike_price=_decimal(raw.get("strike_price"), "strike_price"),
            contract_value=_decimal(raw.get("contract_value"), "contract_value"),
            tick_size=_decimal(raw.get("tick_size"), "tick_size"),
            launch_time=_datetime(raw.get("launch_time"), "launch_time"),
            settlement_time=_datetime(
                raw.get("settlement_time"),
                "settlement_time",
            ),
            maker_fee=_decimal(
                raw.get("maker_commission_rate"),
                "maker_commission_rate",
            ),
            taker_fee=_decimal(
                raw.get("taker_commission_rate"),
                "taker_commission_rate",
            ),
            premium_cap_rate=_decimal(
                product_specs.get("premium_commission_rate"),
                "product_specs.premium_commission_rate",
            ),
            position_size_limit=_decimal(
                raw.get("position_size_limit"),
                "position_size_limit",
            ),
            is_quanto=_boolean(raw.get("is_quanto"), "is_quanto"),
            notional_type=_string(raw.get("notional_type"), "notional_type"),
            trading_status=_string(raw.get("trading_status"), "trading_status"),
            state=_string(raw.get("state"), "state"),
        )


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field} must be an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{field} must be numeric")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be numeric") from exc


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


def _datetime(value: object, field: str) -> datetime:
    text = _string(value, field)

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 datetime") from exc

    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone information")

    return parsed
