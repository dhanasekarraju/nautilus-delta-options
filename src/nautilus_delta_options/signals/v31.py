from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import talib

from nautilus_delta_options.signals.binance import (
    BinanceCandleSnapshot,
)


class V31SignalDecision(StrEnum):
    CALL = "call"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class V31CallSignal:
    underlying: str
    symbol: str
    candle_open_ms: int
    candle_close_ms: int
    close_price: float
    rsi: float
    ema20: float
    ema50: float
    atr: float
    atr_pct: float
    volume: float
    rsi_below_35: bool
    ema20_above_ema50: bool
    atr_pct_below_035: bool
    positive_volume: bool
    decision: V31SignalDecision

    @property
    def active(self) -> bool:
        return self.decision is V31SignalDecision.CALL

    @property
    def signal_key(self) -> str:
        return f"{self.underlying}:{self.candle_close_ms}:{self.decision.value}"


def evaluate_v31_call_signal(
    snapshot: BinanceCandleSnapshot,
) -> V31CallSignal:
    if len(snapshot.candles) < 200:
        raise ValueError("V3.1 requires at least 200 completed candles")

    candles = snapshot.candles
    close_prices = np.asarray(
        [float(candle.close) for candle in candles],
        dtype=np.float64,
    )
    high_prices = np.asarray(
        [float(candle.high) for candle in candles],
        dtype=np.float64,
    )
    low_prices = np.asarray(
        [float(candle.low) for candle in candles],
        dtype=np.float64,
    )

    rsi_values = talib.RSI(
        close_prices,
        timeperiod=14,
    )
    ema20_values = talib.EMA(
        close_prices,
        timeperiod=20,
    )
    ema50_values = talib.EMA(
        close_prices,
        timeperiod=50,
    )
    atr_values = talib.ATR(
        high_prices,
        low_prices,
        close_prices,
        timeperiod=14,
    )

    latest = candles[-1]
    close_price = float(latest.close)
    rsi = float(rsi_values[-1])
    ema20 = float(ema20_values[-1])
    ema50 = float(ema50_values[-1])
    atr = float(atr_values[-1])
    volume = float(latest.volume)
    atr_pct = atr / close_price

    indicator_values = (
        close_price,
        rsi,
        ema20,
        ema50,
        atr,
        atr_pct,
        volume,
    )

    if not all(math.isfinite(value) for value in indicator_values):
        raise ValueError("V3.1 indicators must be finite")

    rsi_below_35 = rsi < 35
    ema20_above_ema50 = ema20 > ema50
    atr_pct_below_035 = atr_pct < 0.035
    positive_volume = volume > 0
    active = is_v31_call_entry(
        rsi=rsi,
        ema20=ema20,
        ema50=ema50,
        atr_pct=atr_pct,
        volume=volume,
    )

    return V31CallSignal(
        underlying=snapshot.underlying,
        symbol=snapshot.symbol,
        candle_open_ms=latest.open_time_ms,
        candle_close_ms=latest.close_time_ms,
        close_price=close_price,
        rsi=rsi,
        ema20=ema20,
        ema50=ema50,
        atr=atr,
        atr_pct=atr_pct,
        volume=volume,
        rsi_below_35=rsi_below_35,
        ema20_above_ema50=ema20_above_ema50,
        atr_pct_below_035=atr_pct_below_035,
        positive_volume=positive_volume,
        decision=(V31SignalDecision.CALL if active else V31SignalDecision.NONE),
    )


def is_v31_call_entry(
    *,
    rsi: float,
    ema20: float,
    ema50: float,
    atr_pct: float,
    volume: float,
) -> bool:
    return rsi < 35 and ema20 > ema50 and atr_pct < 0.035 and volume > 0
