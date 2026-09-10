from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import median

from nautilus_delta_options.delta.models import (
    DeltaOptionContractType,
    DeltaOptionTicker,
)
from nautilus_delta_options.delta.public_client import DeltaOptionChainSnapshot


@dataclass(frozen=True, slots=True)
class V34QualityConfig:
    min_dte: int = 1
    max_dte: int = 3
    target_abs_delta: float = 0.50
    min_abs_delta: float = 0.35
    max_abs_delta: float = 0.65
    # Quality layer is intentionally stricter than the broad 2.5% market
    # eligibility ceiling. Long-premium entries pay the spread immediately.
    max_spread_fraction: float = 0.015
    # Absolute liquidity floors are deliberately separate from relative
    # ranking. A thin contract must not become 'high quality' merely
    # because the rest of the current peer set is worse.
    min_open_interest: float = 100.0
    min_volume: float = 1.0
    min_quote_size: float = 1.0

    delta_weight: float = 25.0
    spread_weight: float = 20.0
    gamma_weight: float = 15.0
    theta_weight: float = 15.0
    vega_weight: float = 5.0
    iv_weight: float = 10.0
    liquidity_weight: float = 10.0

    def __post_init__(self) -> None:
        if self.min_dte < 1:
            raise ValueError("V3.4 contract quality blocks 0DTE")
        if self.max_dte < self.min_dte:
            raise ValueError("max_dte cannot be below min_dte")
        if not 0 < self.min_abs_delta < self.target_abs_delta < self.max_abs_delta < 1:
            raise ValueError("target delta must sit inside the configured band")
        if self.max_spread_fraction <= 0:
            raise ValueError("max_spread_fraction must be positive")
        if self.min_open_interest < 0:
            raise ValueError("min_open_interest cannot be negative")
        if self.min_volume < 0:
            raise ValueError("min_volume cannot be negative")
        if self.min_quote_size <= 0:
            raise ValueError("min_quote_size must be positive")

        total = (
            self.delta_weight
            + self.spread_weight
            + self.gamma_weight
            + self.theta_weight
            + self.vega_weight
            + self.iv_weight
            + self.liquidity_weight
        )
        if not math.isclose(total, 100.0, abs_tol=1e-9):
            raise ValueError("V3.4 quality weights must sum to 100")


@dataclass(frozen=True, slots=True)
class V34QualityComponents:
    delta_fit: float
    spread: float
    convexity_efficiency: float
    theta: float
    vega: float
    iv: float
    liquidity: float


@dataclass(frozen=True, slots=True)
class V34ContractQuality:
    symbol: str
    contract_type: DeltaOptionContractType
    dte: int
    strike: float
    abs_delta: float
    spread_fraction: float
    mark_iv: float
    gamma_convexity_1pct: float
    theta_burden: float
    convexity_efficiency: float
    vega_efficiency: float
    open_interest: float
    volume: float
    quote_depth: float
    score: float
    components: V34QualityComponents


@dataclass(frozen=True, slots=True)
class _RawQuality:
    ticker: DeltaOptionTicker
    dte: int
    abs_delta: float
    spread_fraction: float
    iv: float
    gamma_convexity_1pct: float
    theta_burden: float
    convexity_efficiency: float
    vega_efficiency: float
    open_interest: float
    volume: float
    quote_depth: float


def rank_v34_contract_quality(
    chain: DeltaOptionChainSnapshot,
    *,
    contract_type: DeltaOptionContractType,
    as_of: date,
    config: V34QualityConfig | None = None,
) -> tuple[V34ContractQuality, ...]:
    """Rank buy-side option contracts without creating entry authority.

    This layer deliberately does not know about ledgers, position sizing,
    stops, targets, or order placement. It only ranks currently tradeable
    Delta contracts for quality after a direction has been chosen elsewhere.

    DTE receives no fixed preference. Nearer/further expiries compete through
    their observed theta burden, vega efficiency, IV and liquidity.
    """

    cfg = config or V34QualityConfig()
    rows = tuple(
        row
        for ticker in chain.tickers
        if (row := _raw_quality(ticker, contract_type, as_of, cfg)) is not None
    )
    if not rows:
        return ()

    convexity_scores = _normalize_high([x.convexity_efficiency for x in rows])
    theta_scores = _normalize_low([x.theta_burden for x in rows])
    vega_scores = _normalize_high([x.vega_efficiency for x in rows])
    iv_scores = _relative_iv_scores(rows)
    liquidity_scores = _liquidity_scores(rows)

    ranked: list[V34ContractQuality] = []

    for index, row in enumerate(rows):
        delta_fit = _delta_fit(row.abs_delta, cfg)
        spread_fit = _clamp(
            1.0 - row.spread_fraction / cfg.max_spread_fraction,
            0.0,
            1.0,
        )

        components = V34QualityComponents(
            delta_fit=cfg.delta_weight * delta_fit,
            spread=cfg.spread_weight * spread_fit,
            convexity_efficiency=(
                cfg.gamma_weight * convexity_scores[index]
            ),
            theta=cfg.theta_weight * theta_scores[index],
            vega=cfg.vega_weight * vega_scores[index],
            iv=cfg.iv_weight * iv_scores[index],
            liquidity=cfg.liquidity_weight * liquidity_scores[index],
        )

        score = (
            components.delta_fit
            + components.spread
            + components.convexity_efficiency
            + components.theta
            + components.vega
            + components.iv
            + components.liquidity
        )

        ranked.append(
            V34ContractQuality(
                symbol=row.ticker.symbol,
                contract_type=row.ticker.contract_type,
                dte=row.dte,
                strike=float(row.ticker.strike_price),
                abs_delta=row.abs_delta,
                spread_fraction=row.spread_fraction,
                mark_iv=row.iv,
                gamma_convexity_1pct=row.gamma_convexity_1pct,
                theta_burden=row.theta_burden,
                convexity_efficiency=row.convexity_efficiency,
                vega_efficiency=row.vega_efficiency,
                open_interest=row.open_interest,
                volume=row.volume,
                quote_depth=row.quote_depth,
                score=score,
                components=components,
            )
        )

    return tuple(
        sorted(
            ranked,
            key=lambda x: (
                -x.score,
                x.spread_fraction,
                abs(x.abs_delta - cfg.target_abs_delta),
                -x.open_interest,
                -x.volume,
                x.symbol,
            ),
        )
    )


def _raw_quality(
    ticker: DeltaOptionTicker,
    contract_type: DeltaOptionContractType,
    as_of: date,
    cfg: V34QualityConfig,
) -> _RawQuality | None:
    if ticker.contract_type != contract_type:
        return None

    dte = (ticker.expiry - as_of).days
    if not cfg.min_dte <= dte <= cfg.max_dte:
        return None

    if not ticker.has_tradeable_quote:
        return None

    if (
        ticker.delta is None
        or ticker.gamma is None
        or ticker.theta is None
        or ticker.vega is None
    ):
        return None

    abs_delta = abs(float(ticker.delta))
    if not cfg.min_abs_delta <= abs_delta <= cfg.max_abs_delta:
        return None

    spread = ticker.spread_fraction
    if spread is None:
        return None
    spread_fraction = float(spread)
    if spread_fraction < 0 or spread_fraction > cfg.max_spread_fraction:
        return None

    iv = _iv(ticker)
    if iv is None or not math.isfinite(iv) or iv <= 0:
        return None

    mark_price = float(ticker.mark_price)
    spot_price = float(ticker.spot_price)
    if mark_price <= 0 or spot_price <= 0:
        return None

    assert ticker.bid_size is not None
    assert ticker.ask_size is not None
    bid_size = float(ticker.bid_size)
    ask_size = float(ticker.ask_size)
    if bid_size < cfg.min_quote_size or ask_size < cfg.min_quote_size:
        return None

    open_interest = max(float(ticker.open_interest_contracts), 0.0)
    if open_interest < cfg.min_open_interest:
        return None

    volume = max(float(ticker.volume or 0), 0.0)
    if volume < cfg.min_volume:
        return None
    quote_depth = bid_size + ask_size

    # Approximate relative delta change generated by a 1% underlying move.
    # This is dimensionless and therefore comparable between BTC and ETH.
    gamma_convexity_1pct = (
        abs(float(ticker.gamma)) * (spot_price * 0.01) / max(abs_delta, 1e-9)
    )

    # Delta reports theta and vega in option-price units. Dividing by current
    # premium expresses each as burden/exposure per unit of premium paid.
    theta_burden = abs(float(ticker.theta)) / mark_price
    vega_efficiency = abs(float(ticker.vega)) / mark_price
    # Raw gamma is not rewarded by itself. Near-expiry options can have
    # attractive gamma only because they also carry very heavy decay.
    # Rank the convexity received per unit of observed theta burden.
    convexity_efficiency = gamma_convexity_1pct / max(theta_burden, 1e-9)

    metrics = (
        gamma_convexity_1pct,
        theta_burden,
        convexity_efficiency,
        vega_efficiency,
        open_interest,
        volume,
        quote_depth,
    )
    if not all(math.isfinite(value) and value >= 0 for value in metrics):
        return None

    return _RawQuality(
        ticker=ticker,
        dte=dte,
        abs_delta=abs_delta,
        spread_fraction=spread_fraction,
        iv=iv,
        gamma_convexity_1pct=gamma_convexity_1pct,
        theta_burden=theta_burden,
        convexity_efficiency=convexity_efficiency,
        vega_efficiency=vega_efficiency,
        open_interest=open_interest,
        volume=volume,
        quote_depth=quote_depth,
    )


def _relative_iv_scores(rows: tuple[_RawQuality, ...]) -> list[float]:
    # Compare IV within side + DTE peers so the natural term structure does
    # not make a longer-dated contract look artificially expensive/cheap.
    medians: dict[int, float] = {}
    for dte in {row.dte for row in rows}:
        medians[dte] = median(row.iv for row in rows if row.dte == dte)

    relative = [
        row.iv / medians[row.dte]
        if medians[row.dte] > 0
        else 1.0
        for row in rows
    ]
    return _normalize_low(relative)


def _liquidity_scores(rows: tuple[_RawQuality, ...]) -> list[float]:
    oi = _normalize_high([math.log1p(x.open_interest) for x in rows])
    volume = _normalize_high([math.log1p(x.volume) for x in rows])
    depth = _normalize_high([math.log1p(x.quote_depth) for x in rows])

    return [
        (oi_score + volume_score + depth_score) / 3.0
        for oi_score, volume_score, depth_score in zip(
            oi,
            volume,
            depth,
            strict=True,
        )
    ]


def _delta_fit(abs_delta: float, cfg: V34QualityConfig) -> float:
    if abs_delta <= cfg.target_abs_delta:
        width = cfg.target_abs_delta - cfg.min_abs_delta
    else:
        width = cfg.max_abs_delta - cfg.target_abs_delta
    return _clamp(
        1.0 - abs(abs_delta - cfg.target_abs_delta) / width,
        0.0,
        1.0,
    )


def _normalize_high(values: list[float]) -> list[float]:
    if not values:
        return []

    low = min(values)
    high = max(values)
    if math.isclose(low, high, rel_tol=1e-12, abs_tol=1e-12):
        return [0.5 for _ in values]

    width = high - low
    return [_clamp((value - low) / width, 0.0, 1.0) for value in values]


def _normalize_low(values: list[float]) -> list[float]:
    return [1.0 - value for value in _normalize_high(values)]


def _iv(ticker: DeltaOptionTicker) -> float | None:
    if ticker.mark_iv is not None:
        return float(ticker.mark_iv)
    if ticker.bid_iv is not None and ticker.ask_iv is not None:
        return (float(ticker.bid_iv) + float(ticker.ask_iv)) / 2.0
    return None


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)
