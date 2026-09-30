from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from nautilus_delta_options.research.experiment1.attempts import (
    record_evaluation_attempt_failed,
    record_evaluation_attempt_started,
)
from nautilus_delta_options.research.experiment1.journal import (
    ExperimentJournal,
)
from nautilus_delta_options.research.experiment1.market_data import (
    Experiment1CapturedMarketData,
)
from nautilus_delta_options.research.experiment1.models import (
    EvaluationSlot,
)
from nautilus_delta_options.research.experiment1.ownership import (
    CollectorLease,
)
from nautilus_delta_options.research.experiment1.service import (
    enqueue_completed_evaluation,
)
from nautilus_delta_options.research.experiment1.state_machine import (
    EvaluationAttemptContext,
    EvaluationReturn,
)
from nautilus_delta_options.signals.v34 import V34Config

_NS_PER_MS = 1_000_000


@dataclass(frozen=True, slots=True)
class AttemptBinding:
    attempt_id: str
    slot: EvaluationSlot


class Experiment1EvaluationAdapter:
    """Bind evaluator lifecycle hooks to immutable Experiment 1 slots."""

    def __init__(
        self,
        *,
        lease: CollectorLease,
        experiment_id: str,
        run_id: str,
        slots: Sequence[EvaluationSlot],
        utc_ns: Callable[[], int],
        attempt_id_factory: Callable[[], str] | None = None,
        journal: ExperimentJournal | None = None,
        market_data: Experiment1CapturedMarketData | None = None,
        config: V34Config | None = None,
    ) -> None:
        if not experiment_id:
            raise ValueError(
                "experiment_id must be non-empty"
            )

        if not run_id:
            raise ValueError(
                "run_id must be non-empty"
            )

        if not lease.is_held:
            raise RuntimeError(
                "collector lease must be held"
            )

        if not slots:
            raise ValueError(
                "slots must be non-empty"
            )

        evidence_parts = (
            journal,
            market_data,
            config,
        )

        if (
            any(part is None for part in evidence_parts)
            and not all(
                part is None
                for part in evidence_parts
            )
        ):
            raise ValueError(
                "journal, market_data, and config must be "
                "configured together"
            )

        self._lease = lease
        self._experiment_id = experiment_id
        self._run_id = run_id
        self._utc_ns = utc_ns
        self._attempt_id_factory = (
            attempt_id_factory
            or (lambda: uuid.uuid4().hex)
        )
        self._journal = journal
        self._market_data = market_data
        self._config = config

        slot_by_key: dict[
            tuple[str, int],
            EvaluationSlot,
        ] = {}

        for slot in slots:
            if slot.experiment_id != experiment_id:
                raise ValueError(
                    "slot experiment does not match adapter experiment"
                )

            key = (
                slot.asset,
                slot.candle_close_boundary_ns,
            )

            if key in slot_by_key:
                raise ValueError(
                    "duplicate asset/boundary schedule slot"
                )

            slot_by_key[key] = slot

        self._slot_by_key = slot_by_key

        self._active_attempts: dict[
            str,
            AttemptBinding,
        ] = {}

    def slot_for_context(
        self,
        context: EvaluationAttemptContext,
    ) -> EvaluationSlot:
        close_boundary_ns = (
            context.candles.candle_close_ms + 1
        ) * _NS_PER_MS

        key = (
            context.underlying,
            close_boundary_ns,
        )

        try:
            slot = self._slot_by_key[key]
        except KeyError as error:
            raise RuntimeError(
                "evaluator context has no scheduled Experiment 1 slot"
            ) from error

        if (
            context.candles.underlying
            != context.underlying
        ):
            raise ValueError(
                "context candle underlying does not match"
            )

        if (
            context.source_option_chain.underlying
            != context.underlying
        ):
            raise ValueError(
                "context source option chain underlying does not match"
            )

        if (
            context.evaluated_option_chain.underlying
            != context.underlying
        ):
            raise ValueError(
                "context evaluated option chain underlying does not match"
            )

        return slot

    def on_attempt_start(
        self,
        context: EvaluationAttemptContext,
    ) -> str:
        slot = self.slot_for_context(context)

        attempt_id = self._attempt_id_factory()

        if not attempt_id:
            raise RuntimeError(
                "attempt_id factory returned an empty value"
            )

        if attempt_id in self._active_attempts:
            raise RuntimeError(
                "attempt_id factory returned a duplicate value"
            )

        event_time_utc_ns = self._utc_ns()

        if event_time_utc_ns <= 0:
            raise RuntimeError(
                "attempt start clock must be positive"
            )

        record_evaluation_attempt_started(
            self._lease,
            experiment_id=self._experiment_id,
            run_id=self._run_id,
            slot=slot,
            attempt_id=attempt_id,
            event_time_utc_ns=event_time_utc_ns,
        )

        self._active_attempts[attempt_id] = AttemptBinding(
            attempt_id=attempt_id,
            slot=slot,
        )

        return attempt_id

    def on_attempt_failure(
        self,
        context: EvaluationAttemptContext,
        attempt_id: str,
        error: Exception,
    ) -> bool:
        binding = self._active_attempts.get(
            attempt_id
        )

        if binding is None:
            raise RuntimeError(
                "evaluation failure references unknown attempt"
            )

        expected_slot = self.slot_for_context(
            context
        )

        if binding.slot != expected_slot:
            raise RuntimeError(
                "evaluation failure slot differs from attempt start"
            )

        event_time_utc_ns = self._utc_ns()

        if event_time_utc_ns <= 0:
            raise RuntimeError(
                "attempt failure clock must be positive"
            )

        record_evaluation_attempt_failed(
            self._lease,
            experiment_id=self._experiment_id,
            run_id=self._run_id,
            slot=binding.slot,
            attempt_id=attempt_id,
            reason=(
                f"{type(error).__name__}: {error}"
            ),
            event_time_utc_ns=event_time_utc_ns,
        )

        del self._active_attempts[attempt_id]

        return True

    def on_evaluation_return(
        self,
        result: EvaluationReturn,
    ) -> bool:
        """Queue exact completed evidence for one evaluator return."""

        attempt_id = result.attempt_id

        # No durable evaluation_started event means this result
        # cannot be admitted into durable evaluation evidence.
        if attempt_id is None:
            return False

        journal = self._journal
        market_data = self._market_data
        config = self._config

        if (
            journal is None
            or market_data is None
            or config is None
        ):
            raise RuntimeError(
                "completed evaluation evidence is not configured"
            )

        binding = self.binding_for_attempt(
            attempt_id
        )

        if binding.slot.asset != result.underlying:
            raise RuntimeError(
                "evaluation return underlying differs from attempt"
            )

        expected_boundary_ns = (
            result.signal.candle_close_ms + 1
        ) * _NS_PER_MS

        if (
            binding.slot.candle_close_boundary_ns
            != expected_boundary_ns
        ):
            raise RuntimeError(
                "evaluation return boundary differs from attempt"
            )

        history = market_data.captured_history(
            result.underlying
        )
        option_chain = market_data.captured_option_chain(
            result.underlying
        )

        # The evidence wrapper must be the exact capture whose parsed
        # snapshot was handed to the evaluator, never a reconstruction.
        if history.snapshot is not result.candles:
            raise RuntimeError(
                "evaluation history is not the retained raw capture"
            )

        if (
            option_chain.snapshot
            is not result.source_option_chain
        ):
            raise RuntimeError(
                "evaluation option chain is not the retained raw capture"
            )

        event_time_utc_ns = self._utc_ns()

        if event_time_utc_ns <= 0:
            raise RuntimeError(
                "completed evaluation clock must be positive"
            )

        refs = enqueue_completed_evaluation(
            journal,
            experiment_id=self._experiment_id,
            run_id=self._run_id,
            slot=binding.slot,
            attempt_id=attempt_id,
            history=history,
            option_chain=option_chain,
            evaluated_option_chain=(
                result.evaluated_option_chain
            ),
            signal=result.signal,
            population_status=result.population_status,
            retry_ordinal=result.retry_ordinal,
            previous_chain=result.previous_chain,
            config=config,
            as_of=result.as_of,
            event_time_utc_ns=event_time_utc_ns,
            canonical_decision=result.canonical_decision,
            canonical_captured_ns=(
                result.canonical_captured_ns
            ),
        )

        if refs is None:
            # Keep the durable start unresolved. If the process dies,
            # startup recovery will conservatively seal the slot.
            return False

        # Queue acceptance is not SQLite durability. Removing the
        # in-memory binding is still safe because a writer failure
        # leaves the durable evaluation_started event unresolved,
        # which startup recovery detects.
        self.complete_attempt(
            attempt_id
        )

        return True

    def binding_for_attempt(
        self,
        attempt_id: str,
    ) -> AttemptBinding:
        try:
            return self._active_attempts[
                attempt_id
            ]
        except KeyError as error:
            raise RuntimeError(
                "unknown active evaluation attempt"
            ) from error

    def complete_attempt(
        self,
        attempt_id: str,
    ) -> AttemptBinding:
        """Forget an attempt only after completed evidence is accepted."""

        try:
            return self._active_attempts.pop(
                attempt_id
            )
        except KeyError as error:
            raise RuntimeError(
                "unknown active evaluation attempt"
            ) from error
