from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol, cast

from nautilus_delta_options.delta.history import DeltaCandleSnapshot
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaUnderlying,
)
from nautilus_delta_options.paper import quote_safety
from nautilus_delta_options.research.experiment1.population import (
    PopulationBinding,
    PopulationStatus,
)
from nautilus_delta_options.selection.v34_quality import (
    V34ContractQuality,
    V34QualityConfig,
    rank_v34_contract_quality,
)
from nautilus_delta_options.signals.v34 import (
    V34ChainState,
    V34Config,
    V34Decision,
    V34ShadowSignal,
    evaluate_v34_shadow_signal,
)


class ResearchHistoryMarketData(Protocol):
    def fetch_5m_candles(
        self,
        underlying: DeltaUnderlying,
        *,
        count: int = 240,
        now_s: int | None = None,
    ) -> DeltaCandleSnapshot: ...


class ResearchOptionMarketData(Protocol):
    def fetch_option_chain(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionChainSnapshot: ...


@dataclass(frozen=True, slots=True)
class ResearchQualitySelection:
    underlying: DeltaUnderlying
    call_candidate_count: int
    put_candidate_count: int
    best_call: V34ContractQuality | None
    best_put: V34ContractQuality | None


@dataclass(frozen=True, slots=True)
class EvaluationReturn:
    underlying: DeltaUnderlying
    candles: DeltaCandleSnapshot
    source_option_chain: DeltaOptionChainSnapshot
    evaluated_option_chain: DeltaOptionChainSnapshot
    previous_chain: V34ChainState | None
    signal: V34ShadowSignal
    as_of: date
    captured_ns: int

    # Population ownership is established immediately when the
    # evaluator returns, before evidence persistence and ranking.
    population_status: PopulationStatus
    retry_ordinal: int
    canonical_decision: V34Decision | None
    canonical_captured_ns: int | None


EvaluationReturnHook = Callable[[EvaluationReturn], bool | None]


@dataclass(frozen=True, slots=True)
class ResearchObserverCycle:
    candle_close_ms: int | None
    evaluated: bool
    signals: tuple[V34ShadowSignal, ...]
    warnings: tuple[str, ...]
    quality: tuple[ResearchQualitySelection, ...] = ()

    # Research-only evidence. These may be populated even when the frozen
    # observer-visible result is an aborted cycle with signals=().
    evaluation_returns: tuple[EvaluationReturn, ...] = ()
    evidence_faults: tuple[str, ...] = ()
    failure_stage: str | None = None


class Experiment1ObserverStateMachine:
    """Research-only reproduction of the frozen V3.4 observer state machine.

    Evidence hooks have zero authority over frozen observer state.
    """

    def __init__(
        self,
        *,
        history_client: ResearchHistoryMarketData,
        delta_client: ResearchOptionMarketData,
        underlyings: Sequence[DeltaUnderlying] = ("BTC", "ETH"),
        config: V34Config | None = None,
        quality_config: V34QualityConfig | None = None,
        candle_count: int = 240,
        clock_ns: Callable[[], int] = time.time_ns,
        utc_date: Callable[[], date] | None = None,
        on_evaluation_return: EvaluationReturnHook | None = None,
        population_bindings: Sequence[PopulationBinding] = (),
    ) -> None:
        if not underlyings:
            raise ValueError(
                "At least one V3.4 underlying is required"
            )

        if len(underlyings) != len(set(underlyings)):
            raise ValueError(
                "V3.4 underlyings must be unique"
            )

        if candle_count < 200:
            raise ValueError(
                "V3.4 shadow requires at least 200 candles"
            )

        self._history_client = history_client
        self._delta_client = delta_client
        self._underlyings = tuple(underlyings)
        self._config = config or V34Config()
        self._quality_config = (
            quality_config or V34QualityConfig()
        )
        self._candle_count = candle_count
        self._clock_ns = clock_ns
        self._utc_date = (
            utc_date
            or (lambda: datetime.now(UTC).date())
        )
        self._on_evaluation_return = on_evaluation_return

        self._previous_chains: dict[
            DeltaUnderlying,
            V34ChainState,
        ] = {}

        self._last_candle_close_ms: int | None = None

        # First actual successful evaluator return owns the
        # Experiment 1 population identity for (asset, candle).
        #
        # This state advances independently of observer rolling
        # signal state because even an ultimately aborted cycle
        # may have produced a real evaluator result.
        #
        # Evidence persistence has no authority over this map:
        # a rejected/lost journal write must never allow a later
        # retry to become canonical.
        self._canonical_returns: dict[
            tuple[DeltaUnderlying, int],
            tuple[V34Decision, int],
        ] = {}

        self._population_gaps: set[
            tuple[DeltaUnderlying, int]
        ] = set()

        self._evaluation_return_counts: dict[
            tuple[DeltaUnderlying, int],
            int,
        ] = {}

        self._hydrate_population_bindings(
            population_bindings
        )

    def _hydrate_population_bindings(
        self,
        bindings: Sequence[PopulationBinding],
    ) -> None:
        """Restore research population ownership only.

        Frozen observer rolling state is intentionally untouched.
        """

        for binding in bindings:
            if binding.asset not in self._underlyings:
                raise ValueError(
                    "population binding asset is not configured"
                )

            underlying = binding.asset

            key = (
                underlying,
                binding.signal_candle_close_ms,
            )

            if (
                key in self._canonical_returns
                or key in self._population_gaps
            ):
                raise ValueError(
                    "duplicate recovered population binding"
                )

            if binding.durable_evaluation_count < 0:
                raise ValueError(
                    "durable evaluation count cannot be negative"
                )

            if binding.binding_kind == "canonical":
                decision = binding.canonical_decision
                captured_ns = binding.canonical_captured_ns

                if decision is None or captured_ns is None:
                    raise ValueError(
                        "canonical population binding is incomplete"
                    )

                if captured_ns <= 0:
                    raise ValueError(
                        "canonical captured_ns must be positive"
                    )

                if binding.durable_evaluation_count < 1:
                    raise ValueError(
                        "canonical binding requires durable "
                        "evaluation evidence"
                    )

                self._canonical_returns[key] = (
                    decision,
                    captured_ns,
                )

            elif binding.binding_kind == "gap":
                if (
                    binding.canonical_decision is not None
                    or binding.canonical_captured_ns is not None
                ):
                    raise ValueError(
                        "gap binding cannot contain canonical state"
                    )

                self._population_gaps.add(key)

            else:
                raise ValueError(
                    "unsupported population binding kind"
                )

            # This is the next ordinal among evaluator-return
            # evidence already known durably after restart.
            #
            # It does not claim that an abruptly terminated process
            # could not have produced additional non-durable returns.
            self._evaluation_return_counts[key] = (
                binding.durable_evaluation_count
            )

    @property
    def entry_authority(self) -> bool:
        return False

    @property
    def last_candle_close_ms(self) -> int | None:
        return self._last_candle_close_ms

    def previous_chain(
        self,
        underlying: DeltaUnderlying,
    ) -> V34ChainState | None:
        return self._previous_chains.get(underlying)

    def run_cycle(self) -> ResearchObserverCycle:
        candles: list[DeltaCandleSnapshot] = []

        # Frozen ordering:
        # BTC history -> ETH history.
        for underlying in self._underlyings:
            try:
                snapshot = (
                    self._history_client.fetch_5m_candles(
                        underlying,
                        count=self._candle_count,
                    )
                )
            except Exception as error:
                return ResearchObserverCycle(
                    candle_close_ms=None,
                    evaluated=False,
                    signals=(),
                    warnings=(
                        f"Delta candle fetch failed: {error}",
                    ),
                    failure_stage=(
                        f"{underlying.lower()}_history"
                    ),
                )

            candles.append(snapshot)

        candle_tuple = tuple(candles)

        candle_closes = {
            snapshot.candle_close_ms
            for snapshot in candle_tuple
        }

        if len(candle_closes) != 1:
            closes = ", ".join(
                (
                    f"{snapshot.underlying}="
                    f"{snapshot.candle_close_ms}"
                )
                for snapshot in candle_tuple
            )

            return ResearchObserverCycle(
                candle_close_ms=None,
                evaluated=False,
                signals=(),
                warnings=(
                    "Delta completed-candle boundary "
                    f"not aligned: {closes}",
                ),
                failure_stage="candle_alignment",
            )

        candle_close_ms = next(iter(candle_closes))

        if (
            self._last_candle_close_ms is not None
            and candle_close_ms
            <= self._last_candle_close_ms
        ):
            return ResearchObserverCycle(
                candle_close_ms=candle_close_ms,
                evaluated=False,
                signals=(),
                warnings=(),
                failure_stage="duplicate_boundary",
            )

        candle_by_underlying = {
            snapshot.underlying: snapshot
            for snapshot in candle_tuple
        }

        chains: dict[
            DeltaUnderlying,
            DeltaOptionChainSnapshot,
        ] = {}

        # Frozen ordering:
        # BTC option chain -> ETH option chain.
        for underlying in self._underlyings:
            try:
                chains[underlying] = (
                    self._delta_client.fetch_option_chain(
                        underlying
                    )
                )
            except Exception as error:
                return self._scoring_failure(
                    candle_close_ms=candle_close_ms,
                    failure_stage=(
                        f"{underlying.lower()}_chain"
                    ),
                    error=error,
                    evaluation_returns=(),
                    evidence_faults=(),
                )

        try:
            as_of = self._utc_date()
        except Exception as error:
            return self._scoring_failure(
                candle_close_ms=candle_close_ms,
                failure_stage="as_of",
                error=error,
                evaluation_returns=(),
                evidence_faults=(),
            )

        candidate_signals: list[V34ShadowSignal] = []
        quality_selections: list[
            ResearchQualitySelection
        ] = []
        evaluation_returns: list[EvaluationReturn] = []
        evidence_faults: list[str] = []

        # Frozen ordering per asset:
        #
        # captured_ns
        # -> fresh_chain
        # -> evaluate
        # -> CALL ranking
        # -> PUT ranking
        #
        # Research evidence is observed immediately after the
        # evaluator successfully returns, but evidence failure
        # must never alter frozen state-machine behavior.
        for underlying in self._underlyings:
            source_chain = chains[underlying]

            try:
                captured_ns = self._clock_ns()
            except Exception as error:
                return self._scoring_failure(
                    candle_close_ms=candle_close_ms,
                    failure_stage=(
                        f"{underlying.lower()}_clock"
                    ),
                    error=error,
                    evaluation_returns=tuple(
                        evaluation_returns
                    ),
                    evidence_faults=tuple(
                        evidence_faults
                    ),
                )

            try:
                evaluated_chain = quote_safety.fresh_chain(
                    source_chain,
                    captured_ns,
                )
            except Exception as error:
                return self._scoring_failure(
                    candle_close_ms=candle_close_ms,
                    failure_stage=(
                        f"{underlying.lower()}_fresh_chain"
                    ),
                    error=error,
                    evaluation_returns=tuple(
                        evaluation_returns
                    ),
                    evidence_faults=tuple(
                        evidence_faults
                    ),
                )

            previous_chain = self._previous_chains.get(
                underlying
            )

            try:
                signal = evaluate_v34_shadow_signal(
                    candle_by_underlying[underlying],
                    evaluated_chain,
                    as_of=as_of,
                    captured_ns=captured_ns,
                    previous_chain=previous_chain,
                    config=self._config,
                )
            except Exception as error:
                return self._scoring_failure(
                    candle_close_ms=candle_close_ms,
                    failure_stage=(
                        f"{underlying.lower()}_evaluation"
                    ),
                    error=error,
                    evaluation_returns=tuple(
                        evaluation_returns
                    ),
                    evidence_faults=tuple(
                        evidence_faults
                    ),
                )

            # Same frozen ordering: evaluator result exists before
            # quality ranking begins.
            candidate_signals.append(signal)

            population_key = (
                underlying,
                signal.candle_close_ms,
            )

            canonical = self._canonical_returns.get(
                population_key
            )

            is_recovered_gap = (
                population_key in self._population_gaps
            )

            retry_ordinal = self._evaluation_return_counts.get(
                population_key,
                0,
            )

            if is_recovered_gap:
                population_status: PopulationStatus = (
                    "excluded_by_gap"
                )
                canonical_decision = None
                canonical_captured_ns = None

            elif canonical is None:
                population_status = "canonical"
                canonical_decision = signal.decision
                canonical_captured_ns = captured_ns

                # Seal canonical ownership BEFORE calling any
                # evidence hook or quality-ranking code.
                self._canonical_returns[
                    population_key
                ] = (
                    canonical_decision,
                    canonical_captured_ns,
                )
            else:
                (
                    canonical_decision,
                    canonical_captured_ns,
                ) = canonical

                if signal.decision == canonical_decision:
                    population_status = (
                        "same_decision_retry"
                    )
                else:
                    population_status = (
                        "conflicting_retry"
                    )

            self._evaluation_return_counts[
                population_key
            ] = retry_ordinal + 1

            evaluation_return = EvaluationReturn(
                underlying=underlying,
                candles=candle_by_underlying[underlying],
                source_option_chain=source_chain,
                evaluated_option_chain=evaluated_chain,
                previous_chain=previous_chain,
                signal=signal,
                as_of=as_of,
                captured_ns=captured_ns,
                population_status=population_status,
                retry_ordinal=retry_ordinal,
                canonical_decision=canonical_decision,
                canonical_captured_ns=canonical_captured_ns,
            )

            evaluation_returns.append(evaluation_return)

            hook = self._on_evaluation_return

            if hook is not None:
                try:
                    accepted = hook(evaluation_return)

                    if accepted is False:
                        evidence_faults.append(
                            
                                f"{underlying}: evaluation "
                                "evidence rejected"
                            
                        )
                except Exception as error:
                    evidence_faults.append(
                        
                            f"{underlying}: evaluation "
                            f"evidence fault: {error}"
                        
                    )

            try:
                call_ranked = rank_v34_contract_quality(
                    evaluated_chain,
                    contract_type="call_options",
                    as_of=as_of,
                    config=self._quality_config,
                )
            except Exception as error:
                return self._scoring_failure(
                    candle_close_ms=candle_close_ms,
                    failure_stage=(
                        f"{underlying.lower()}_call_ranking"
                    ),
                    error=error,
                    evaluation_returns=tuple(
                        evaluation_returns
                    ),
                    evidence_faults=tuple(
                        evidence_faults
                    ),
                )

            try:
                put_ranked = rank_v34_contract_quality(
                    evaluated_chain,
                    contract_type="put_options",
                    as_of=as_of,
                    config=self._quality_config,
                )
            except Exception as error:
                return self._scoring_failure(
                    candle_close_ms=candle_close_ms,
                    failure_stage=(
                        f"{underlying.lower()}_put_ranking"
                    ),
                    error=error,
                    evaluation_returns=tuple(
                        evaluation_returns
                    ),
                    evidence_faults=tuple(
                        evidence_faults
                    ),
                )

            quality_selections.append(
                ResearchQualitySelection(
                    underlying=underlying,
                    call_candidate_count=len(call_ranked),
                    put_candidate_count=len(put_ranked),
                    best_call=(
                        call_ranked[0]
                        if call_ranked
                        else None
                    ),
                    best_put=(
                        put_ranked[0]
                        if put_ranked
                        else None
                    ),
                )
            )

        signals = tuple(candidate_signals)

        # Exact frozen commit point:
        # nothing above this line may advance rolling state.
        for signal in signals:
            underlying = cast(
                DeltaUnderlying,
                signal.underlying,
            )
            self._previous_chains[
                underlying
            ] = signal.chain_state

        self._last_candle_close_ms = candle_close_ms

        return ResearchObserverCycle(
            candle_close_ms=candle_close_ms,
            evaluated=True,
            signals=signals,
            warnings=(),
            quality=tuple(quality_selections),
            evaluation_returns=tuple(
                evaluation_returns
            ),
            evidence_faults=tuple(evidence_faults),
            failure_stage=None,
        )

    @staticmethod
    def _scoring_failure(
        *,
        candle_close_ms: int,
        failure_stage: str,
        error: Exception,
        evaluation_returns: tuple[
            EvaluationReturn,
            ...,
        ],
        evidence_faults: tuple[str, ...],
    ) -> ResearchObserverCycle:
        return ResearchObserverCycle(
            candle_close_ms=candle_close_ms,
            evaluated=False,
            signals=(),
            warnings=(
                f"V3.4 scoring cycle failed: {error}",
            ),
            quality=(),
            evaluation_returns=evaluation_returns,
            evidence_faults=evidence_faults,
            failure_stage=failure_stage,
        )
