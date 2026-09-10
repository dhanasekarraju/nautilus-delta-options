from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from statistics import median

import numpy as np
import talib

from nautilus_delta_options.delta.history import DeltaCandleSnapshot
from nautilus_delta_options.delta.models import DeltaOptionContractType, DeltaOptionTicker
from nautilus_delta_options.delta.public_client import DeltaOptionChainSnapshot


class V34Decision(StrEnum):
    CALL = "call"
    PUT = "put"
    WAIT = "wait"


@dataclass(frozen=True, slots=True)
class V34Config:
    min_dte: int = 1
    max_dte: int = 3
    min_abs_delta: float = 0.35
    max_abs_delta: float = 0.65
    max_spread_fraction: float = 0.025
    min_direction_score: float = 50.0
    min_total_score: float = 72.0
    min_score_edge: float = 14.0
    min_flow_confirmations: int = 2
    min_common_contracts_per_side: int = 2
    min_adx: float = 18.0
    max_extension_atr: float = 1.5
    min_previous_age_seconds: float = 180.0
    max_previous_age_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.min_dte < 1:
            raise ValueError("V3.4 blocks 0DTE")
        if self.max_dte < self.min_dte:
            raise ValueError("max_dte cannot be below min_dte")
        if not 0 < self.min_abs_delta < self.max_abs_delta < 1:
            raise ValueError("invalid absolute-delta range")
        if self.min_flow_confirmations < 1:
            raise ValueError("min_flow_confirmations must be positive")
        if self.min_common_contracts_per_side < 1:
            raise ValueError("min_common_contracts_per_side must be positive")
        if self.min_adx < 0:
            raise ValueError("min_adx cannot be negative")
        if self.max_extension_atr <= 0:
            raise ValueError("max_extension_atr must be positive")


@dataclass(frozen=True, slots=True)
class V34UnderlyingState:
    close: float
    rsi: float
    ema20: float
    ema50: float
    adx: float
    atr_pct: float
    ema20_slope_atr: float
    return_5m: float
    return_15m: float
    return_30m: float
    extension_atr: float
    call_score: float
    put_score: float


@dataclass(frozen=True, slots=True)
class V34ContractState:
    symbol: str
    contract_type: DeltaOptionContractType
    iv: float
    mark_price: float
    open_interest: float
    volume: float
    bid_size: float
    ask_size: float


@dataclass(frozen=True, slots=True)
class V34ChainState:
    underlying: str
    captured_ns: int
    contracts: tuple[V34ContractState, ...]


@dataclass(frozen=True, slots=True)
class V34FlowState:
    call_score: float
    put_score: float
    call_confirmations: int
    put_confirmations: int
    iv_edge: float
    oi_edge: float
    volume_edge: float
    depth_edge: float
    premium_edge: float


@dataclass(frozen=True, slots=True)
class V34ShadowSignal:
    underlying: str
    candle_close_ms: int
    decision: V34Decision
    call_score: float
    put_score: float
    confidence: float
    underlying_state: V34UnderlyingState
    flow_state: V34FlowState | None
    chain_state: V34ChainState
    reasons: tuple[str, ...]


def evaluate_v34_underlying(snapshot: DeltaCandleSnapshot) -> V34UnderlyingState:
    if len(snapshot.candles) < 200:
        raise ValueError("V3.4 requires at least 200 completed Delta candles")

    closes = np.asarray([float(x.close) for x in snapshot.candles], dtype=np.float64)
    highs = np.asarray([float(x.high) for x in snapshot.candles], dtype=np.float64)
    lows = np.asarray([float(x.low) for x in snapshot.candles], dtype=np.float64)

    ema20_values = talib.EMA(closes, timeperiod=20)
    ema50_values = talib.EMA(closes, timeperiod=50)
    rsi_values = talib.RSI(closes, timeperiod=14)
    atr_values = talib.ATR(highs, lows, closes, timeperiod=14)
    adx_values = talib.ADX(highs, lows, closes, timeperiod=14)

    close = float(closes[-1])
    ema20 = float(ema20_values[-1])
    ema50 = float(ema50_values[-1])
    rsi = float(rsi_values[-1])
    atr = float(atr_values[-1])
    adx = float(adx_values[-1])
    ema20_3 = float(ema20_values[-4])

    values = (close, ema20, ema50, rsi, atr, adx, ema20_3)
    if not all(math.isfinite(x) for x in values) or close <= 0 or atr <= 0:
        raise ValueError("V3.4 indicators must be finite and positive")

    r5 = close / float(closes[-2]) - 1.0
    r15 = close / float(closes[-4]) - 1.0
    r30 = close / float(closes[-7]) - 1.0
    slope = (ema20 - ema20_3) / atr
    extension = abs(close - ema20) / atr

    call_score = _direction_score(
        side=1,
        close=close,
        ema20=ema20,
        ema50=ema50,
        slope=slope,
        rsi=rsi,
        adx=adx,
        r5=r5,
        r15=r15,
        r30=r30,
    )
    put_score = _direction_score(
        side=-1,
        close=close,
        ema20=ema20,
        ema50=ema50,
        slope=slope,
        rsi=rsi,
        adx=adx,
        r5=r5,
        r15=r15,
        r30=r30,
    )

    return V34UnderlyingState(
        close=close,
        rsi=rsi,
        ema20=ema20,
        ema50=ema50,
        adx=adx,
        atr_pct=atr / close,
        ema20_slope_atr=slope,
        return_5m=r5,
        return_15m=r15,
        return_30m=r30,
        extension_atr=extension,
        call_score=call_score,
        put_score=put_score,
    )


def build_v34_chain_state(
    chain: DeltaOptionChainSnapshot,
    *,
    as_of: date,
    captured_ns: int,
    config: V34Config | None = None,
) -> V34ChainState:
    cfg = config or V34Config()
    rows: list[V34ContractState] = []

    for ticker in chain.tickers:
        dte = (ticker.expiry - as_of).days
        if not cfg.min_dte <= dte <= cfg.max_dte:
            continue
        if not ticker.has_tradeable_quote or ticker.delta is None:
            continue
        if not cfg.min_abs_delta <= abs(float(ticker.delta)) <= cfg.max_abs_delta:
            continue

        spread = ticker.spread_fraction
        if spread is None or float(spread) > cfg.max_spread_fraction:
            continue

        iv = _iv(ticker)
        if iv is None:
            continue

        assert ticker.bid_size is not None
        assert ticker.ask_size is not None

        rows.append(
            V34ContractState(
                symbol=ticker.symbol,
                contract_type=ticker.contract_type,
                iv=iv,
                mark_price=float(ticker.mark_price),
                open_interest=float(ticker.open_interest_contracts),
                volume=float(ticker.volume or 0),
                bid_size=float(ticker.bid_size),
                ask_size=float(ticker.ask_size),
            )
        )

    if not rows:
        raise ValueError("No V3.4 core option contracts available")

    return V34ChainState(
        underlying=chain.underlying,
        captured_ns=captured_ns,
        contracts=tuple(sorted(rows, key=lambda x: x.symbol)),
    )


def evaluate_v34_chain_flow(
    previous: V34ChainState,
    current: V34ChainState,
) -> V34FlowState:
    if previous.underlying != current.underlying:
        raise ValueError("Chain-flow underlyings must match")

    old = {x.symbol: x for x in previous.contracts}
    new = {x.symbol: x for x in current.contracts}
    common = sorted(set(old) & set(new))

    metrics: dict[str, dict[str, float]] = {}
    for side in ("call_options", "put_options"):
        pairs = [
            (old[s], new[s])
            for s in common
            if old[s].contract_type == side and new[s].contract_type == side
        ]
        if not pairs:
            metrics[side] = dict(iv=0, oi=0, vol=0, depth=0, premium=0)
            continue

        old_oi = sum(max(a.open_interest, 0) for a, _ in pairs)
        new_oi = sum(max(b.open_interest, 0) for _, b in pairs)
        oi_change = (new_oi - old_oi) / max(old_oi, 1.0)

        # Delta ticker volume is a rolling aggregate, not an execution-side
        # aggressor feed. Only positive net change is usable as weak support;
        # a decrease/reset must never be converted into a synthetic surge.
        vol_delta = sum(
            max(b.volume - a.volume, 0.0)
            for a, b in pairs
        )
        iv_change = float(median(b.iv - a.iv for a, b in pairs))
        premium = float(
            median(b.mark_price / a.mark_price - 1.0 for a, b in pairs if a.mark_price > 0)
        )
        old_bid = sum(a.bid_size for a, _ in pairs)
        old_ask = sum(a.ask_size for a, _ in pairs)
        new_bid = sum(b.bid_size for _, b in pairs)
        new_ask = sum(b.ask_size for _, b in pairs)

        old_depth = (old_bid - old_ask) / max(old_bid + old_ask, 1.0)
        new_depth = (new_bid - new_ask) / max(new_bid + new_ask, 1.0)
        depth_change = new_depth - old_depth

        metrics[side] = {
            "iv": iv_change,
            "oi": oi_change,
            "vol": vol_delta,
            "depth": depth_change,
            "premium": premium,
        }

    call = metrics["call_options"]
    put = metrics["put_options"]

    iv_edge = call["iv"] - put["iv"]
    oi_edge = call["oi"] - put["oi"]
    volume_total = call["vol"] + put["vol"]
    volume_edge = (
        (call["vol"] - put["vol"]) / volume_total
        if volume_total > 0
        else 0.0
    )
    depth_edge = call["depth"] - put["depth"]
    premium_edge = call["premium"] - put["premium"]

    call_score, call_confirms = _flow_score(
        iv_edge, oi_edge, volume_edge, depth_edge, premium_edge
    )
    put_score, put_confirms = _flow_score(
        -iv_edge, -oi_edge, -volume_edge, -depth_edge, -premium_edge
    )

    return V34FlowState(
        call_score=call_score,
        put_score=put_score,
        call_confirmations=call_confirms,
        put_confirmations=put_confirms,
        iv_edge=iv_edge,
        oi_edge=oi_edge,
        volume_edge=volume_edge,
        depth_edge=depth_edge,
        premium_edge=premium_edge,
    )


def evaluate_v34_shadow_signal(
    candles: DeltaCandleSnapshot,
    chain: DeltaOptionChainSnapshot,
    *,
    as_of: date,
    captured_ns: int,
    previous_chain: V34ChainState | None,
    config: V34Config | None = None,
) -> V34ShadowSignal:
    cfg = config or V34Config()
    if candles.underlying != chain.underlying:
        raise ValueError("Candle and chain underlyings must match")

    underlying = evaluate_v34_underlying(candles)
    current = build_v34_chain_state(
        chain,
        as_of=as_of,
        captured_ns=captured_ns,
        config=cfg,
    )

    flow: V34FlowState | None = None
    reasons: list[str] = []

    if previous_chain is None:
        reasons.append("warming_up_chain_flow")
    else:
        age = (captured_ns - previous_chain.captured_ns) / 1_000_000_000
        if previous_chain.underlying != current.underlying:
            reasons.append("previous_chain_underlying_mismatch")
        elif not cfg.min_previous_age_seconds <= age <= cfg.max_previous_age_seconds:
            reasons.append("previous_chain_age_outside_window")
        else:
            call_common = _common_contract_count(
                previous_chain,
                current,
                "call_options",
            )
            put_common = _common_contract_count(
                previous_chain,
                current,
                "put_options",
            )
            if (
                call_common < cfg.min_common_contracts_per_side
                or put_common < cfg.min_common_contracts_per_side
            ):
                reasons.append("insufficient_chain_overlap")
            else:
                flow = evaluate_v34_chain_flow(previous_chain, current)

    call_score = underlying.call_score
    put_score = underlying.put_score

    if flow is not None:
        call_score += flow.call_score
        put_score += flow.put_score

    call_overextended = (
        underlying.close > underlying.ema20
        and underlying.extension_atr > cfg.max_extension_atr
    )
    put_overextended = (
        underlying.close < underlying.ema20
        and underlying.extension_atr > cfg.max_extension_atr
    )

    if call_overextended:
        call_score -= 15
        reasons.append("call_overextended")
    if put_overextended:
        put_score -= 15
        reasons.append("put_overextended")

    call_score = _clamp(call_score, 0, 100)
    put_score = _clamp(put_score, 0, 100)
    decision = V34Decision.WAIT

    trend_regime_ok = underlying.adx >= cfg.min_adx
    if not trend_regime_ok:
        reasons.append("weak_trend_regime")

    call_momentum_aligned = (
        underlying.return_15m > 0
        and underlying.return_30m > 0
    )
    put_momentum_aligned = (
        underlying.return_15m < 0
        and underlying.return_30m < 0
    )

    if flow is None:
        reasons.append("flow_confirmation_required")
    elif (
        trend_regime_ok
        and call_momentum_aligned
        and not call_overextended
        and call_score >= cfg.min_total_score
        and underlying.call_score >= cfg.min_direction_score
        and call_score - put_score >= cfg.min_score_edge
        and flow.call_confirmations >= cfg.min_flow_confirmations
    ):
        decision = V34Decision.CALL
    elif (
        trend_regime_ok
        and put_momentum_aligned
        and not put_overextended
        and put_score >= cfg.min_total_score
        and underlying.put_score >= cfg.min_direction_score
        and put_score - call_score >= cfg.min_score_edge
        and flow.put_confirmations >= cfg.min_flow_confirmations
    ):
        decision = V34Decision.PUT
    else:
        if not call_momentum_aligned and not put_momentum_aligned:
            reasons.append("mid_horizon_momentum_conflict")
        elif call_score >= put_score and not call_momentum_aligned:
            reasons.append("call_mid_horizon_not_aligned")
        elif put_score > call_score and not put_momentum_aligned:
            reasons.append("put_mid_horizon_not_aligned")
        reasons.append("score_or_confirmation_gate_not_met")

    confidence = (
        max(call_score, put_score)
        if decision is not V34Decision.WAIT
        else abs(call_score - put_score)
    )

    return V34ShadowSignal(
        underlying=candles.underlying,
        candle_close_ms=candles.candle_close_ms,
        decision=decision,
        call_score=call_score,
        put_score=put_score,
        confidence=confidence,
        underlying_state=underlying,
        flow_state=flow,
        chain_state=current,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _direction_score(
    *,
    side: int,
    close: float,
    ema20: float,
    ema50: float,
    slope: float,
    rsi: float,
    adx: float,
    r5: float,
    r15: float,
    r30: float,
) -> float:
    score = 0.0
    if side * (ema20 - ema50) > 0:
        score += 18
    if side * slope > 0.03:
        score += 12
    if side * (close - ema20) > 0:
        score += 8
    if side * r5 > 0:
        score += 8
    if side * r15 > 0:
        score += 14
    if side * r30 > 0:
        score += 14

    if side > 0 and 50 <= rsi <= 70:
        score += 12
    elif side < 0 and 30 <= rsi <= 50:
        score += 12
    elif side > 0 and rsi > 75:
        score -= 8
    elif side < 0 and rsi < 25:
        score -= 8

    score += 8 if adx >= 20 else (-8 if adx < 15 else 0)
    return _clamp(score, 0, 75)


def _flow_score(
    iv_edge: float,
    oi_edge: float,
    volume_edge: float,
    depth_edge: float,
    premium_edge: float,
) -> tuple[float, int]:
    """Score directional option flow conservatively.

    Premium, IV and depth-change can contribute primary confirmations.
    OI and aggregate volume are ambiguous without price/volatility context:
    writers can increase OI and ticker volume does not identify aggressor side.
    They therefore count only when corroborated by primary evidence.
    """

    score = 0.0
    confirms = 0

    premium_confirm = premium_edge > 0.005
    iv_confirm = iv_edge > 0.002
    depth_confirm = depth_edge > 0.10

    if premium_confirm:
        score += 10.0
        confirms += 1
    if iv_confirm:
        score += 8.0
        confirms += 1
    if depth_confirm:
        score += 5.0
        confirms += 1

    # OI is directional support only when option premium or IV is already
    # moving in the same direction. OI by itself cannot distinguish buying
    # from writing.
    if oi_edge > 0.01 and (premium_confirm or iv_confirm):
        score += 4.0
        confirms += 1

    # Volume is also supporting evidence only. It is an aggregate ticker
    # quantity, not signed aggressor flow.
    if volume_edge > 0.10 and (premium_confirm or depth_confirm):
        score += 3.0
        confirms += 1

    return score, confirms


def _common_contract_count(
    previous: V34ChainState,
    current: V34ChainState,
    contract_type: DeltaOptionContractType,
) -> int:
    old = {
        row.symbol
        for row in previous.contracts
        if row.contract_type == contract_type
    }
    new = {
        row.symbol
        for row in current.contracts
        if row.contract_type == contract_type
    }
    return len(old & new)


def _iv(ticker: DeltaOptionTicker) -> float | None:
    if ticker.mark_iv is not None:
        return float(ticker.mark_iv)
    if ticker.bid_iv is not None and ticker.ask_iv is not None:
        return (float(ticker.bid_iv) + float(ticker.ask_iv)) / 2
    return None


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)
