from __future__ import annotations

from decimal import Decimal
from typing import Self

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


class PaperLedgerSession:
    """Coordinates paper-ledger mutations with durable persistence."""

    def __init__(
        self,
        ledger: PaperLedger,
        store: SQLitePaperLedgerStore,
    ) -> None:
        self._ledger = ledger
        self._store = store

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
    def ledger(self) -> PaperLedger:
        return self._ledger

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
        position = self._ledger.open_long(
            record,
            contracts=contracts,
            stop_exit_bid=stop_exit_bid,
            target_exit_bid=target_exit_bid,
            stop_spot=stop_spot,
            target_spot=target_spot,
        )
        self._store.save(self._ledger)
        return position

    def process_exit(
        self,
        trade_id: int,
        record: DeltaOptionMarketRecord,
    ) -> PaperClosedTrade | None:
        closed_trade = self._ledger.process_exit(
            trade_id,
            record,
        )

        if closed_trade is not None:
            self._store.save(self._ledger)

        return closed_trade

    def close_long(
        self,
        trade_id: int,
        record: DeltaOptionMarketRecord,
        *,
        reason: ExitReason = ExitReason.MANUAL,
    ) -> PaperClosedTrade:
        closed_trade = self._ledger.close_long(
            trade_id,
            record,
            reason=reason,
        )
        self._store.save(self._ledger)
        return closed_trade


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
