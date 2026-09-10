from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from nautilus_delta_options.delta.history import DeltaCandleSnapshot
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaUnderlying,
)
from nautilus_delta_options.selection.v34_quality import (
    V34ContractQuality,
    V34QualityConfig,
    rank_v34_contract_quality,
)
from nautilus_delta_options.signals.v34 import (
    V34ChainState,
    V34Config,
    V34ShadowSignal,
    evaluate_v34_shadow_signal,
)


class V34ShadowHistoryMarketData(Protocol):
    def fetch_5m_candles(
        self,
        underlying: DeltaUnderlying,
        *,
        count: int = 240,
        now_s: int | None = None,
    ) -> DeltaCandleSnapshot: ...


class V34ShadowOptionMarketData(Protocol):
    def fetch_option_chain(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionChainSnapshot: ...


@dataclass(frozen=True, slots=True)
class V34ShadowQualitySelection:
    underlying: DeltaUnderlying
    call_candidate_count: int
    put_candidate_count: int
    best_call: V34ContractQuality | None
    best_put: V34ContractQuality | None


@dataclass(frozen=True, slots=True)
class V34ShadowCycle:
    candle_close_ms: int | None
    evaluated: bool
    signals: tuple[V34ShadowSignal, ...]
    warnings: tuple[str, ...]
    quality: tuple[V34ShadowQualitySelection, ...] = ()


class V34ShadowObserver:
    """Stateful V3.4 observer with deliberately zero entry authority.

    The observer owns only public-market snapshots and V3.4 scoring state.
    It has no PaperLedgerSession dependency and therefore cannot open,
    close, size, or mutate paper positions.
    """

    def __init__(
        self,
        *,
        history_client: V34ShadowHistoryMarketData,
        delta_client: V34ShadowOptionMarketData,
        underlyings: Sequence[DeltaUnderlying] = ("BTC", "ETH"),
        config: V34Config | None = None,
        quality_config: V34QualityConfig | None = None,
        candle_count: int = 240,
        clock_ns: Callable[[], int] = time.time_ns,
        utc_date: Callable[[], date] | None = None,
    ) -> None:
        if not underlyings:
            raise ValueError("At least one V3.4 underlying is required")
        if len(underlyings) != len(set(underlyings)):
            raise ValueError("V3.4 underlyings must be unique")
        if candle_count < 200:
            raise ValueError("V3.4 shadow requires at least 200 candles")

        self._history_client = history_client
        self._delta_client = delta_client
        self._underlyings = tuple(underlyings)
        self._config = config or V34Config()
        self._quality_config = quality_config or V34QualityConfig()
        self._candle_count = candle_count
        self._clock_ns = clock_ns
        self._utc_date = utc_date or (lambda: datetime.now(UTC).date())
        self._previous_chains: dict[DeltaUnderlying, V34ChainState] = {}
        self._last_candle_close_ms: int | None = None

    @property
    def entry_authority(self) -> bool:
        return False

    @property
    def last_candle_close_ms(self) -> int | None:
        return self._last_candle_close_ms

    def run_cycle(self) -> V34ShadowCycle:
        try:
            candles = tuple(
                self._history_client.fetch_5m_candles(
                    underlying,
                    count=self._candle_count,
                )
                for underlying in self._underlyings
            )
        except Exception as error:
            return V34ShadowCycle(
                candle_close_ms=None,
                evaluated=False,
                signals=(),
                warnings=(f"Delta candle fetch failed: {error}",),
            )

        candle_closes = {snapshot.candle_close_ms for snapshot in candles}
        if len(candle_closes) != 1:
            closes = ", ".join(
                f"{snapshot.underlying}={snapshot.candle_close_ms}"
                for snapshot in candles
            )
            return V34ShadowCycle(
                candle_close_ms=None,
                evaluated=False,
                signals=(),
                warnings=(f"Delta completed-candle boundary not aligned: {closes}",),
            )

        candle_close_ms = next(iter(candle_closes))
        if (
            self._last_candle_close_ms is not None
            and candle_close_ms <= self._last_candle_close_ms
        ):
            return V34ShadowCycle(
                candle_close_ms=candle_close_ms,
                evaluated=False,
                signals=(),
                warnings=(),
            )

        candle_by_underlying = {
            snapshot.underlying: snapshot
            for snapshot in candles
        }

        try:
            chains = {
                underlying: self._delta_client.fetch_option_chain(underlying)
                for underlying in self._underlyings
            }

            as_of = self._utc_date()
            candidate_signals: list[V34ShadowSignal] = []
            quality_selections: list[V34ShadowQualitySelection] = []

            for underlying in self._underlyings:
                chain = chains[underlying]
                captured_ns = self._clock_ns()
                signal = evaluate_v34_shadow_signal(
                    candle_by_underlying[underlying],
                    chain,
                    as_of=as_of,
                    captured_ns=captured_ns,
                    previous_chain=self._previous_chains.get(underlying),
                    config=self._config,
                )
                candidate_signals.append(signal)

                call_ranked = rank_v34_contract_quality(
                    chain,
                    contract_type="call_options",
                    as_of=as_of,
                    config=self._quality_config,
                )
                put_ranked = rank_v34_contract_quality(
                    chain,
                    contract_type="put_options",
                    as_of=as_of,
                    config=self._quality_config,
                )
                quality_selections.append(
                    V34ShadowQualitySelection(
                        underlying=underlying,
                        call_candidate_count=len(call_ranked),
                        put_candidate_count=len(put_ranked),
                        best_call=call_ranked[0] if call_ranked else None,
                        best_put=put_ranked[0] if put_ranked else None,
                    )
                )
        except Exception as error:
            # All-or-nothing snapshot ownership: if either side fails, do not
            # advance the V3.4 previous-chain baseline or candle boundary.
            return V34ShadowCycle(
                candle_close_ms=candle_close_ms,
                evaluated=False,
                signals=(),
                warnings=(f"V3.4 scoring cycle failed: {error}",),
            )

        signals = tuple(candidate_signals)
        quality = tuple(quality_selections)

        # Commit rolling state only after both BTC and ETH were evaluated.
        for signal in signals:
            self._previous_chains[signal.underlying] = signal.chain_state
        self._last_candle_close_ms = candle_close_ms

        return V34ShadowCycle(
            candle_close_ms=candle_close_ms,
            evaluated=True,
            signals=signals,
            warnings=(),
            quality=quality,
        )


def v34_shadow_cycle_payload(cycle: V34ShadowCycle) -> dict[str, object]:
    quality_by_underlying = {row.underlying: row for row in cycle.quality}
    return {
        "status": (
            "ready"
            if cycle.evaluated
            else ("warning" if cycle.warnings else "idle")
        ),
        "entry_authority": False,
        "candle_close_ms": cycle.candle_close_ms,
        "signals": [
            v34_shadow_signal_payload(
                signal,
                quality_by_underlying.get(signal.underlying),
            )
            for signal in cycle.signals
        ],
        "warnings": list(cycle.warnings),
    }


def v34_shadow_signal_payload(
    signal: V34ShadowSignal,
    quality: V34ShadowQualitySelection | None = None,
) -> dict[str, object]:
    u = signal.underlying_state
    flow = signal.flow_state

    return {
        "underlying": signal.underlying,
        "candle_close_ms": signal.candle_close_ms,
        "decision": signal.decision.value.upper(),
        "call_score": signal.call_score,
        "put_score": signal.put_score,
        # Kept for API compatibility; score_edge is the clearer name.
        "confidence": signal.confidence,
        "score_edge": signal.confidence,
        "reasons": list(signal.reasons),
        "underlying_state": {
            "close": u.close,
            "rsi": u.rsi,
            "ema20": u.ema20,
            "ema50": u.ema50,
            "adx": u.adx,
            "atr_pct": u.atr_pct,
            "ema20_slope_atr": u.ema20_slope_atr,
            "return_5m": u.return_5m,
            "return_15m": u.return_15m,
            "return_30m": u.return_30m,
            "extension_atr": u.extension_atr,
            "direction_call_score": u.call_score,
            "direction_put_score": u.put_score,
        },
        "flow_state": (
            None
            if flow is None
            else {
                "call_score": flow.call_score,
                "put_score": flow.put_score,
                "call_confirmations": flow.call_confirmations,
                "put_confirmations": flow.put_confirmations,
                "iv_edge": flow.iv_edge,
                "oi_edge": flow.oi_edge,
                "volume_edge": flow.volume_edge,
                "depth_edge": flow.depth_edge,
                "premium_edge": flow.premium_edge,
            }
        ),
        "core_contracts": len(signal.chain_state.contracts),
        "contract_quality": _quality_selection_payload(quality),
    }


def _quality_selection_payload(
    quality: V34ShadowQualitySelection | None,
) -> dict[str, object] | None:
    if quality is None:
        return None

    return {
        "call_candidate_count": quality.call_candidate_count,
        "put_candidate_count": quality.put_candidate_count,
        "best_call": _contract_quality_payload(quality.best_call),
        "best_put": _contract_quality_payload(quality.best_put),
    }


def _contract_quality_payload(
    row: V34ContractQuality | None,
) -> dict[str, object] | None:
    if row is None:
        return None

    c = row.components
    return {
        "symbol": row.symbol,
        "score": row.score,
        "dte": row.dte,
        "strike": row.strike,
        "abs_delta": row.abs_delta,
        "spread_fraction": row.spread_fraction,
        "mark_iv": row.mark_iv,
        "gamma_convexity_1pct": row.gamma_convexity_1pct,
        "theta_burden": row.theta_burden,
        "convexity_efficiency": row.convexity_efficiency,
        "vega_efficiency": row.vega_efficiency,
        "open_interest": row.open_interest,
        "volume": row.volume,
        "quote_depth": row.quote_depth,
        "components": {
            "delta_fit": c.delta_fit,
            "spread": c.spread,
            "convexity_efficiency": c.convexity_efficiency,
            "theta": c.theta,
            "vega": c.vega,
            "iv": c.iv,
            "liquidity": c.liquidity,
        },
    }
