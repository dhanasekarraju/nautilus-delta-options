from decimal import Decimal

from nautilus_trader.model import (
    CryptoOption,
    OptionGreeks,
    Price,
    Quantity,
    QuoteTick,
)

from nautilus_delta_options.delta.models import DeltaOptionTicker


def map_delta_ticker_to_quote_tick(
    ticker: DeltaOptionTicker,
    instrument: CryptoOption,
    *,
    ts_init_ns: int,
) -> QuoteTick:
    _validate_symbol(ticker, instrument)

    if not ticker.has_tradeable_quote:
        raise ValueError("Ticker does not contain a tradeable quote")

    assert ticker.best_bid is not None
    assert ticker.best_ask is not None
    assert ticker.bid_size is not None
    assert ticker.ask_size is not None

    ts_event_ns = _delta_microseconds_to_ns(ticker.exchange_timestamp)
    _validate_init_timestamp(ts_event_ns, ts_init_ns)

    return QuoteTick(
        instrument_id=instrument.id,
        bid_price=Price.from_str(
            _fixed_precision_text(
                ticker.best_bid,
                instrument.price_precision,
            ),
        ),
        ask_price=Price.from_str(
            _fixed_precision_text(
                ticker.best_ask,
                instrument.price_precision,
            ),
        ),
        bid_size=Quantity.from_str(
            _fixed_precision_text(
                ticker.bid_size,
                instrument.size_precision,
            ),
        ),
        ask_size=Quantity.from_str(
            _fixed_precision_text(
                ticker.ask_size,
                instrument.size_precision,
            ),
        ),
        ts_event=ts_event_ns,
        ts_init=ts_init_ns,
    )


def map_delta_ticker_to_option_greeks(
    ticker: DeltaOptionTicker,
    instrument: CryptoOption,
    *,
    ts_init_ns: int,
) -> OptionGreeks:
    _validate_symbol(ticker, instrument)

    if ticker.delta is None:
        raise ValueError("Ticker is missing delta")
    if ticker.gamma is None:
        raise ValueError("Ticker is missing gamma")
    if ticker.vega is None:
        raise ValueError("Ticker is missing vega")
    if ticker.theta is None:
        raise ValueError("Ticker is missing theta")

    ts_event_ns = _delta_microseconds_to_ns(ticker.exchange_timestamp)
    _validate_init_timestamp(ts_event_ns, ts_init_ns)

    return OptionGreeks(
        instrument_id=instrument.id,
        delta=float(ticker.delta),
        gamma=float(ticker.gamma),
        vega=float(ticker.vega),
        theta=float(ticker.theta),
        rho=float(ticker.rho) if ticker.rho is not None else 0.0,
        mark_iv=_optional_float(ticker.mark_iv),
        bid_iv=_optional_float(ticker.bid_iv),
        ask_iv=_optional_float(ticker.ask_iv),
        underlying_price=float(ticker.spot_price),
        open_interest=float(ticker.open_interest_contracts),
        ts_event=ts_event_ns,
        ts_init=ts_init_ns,
    )


def _validate_symbol(
    ticker: DeltaOptionTicker,
    instrument: CryptoOption,
) -> None:
    if str(instrument.raw_symbol) != ticker.symbol:
        raise ValueError(
            f"Ticker symbol {ticker.symbol} does not match instrument {instrument.raw_symbol}",
        )


def _delta_microseconds_to_ns(timestamp: int) -> int:
    if timestamp <= 0:
        raise ValueError("Delta timestamp must be positive")
    return timestamp * 1_000


def _validate_init_timestamp(ts_event_ns: int, ts_init_ns: int) -> None:
    if ts_init_ns < ts_event_ns:
        raise ValueError("ts_init_ns cannot precede ts_event_ns")


def _fixed_precision_text(value: Decimal, precision: int) -> str:
    quantum = Decimal(1).scaleb(-precision)
    quantized = value.quantize(quantum)

    if quantized != value:
        raise ValueError(
            f"Value {value} exceeds supported precision {precision}",
        )

    return format(quantized, f".{precision}f")


def _optional_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None
