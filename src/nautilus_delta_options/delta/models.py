from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Self, cast

type DeltaOptionContractType = Literal["call_options", "put_options"]


@dataclass(frozen=True, slots=True)
class DeltaOptionTicker:
    product_id: int
    symbol: str
    underlying: str
    contract_type: DeltaOptionContractType
    strike_price: Decimal
    expiry: date
    mark_price: Decimal
    spot_price: Decimal
    contract_value: Decimal
    tick_size: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    mark_iv: Decimal | None
    bid_iv: Decimal | None
    ask_iv: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    rho: Decimal | None
    vega: Decimal | None
    open_interest_contracts: Decimal
    volume: Decimal
    exchange_timestamp: int
    trading_status: str

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def spread_fraction(self) -> Decimal | None:
        spread = self.spread
        if spread is None or self.best_ask is None or self.best_ask <= 0:
            return None
        return spread / self.best_ask

    @property
    def has_tradeable_quote(self) -> bool:
        return (
            self.trading_status == "operational"
            and self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid > 0
            and self.best_ask >= self.best_bid
            and self.bid_size is not None
            and self.ask_size is not None
            and self.bid_size > 0
            and self.ask_size > 0
        )

    @classmethod
    def from_api(cls, raw: Mapping[str, object]) -> Self:
        quotes = _mapping(raw.get("quotes"), "quotes")
        greeks = _mapping(raw.get("greeks"), "greeks")

        contract_type_value = _string(raw.get("contract_type"), "contract_type")
        if contract_type_value not in ("call_options", "put_options"):
            raise ValueError(f"Unsupported contract_type: {contract_type_value}")
        contract_type = cast(DeltaOptionContractType, contract_type_value)

        symbol = _string(raw.get("symbol"), "symbol")

        return cls(
            product_id=_integer(raw.get("product_id"), "product_id"),
            symbol=symbol,
            underlying=_string(
                raw.get("underlying_asset_symbol"),
                "underlying_asset_symbol",
            ),
            contract_type=contract_type,
            strike_price=_decimal(raw.get("strike_price"), "strike_price"),
            expiry=_expiry_from_symbol(symbol),
            mark_price=_decimal(raw.get("mark_price"), "mark_price"),
            spot_price=_decimal(raw.get("spot_price"), "spot_price"),
            contract_value=_decimal(raw.get("contract_value"), "contract_value"),
            tick_size=_decimal(raw.get("tick_size"), "tick_size"),
            best_bid=_optional_decimal(quotes.get("best_bid"), "quotes.best_bid"),
            best_ask=_optional_decimal(quotes.get("best_ask"), "quotes.best_ask"),
            bid_size=_optional_decimal(quotes.get("bid_size"), "quotes.bid_size"),
            ask_size=_optional_decimal(quotes.get("ask_size"), "quotes.ask_size"),
            mark_iv=_optional_decimal(quotes.get("mark_iv"), "quotes.mark_iv"),
            bid_iv=_optional_decimal(quotes.get("bid_iv"), "quotes.bid_iv"),
            ask_iv=_optional_decimal(quotes.get("ask_iv"), "quotes.ask_iv"),
            delta=_optional_decimal(greeks.get("delta"), "greeks.delta"),
            gamma=_optional_decimal(greeks.get("gamma"), "greeks.gamma"),
            theta=_optional_decimal(greeks.get("theta"), "greeks.theta"),
            rho=_optional_decimal(greeks.get("rho"), "greeks.rho"),
            vega=_optional_decimal(greeks.get("vega"), "greeks.vega"),
            open_interest_contracts=_decimal(
                raw.get("oi_contracts"),
                "oi_contracts",
            ),
            volume=_decimal(raw.get("volume"), "volume"),
            exchange_timestamp=_integer(raw.get("timestamp"), "timestamp"),
            trading_status=_string(
                raw.get("product_trading_status"),
                "product_trading_status",
            ),
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


def _optional_decimal(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, field)


def _expiry_from_symbol(symbol: str) -> date:
    try:
        expiry_token = symbol.rsplit("-", maxsplit=1)[1]
        return datetime.strptime(expiry_token, "%d%m%y").date()
    except (IndexError, ValueError) as exc:
        raise ValueError(f"Invalid Delta option symbol: {symbol}") from exc
