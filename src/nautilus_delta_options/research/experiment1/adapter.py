from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from nautilus_delta_options.research.experiment1.attempts import (
    record_evaluation_attempt_failed,
    record_evaluation_attempt_started,
)
from nautilus_delta_options.research.experiment1.models import (
    EvaluationSlot,
)
from nautilus_delta_options.research.experiment1.ownership import (
    CollectorLease,
)
from nautilus_delta_options.research.experiment1.state_machine import (
    EvaluationAttemptContext,
)

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

        self._lease = lease
        self._experiment_id = experiment_id
        self._run_id = run_id
        self._utc_ns = utc_ns
        self._attempt_id_factory = (
            attempt_id_factory
            or (lambda: uuid.uuid4().hex)
        )

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
