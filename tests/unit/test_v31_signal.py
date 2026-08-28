from decimal import Decimal

from nautilus_delta_options.signals.binance import (
    BinanceCandleSnapshot,
    MarketCandle,
    parse_binance_klines,
)
from nautilus_delta_options.signals.v31 import (
    V31SignalDecision,
    evaluate_v31_call_signal,
    is_v31_call_entry,
)


def _rising_candle(index: int) -> MarketCandle:
    close = Decimal(100 + index)
    open_price = close - Decimal("0.5")

    return MarketCandle(
        open_time_ms=index * 300_000,
        close_time_ms=index * 300_000 + 299_999,
        open=open_price,
        high=close + Decimal("1"),
        low=open_price - Decimal("1"),
        close=close,
        volume=Decimal("10"),
    )


def test_filters_current_incomplete_binance_candle() -> None:
    payload = [
        [
            0,
            "100",
            "110",
            "90",
            "105",
            "10",
            999,
        ],
        [
            1_000,
            "105",
            "115",
            "100",
            "110",
            "11",
            1_999,
        ],
    ]

    candles = parse_binance_klines(
        payload,
        now_ms=1_500,
    )

    assert len(candles) == 1
    assert candles[0].close == Decimal("105")


def test_v31_entry_requires_every_original_condition() -> None:
    passing = {
        "rsi": 34.99,
        "ema20": 101.0,
        "ema50": 100.0,
        "atr_pct": 0.0349,
        "volume": 1.0,
    }

    assert is_v31_call_entry(**passing)
    assert not is_v31_call_entry(**(passing | {"rsi": 35.0}))
    assert not is_v31_call_entry(**(passing | {"ema20": 100.0}))
    assert not is_v31_call_entry(**(passing | {"atr_pct": 0.035}))
    assert not is_v31_call_entry(**(passing | {"volume": 0.0}))


def test_calculates_v31_indicators_from_200_candles() -> None:
    snapshot = BinanceCandleSnapshot(
        underlying="BTC",
        symbol="BTCUSDT",
        interval="5m",
        captured_ms=60_000_000,
        candles=tuple(_rising_candle(index) for index in range(200)),
    )

    signal = evaluate_v31_call_signal(snapshot)

    assert signal.rsi == 100.0
    assert signal.ema20_above_ema50 is True
    assert signal.atr_pct_below_035 is True
    assert signal.positive_volume is True
    assert signal.rsi_below_35 is False
    assert signal.decision is V31SignalDecision.NONE


def test_signal_key_is_unique_per_asset_and_candle() -> None:
    snapshot = BinanceCandleSnapshot(
        underlying="ETH",
        symbol="ETHUSDT",
        interval="5m",
        captured_ms=60_000_000,
        candles=tuple(_rising_candle(index) for index in range(200)),
    )

    signal = evaluate_v31_call_signal(snapshot)

    assert signal.signal_key == (f"ETH:{signal.candle_close_ms}:none")
