from datetime import UTC, datetime
from decimal import Decimal

from nautilus_trader.model import (
    CryptoOption,
    Currency,
    InstrumentId,
    OptionKind,
    Price,
    Quantity,
    Symbol,
    Venue,
)

from nautilus_delta_options.delta.product import DeltaOptionProduct

DELTA_VENUE = Venue("DELTA")


def map_delta_product_to_crypto_option(
    product: DeltaOptionProduct,
    *,
    ts_event_ns: int,
    ts_init_ns: int,
) -> CryptoOption:
    if product.is_quanto:
        raise ValueError("Quanto options are not supported")
    if product.notional_type != "vanilla":
        raise ValueError(f"Unsupported notional_type: {product.notional_type}")
    if product.tick_size <= 0:
        raise ValueError("tick_size must be positive")
    if product.contract_value <= 0:
        raise ValueError("contract_value must be positive")
    if product.launch_time >= product.settlement_time:
        raise ValueError("launch_time must be before settlement_time")
    if ts_event_ns < 0 or ts_init_ns < 0:
        raise ValueError("Nautilus timestamps cannot be negative")

    option_kind = OptionKind.CALL if product.contract_type == "call_options" else OptionKind.PUT

    return CryptoOption(
        instrument_id=InstrumentId(
            Symbol(product.symbol),
            DELTA_VENUE,
        ),
        raw_symbol=Symbol(product.symbol),
        underlying=Currency.from_str(product.underlying),
        quote_currency=Currency.from_str(product.quote_currency),
        settlement_currency=Currency.from_str(product.settlement_currency),
        is_inverse=False,
        option_kind=option_kind,
        strike_price=Price.from_str(_decimal_text(product.strike_price)),
        activation_ns=_datetime_to_unix_ns(product.launch_time),
        expiration_ns=_datetime_to_unix_ns(product.settlement_time),
        price_precision=_decimal_precision(product.tick_size),
        size_precision=0,
        price_increment=Price.from_str(_decimal_text(product.tick_size)),
        size_increment=Quantity.from_str("1"),
        ts_event=ts_event_ns,
        ts_init=ts_init_ns,
        multiplier=Quantity.from_str(_decimal_text(product.contract_value)),
        lot_size=Quantity.from_str("1"),
        max_quantity=Quantity.from_str(
            _decimal_text(product.position_size_limit),
        ),
        min_quantity=Quantity.from_str("1"),
        maker_fee=product.maker_fee,
        taker_fee=product.taker_fee,
        info={
            "delta_product_id": product.product_id,
            "premium_cap_rate": _decimal_text(product.premium_cap_rate),
            "state": product.state,
            "trading_status": product.trading_status,
        },
    )


def _datetime_to_unix_ns(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("datetime must include timezone information")

    utc_value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = utc_value - epoch

    total_microseconds = (
        elapsed.days * 86_400 + elapsed.seconds
    ) * 1_000_000 + elapsed.microseconds
    return total_microseconds * 1_000


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _decimal_precision(value: Decimal) -> int:
    normalized = value.normalize()
    text = format(normalized, "f")

    if "." not in text:
        return 0

    return len(text.split(".", maxsplit=1)[1])
