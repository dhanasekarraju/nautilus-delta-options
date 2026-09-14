from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import Decimal
from threading import RLock
from typing import Self

from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.snapshot import (
    DeltaOptionMarketRecord,
)
from nautilus_delta_options.paper.ledger import (
    ExitReason,
    PaperClosedTrade,
    PaperLedger,
    PaperPosition,
)
from nautilus_delta_options.paper.persistence import (
    SQLitePaperLedgerStore,
)


class PaperLedgerConfigurationError(ValueError):
    """Raised when runtime settings differ from persisted settings."""


class PaperSignalAlreadyConsumedError(ValueError):
    """Raised when a paper signal already produced an entry."""


class PaperLedgerSession:
    """Coordinates paper-ledger mutations with durable persistence."""

    def __init__(
        self,
        ledger: PaperLedger,
        store: SQLitePaperLedgerStore,
    ) -> None:
        self._ledger = ledger
        self._store = store
        self._lock = RLock()
        self._revision = store.revision

    @classmethod
    def load_or_create(
        cls,
        store: SQLitePaperLedgerStore,
        *,
        initial_cash: Decimal,
        minimum_reward_risk: Decimal,
        max_positions: int,
        gst_rate: Decimal | None = None,
    ) -> Self:
        configured = PaperLedger(
            initial_cash=initial_cash,
            minimum_reward_risk=minimum_reward_risk,
            max_positions=max_positions,
            gst_rate=gst_rate,
        )
        persisted = store.load()

        if persisted is None:
            store.save(configured)
            return cls(configured, store)

        _validate_configuration(
            persisted=persisted,
            configured=configured,
        )
        return cls(persisted, store)

    @property
    def revision(self) -> int | None:
        with self._lock:
            return self._revision

    @property
    def ledger(self) -> PaperLedger:
        return self.snapshot()

    def snapshot(self) -> PaperLedger:
        with self._lock:
            return _clone_ledger(self._ledger)

    def open_long(
        self,
        record: DeltaOptionMarketRecord,
        *,
        contracts: Decimal,
        stop_exit_bid: Decimal,
        target_exit_bid: Decimal,
        stop_spot: Decimal,
        target_spot: Decimal,
    ) -> PaperPosition:
        with self._lock:
            candidate = _clone_ledger(self._ledger)
            position = candidate.open_long(
                record,
                contracts=contracts,
                stop_exit_bid=stop_exit_bid,
                target_exit_bid=target_exit_bid,
                stop_spot=stop_spot,
                target_spot=target_spot,
                provenance=self._store.run_provenance,
            )
            self._commit(candidate)
            return position

    def has_consumed_signal(self, signal_key: str) -> bool:
        with self._lock:
            return self._store.has_consumed_signal(signal_key)

    def open_long_for_signal(
        self,
        record: DeltaOptionMarketRecord,
        *,
        signal_key: str,
        signal_underlying: str,
        candle_closed_ns: int,
        contracts: Decimal,
        stop_exit_bid: Decimal,
        target_exit_bid: Decimal,
        stop_spot: Decimal,
        target_spot: Decimal,
        admission: Callable[[PaperLedger], None] | None = None,
        readiness_guard: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> PaperPosition:
        with self._lock:
            if record.ticker.underlying != signal_underlying:
                raise ValueError("Signal underlying does not match the market record")
            if self._store.has_consumed_signal(signal_key):
                raise PaperSignalAlreadyConsumedError(f"Signal already consumed: {signal_key}")

            candidate = _clone_ledger(self._ledger)
            position = candidate.open_long(
                record,
                contracts=contracts,
                stop_exit_bid=stop_exit_bid,
                target_exit_bid=target_exit_bid,
                stop_spot=stop_spot,
                target_spot=target_spot,
                provenance=self._store.run_provenance,
            )
            receipt = self._store.save_with_signal(
                candidate,
                signal_key=signal_key,
                underlying=signal_underlying,
                candle_closed_ns=candle_closed_ns,
                trade_id=position.trade_id,
                expected_revision=self._revision,
                before_commit=(lambda: admission(_clone_ledger(self._ledger)))
                if admission is not None else None,
                readiness_guard=readiness_guard,
            )

            if receipt is None:
                raise PaperSignalAlreadyConsumedError(f"Signal already consumed: {signal_key}")

            self._ledger = candidate
            self._revision = self._store.revision
            return position

    def process_exit(
        self,
        trade_id: int,
        record: DeltaOptionMarketRecord,
    ) -> PaperClosedTrade | None:
        with self._lock:
            candidate = _clone_ledger(self._ledger)
            closed_trade = candidate.process_exit(
                trade_id,
                record,
            )

            self._commit(candidate)

            return closed_trade

    def process_exit_ticker(
        self,
        trade_id: int,
        ticker: DeltaOptionTicker,
        *,
        observed_ns: int | None = None,
        apply_time_policy: bool = False,
    ) -> PaperClosedTrade | None:
        with self._lock:
            candidate = _clone_ledger(self._ledger)
            closed_trade = candidate.process_exit_ticker(
                trade_id,
                ticker,
                observed_ns=observed_ns,
                apply_time_policy=apply_time_policy,
            )

            self._commit(candidate)

            return closed_trade

    def close_long(
        self,
        trade_id: int,
        record: DeltaOptionMarketRecord,
        *,
        reason: ExitReason = ExitReason.MANUAL,
    ) -> PaperClosedTrade:
        with self._lock:
            candidate = _clone_ledger(self._ledger)
            closed_trade = candidate.close_long(
                trade_id,
                record,
                reason=reason,
            )
            self._commit(candidate)
            return closed_trade

    def note_unresolved(self, trade_id: int, reason: str) -> None:
        with self._lock:
            candidate = _clone_ledger(self._ledger)
            candidate.note_unresolved(trade_id, reason)
            self._commit(candidate)

    def settle_long(
        self,
        trade_id: int,
        *,
        settlement_spot: Decimal,
        settlement_fee: Decimal,
        observed_ns: int,
        reference: str,
    ) -> PaperClosedTrade:
        with self._lock:
            candidate = _clone_ledger(self._ledger)
            trade = candidate.settle_long(
                trade_id,
                settlement_spot=settlement_spot,
                settlement_fee=settlement_fee,
                observed_ns=observed_ns,
                reference=reference,
            )
            self._commit(candidate)
            return trade

    def _commit(self, candidate: PaperLedger) -> None:
        self._store.save(candidate, expected_revision=self._revision)
        self._ledger = candidate
        self._revision = self._store.revision


def _clone_ledger(ledger: PaperLedger) -> PaperLedger:
    return PaperLedger.from_state(
        initial_cash=ledger.initial_cash,
        cash=ledger.cash,
        minimum_reward_risk=ledger.minimum_reward_risk,
        max_positions=ledger.max_positions,
        gst_rate=ledger.gst_rate,
        next_trade_id=ledger.next_trade_id,
        open_positions=ledger.open_positions,
        closed_trades=ledger.closed_trades,
    )


def _validate_configuration(
    *,
    persisted: PaperLedger,
    configured: PaperLedger,
) -> None:
    comparisons = (
        (
            "initial_cash",
            persisted.initial_cash,
            configured.initial_cash,
        ),
        (
            "minimum_reward_risk",
            persisted.minimum_reward_risk,
            configured.minimum_reward_risk,
        ),
        (
            "max_positions",
            persisted.max_positions,
            configured.max_positions,
        ),
        (
            "gst_rate",
            persisted.gst_rate,
            configured.gst_rate,
        ),
    )
    mismatches = [
        name
        for name, persisted_value, configured_value in comparisons
        if persisted_value != configured_value
    ]

    if mismatches:
        fields = ", ".join(mismatches)
        raise PaperLedgerConfigurationError(
            f"Runtime configuration differs from persisted ledger: {fields}"
        )
