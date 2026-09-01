from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol

from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaOptionProductsSnapshot,
    DeltaUnderlying,
)
from nautilus_delta_options.delta.snapshot import (
    DeltaMarketSnapshot,
    build_market_snapshot,
)
from nautilus_delta_options.paper.exit_policy import (
    PaperExitPolicyConfig,
    paper_time_exit_due,
)
from nautilus_delta_options.paper.ledger import ExitReason, PaperClosedTrade
from nautilus_delta_options.paper.proposals import (
    PaperEntryProposal,
    PaperProposalConfig,
    build_ranked_paper_entry_proposals,
)
from nautilus_delta_options.paper.session import PaperLedgerSession
from nautilus_delta_options.paper.signal_entries import (
    PaperSignalEntryResult,
    process_v31_call_signals,
)
from nautilus_delta_options.selection.eligibility import (
    EligibilityConfig,
)
from nautilus_delta_options.signals.binance import BinanceCandleSnapshot
from nautilus_delta_options.signals.v31 import (
    V31CallSignal,
    evaluate_v31_call_signal,
)


class DeltaPublicMarketData(Protocol):
    def fetch_option_chain(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionChainSnapshot: ...

    def fetch_option_products(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionProductsSnapshot: ...


class V31PublicMarketData(Protocol):
    def fetch_v31_candles(
        self,
        underlying: DeltaUnderlying,
    ) -> BinanceCandleSnapshot: ...


@dataclass(frozen=True, slots=True)
class PaperDryRunCycle:
    captured_ns: int
    as_of: date
    snapshots: tuple[DeltaMarketSnapshot, ...]
    closed_trades: tuple[PaperClosedTrade, ...]
    proposals: tuple[PaperEntryProposal, ...]
    warnings: tuple[str, ...]
    signals: tuple[V31CallSignal, ...] = ()
    entry_results: tuple[PaperSignalEntryResult, ...] = ()
    entries_enabled: bool = False
    signal_error: str | None = None


class PaperDryRunObserver:
    """Runs paper market cycles, exits, and explicitly enabled signal entries."""

    def __init__(
        self,
        *,
        client: DeltaPublicMarketData,
        session: PaperLedgerSession,
        underlyings: Sequence[DeltaUnderlying] = ("BTC", "ETH"),
        eligibility_config: EligibilityConfig | None = None,
        proposal_config: PaperProposalConfig | None = None,
        signal_client: V31PublicMarketData | None = None,
        entries_enabled: bool = False,
        exit_policy_config: PaperExitPolicyConfig | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not underlyings:
            raise ValueError("At least one underlying is required")
        if len(underlyings) != len(set(underlyings)):
            raise ValueError("Underlyings must be unique")

        self._client = client
        self._session = session
        self._underlyings = tuple(underlyings)
        self._eligibility_config = eligibility_config or EligibilityConfig()
        self._proposal_config = proposal_config or PaperProposalConfig()
        self._exit_policy_config = exit_policy_config or PaperExitPolicyConfig()
        self._clock_ns = clock_ns
        if entries_enabled and signal_client is None:
            raise ValueError("A signal client is required when paper entries are enabled")

        self._signal_client = signal_client
        self._entries_enabled = entries_enabled

    @property
    def entries_enabled(self) -> bool:
        return self._entries_enabled

    def run_cycle(
        self,
        *,
        as_of: date | None = None,
    ) -> PaperDryRunCycle:
        cycle_date = as_of or datetime.now(UTC).date()
        snapshots = tuple(
            self._fetch_snapshot(
                underlying,
                as_of=cycle_date,
            )
            for underlying in self._underlyings
        )
        captured_ns = max(snapshot.captured_ns for snapshot in snapshots)
        warnings = _snapshot_warnings(snapshots)
        records_by_product = {
            record.product.product_id: record
            for snapshot in snapshots
            for record in snapshot.records
        }

        closed_trades: list[PaperClosedTrade] = []

        ledger_snapshot = self._session.snapshot()

        for position in ledger_snapshot.open_positions:
            record = records_by_product.get(position.product_id)

            if record is None:
                warnings.append(f"{position.symbol}: current market record missing")
                continue
            if record.quote is None:
                warnings.append(f"{position.symbol}: current tradeable quote missing")
                continue

            closed: PaperClosedTrade | None

            try:
                if paper_time_exit_due(
                    position,
                    record,
                    observed_ns=captured_ns,
                    config=self._exit_policy_config,
                ):
                    closed = self._session.close_long(
                        position.trade_id,
                        record,
                        reason=ExitReason.TIME,
                    )
                else:
                    closed = self._session.process_exit(
                        position.trade_id,
                        record,
                    )
            except ValueError as error:
                warnings.append(f"{position.symbol}: exit deferred: {error}")
                continue

            if closed is not None:
                closed_trades.append(closed)

        proposals = _rank_global_proposals(
            snapshots,
            session=self._session,
            config=self._proposal_config,
            excluded_product_ids=frozenset(trade.position.product_id for trade in closed_trades),
        )

        signals: tuple[V31CallSignal, ...] = ()
        entry_results: tuple[PaperSignalEntryResult, ...] = ()
        signal_error: str | None = None

        if self._signal_client is not None:
            try:
                signals = tuple(
                    evaluate_v31_call_signal(self._signal_client.fetch_v31_candles(underlying))
                    for underlying in self._underlyings
                )
                entry_results = process_v31_call_signals(
                    signals,
                    snapshots,
                    session=self._session,
                    entries_enabled=self._entries_enabled,
                    proposal_config=self._proposal_config,
                    observed_ns=self._clock_ns(),
                )
            except Exception as error:
                signal_error = str(error)
                warnings.append(f"V3.1 signal processing failed: {error}")

        return PaperDryRunCycle(
            captured_ns=captured_ns,
            as_of=cycle_date,
            snapshots=snapshots,
            closed_trades=tuple(closed_trades),
            proposals=proposals,
            warnings=tuple(warnings),
            signals=signals,
            entry_results=entry_results,
            entries_enabled=self._entries_enabled,
            signal_error=signal_error,
        )

    def _fetch_snapshot(
        self,
        underlying: DeltaUnderlying,
        *,
        as_of: date,
    ) -> DeltaMarketSnapshot:
        catalog = self._client.fetch_option_products(underlying)
        chain = self._client.fetch_option_chain(underlying)
        received_ns = self._clock_ns()
        latest_event_ns = max(
            (ticker.exchange_timestamp * 1_000 for ticker in chain.tickers),
            default=received_ns,
        )
        max_future_skew_ns = int(
            self._proposal_config.entry_safety.max_future_skew_seconds * Decimal("1000000000")
        )
        if latest_event_ns > received_ns + max_future_skew_ns:
            raise ValueError(
                f"{underlying}: Delta timestamps are ahead of the VPS clock; check NTP"
            )
        captured_ns = max(received_ns, latest_event_ns)

        return build_market_snapshot(
            catalog=catalog,
            chain=chain,
            as_of=as_of,
            captured_ns=captured_ns,
            eligibility_config=self._eligibility_config,
        )


def _rank_global_proposals(
    snapshots: tuple[DeltaMarketSnapshot, ...],
    *,
    session: PaperLedgerSession,
    config: PaperProposalConfig,
    excluded_product_ids: frozenset[int],
) -> tuple[PaperEntryProposal, ...]:
    ledger_snapshot = session.snapshot()
    available_slots = (
        ledger_snapshot.max_positions
        - len(ledger_snapshot.open_positions)
    )

    if available_slots <= 0:
        return ()

    proposals = [
        proposal
        for snapshot in snapshots
        for proposal in build_ranked_paper_entry_proposals(
            snapshot,
            ledger=ledger_snapshot,
            config=config,
        )
    ]
    proposals = [
        proposal
        for proposal in proposals
        if proposal.record.product.product_id not in excluded_product_ids
    ]

    proposals.sort(key=_proposal_quality_key)

    limit = min(
        config.max_proposals,
        available_slots,
    )
    return tuple(proposals[:limit])


def _proposal_quality_key(
    proposal: PaperEntryProposal,
) -> tuple[Decimal, Decimal, Decimal, str]:
    ticker = proposal.record.ticker
    spread = ticker.spread_fraction
    volume = ticker.volume or Decimal("0")

    return (
        spread if spread is not None else Decimal("Infinity"),
        -ticker.open_interest_contracts,
        -volume,
        ticker.symbol,
    )


def _snapshot_warnings(
    snapshots: tuple[DeltaMarketSnapshot, ...],
) -> list[str]:
    warnings: list[str] = []

    for snapshot in snapshots:
        warnings.extend(f"{snapshot.underlying}: {error}" for error in snapshot.errors)

        if snapshot.unmatched_product_ids:
            warnings.append(
                f"{snapshot.underlying}: {len(snapshot.unmatched_product_ids)} unmatched products"
            )

    return warnings
