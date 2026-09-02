from decimal import Decimal

from nautilus_delta_options.signals.binance import (
    BinanceCandleSnapshot,
    MarketCandle,
)
from nautilus_delta_options.signals.v32 import (
    V32Signal,
    V32SignalDecision,
    evaluate_v32_signal,
    is_v32_call_entry,
    is_v32_put_entry,
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


def _signal(
    decision: V32SignalDecision,
) -> V32Signal:
    if decision is V32SignalDecision.CALL:
        rsi = 30.0
        ema20 = 101.0
        ema50 = 100.0
    elif decision is V32SignalDecision.PUT:
        rsi = 70.0
        ema20 = 99.0
        ema50 = 100.0
    else:
        rsi = 50.0
        ema20 = 100.0
        ema50 = 100.0

    return V32Signal(
        underlying="BTC",
        symbol="BTCUSDT",
        candle_open_ms=1_787_887_200_000,
        candle_close_ms=1_787_887_499_999,
        close_price=80000.0,
        rsi=rsi,
        ema20=ema20,
        ema50=ema50,
        atr=200.0,
        atr_pct=0.0025,
        volume=100.0,
        rsi_below_35=rsi < 35,
        rsi_above_65=rsi > 65,
        ema20_above_ema50=ema20 > ema50,
        ema20_below_ema50=ema20 < ema50,
        atr_pct_below_035=True,
        positive_volume=True,
        decision=decision,
    )


def test_v32_call_preserves_original_v31_thresholds() -> None:
    passing = {
        "rsi": 34.99,
        "ema20": 101.0,
        "ema50": 100.0,
        "atr_pct": 0.0349,
        "volume": 1.0,
    }

    assert is_v32_call_entry(**passing)
    assert not is_v32_call_entry(**(passing | {"rsi": 35.0}))
    assert not is_v32_call_entry(**(passing | {"ema20": 100.0}))
    assert not is_v32_call_entry(**(passing | {"atr_pct": 0.035}))
    assert not is_v32_call_entry(**(passing | {"volume": 0.0}))


def test_v32_put_requires_every_mirrored_condition() -> None:
    passing = {
        "rsi": 65.01,
        "ema20": 99.0,
        "ema50": 100.0,
        "atr_pct": 0.0349,
        "volume": 1.0,
    }

    assert is_v32_put_entry(**passing)
    assert not is_v32_put_entry(**(passing | {"rsi": 65.0}))
    assert not is_v32_put_entry(**(passing | {"ema20": 100.0}))
    assert not is_v32_put_entry(**(passing | {"atr_pct": 0.035}))
    assert not is_v32_put_entry(**(passing | {"volume": 0.0}))


def test_v32_signal_direction_metadata() -> None:
    call = _signal(V32SignalDecision.CALL)
    put = _signal(V32SignalDecision.PUT)
    wait = _signal(V32SignalDecision.NONE)

    assert call.active
    assert call.contract_type == "call_options"
    assert call.signal_key.endswith(":call")
    assert call.episode_key.endswith(":call")

    assert put.active
    assert put.contract_type == "put_options"
    assert put.signal_key.endswith(":put")
    assert put.episode_key.endswith(":put")

    assert not wait.active
    assert wait.contract_type is None
    assert wait.signal_key.endswith(":none")


def test_v32_episode_key_is_shared_across_assets() -> None:
    btc = _signal(V32SignalDecision.PUT)

    eth = V32Signal(
        underlying="ETH",
        symbol="ETHUSDT",
        candle_open_ms=btc.candle_open_ms,
        candle_close_ms=btc.candle_close_ms,
        close_price=2400.0,
        rsi=btc.rsi,
        ema20=btc.ema20,
        ema50=btc.ema50,
        atr=btc.atr,
        atr_pct=btc.atr_pct,
        volume=btc.volume,
        rsi_below_35=btc.rsi_below_35,
        rsi_above_65=btc.rsi_above_65,
        ema20_above_ema50=btc.ema20_above_ema50,
        ema20_below_ema50=btc.ema20_below_ema50,
        atr_pct_below_035=btc.atr_pct_below_035,
        positive_volume=btc.positive_volume,
        decision=btc.decision,
    )

    assert btc.signal_key != eth.signal_key
    assert btc.episode_key == eth.episode_key


def test_v32_evaluator_waits_when_direction_is_not_aligned() -> None:
    snapshot = BinanceCandleSnapshot(
        underlying="BTC",
        symbol="BTCUSDT",
        interval="5m",
        captured_ms=60_000_000,
        candles=tuple(_rising_candle(index) for index in range(200)),
    )

    signal = evaluate_v32_signal(snapshot)

    assert signal.rsi == 100.0
    assert signal.ema20_above_ema50
    assert not signal.rsi_below_35
    assert signal.decision is V32SignalDecision.NONE
