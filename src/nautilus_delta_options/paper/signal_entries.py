from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from nautilus_delta_options.delta.snapshot import DeltaMarketSnapshot
from nautilus_delta_options.paper.ledger import (
    PaperPosition,
)
from nautilus_delta_options.paper.portfolio_risk import (
    PortfolioRiskConfig,
    PortfolioRiskDecision,
    evaluate_portfolio_entry,
)
from nautilus_delta_options.paper.proposals import (
    PaperEntryProposal,
    PaperProposalConfig,
    build_ranked_paper_entry_proposals,
)
from nautilus_delta_options.paper.session import (
    PaperLedgerSession,
    PaperSignalAlreadyConsumedError,
)
from nautilus_delta_options.selection.entry_safety import signal_is_fresh
from nautilus_delta_options.signals.v31 import V31CallSignal


class PaperSignalEntryStatus(StrEnum):
    DISABLED = "disabled"
    WAIT = "wait"
    ALREADY_CONSUMED = "already_consumed"
    NO_MARKET_SNAPSHOT = "no_market_snapshot"
    NO_CALL_PROPOSAL = "no_call_proposal"
    RISK_REJECTED = "risk_rejected"
    STALE_SIGNAL = "stale_signal"
    OPENED = "opened"


@dataclass(frozen=True, slots=True)
class PaperSignalEntryResult:
    status: PaperSignalEntryStatus
    signal: V31CallSignal
    proposal: PaperEntryProposal | None
    position: PaperPosition | None
    risk_decisions: tuple[PortfolioRiskDecision, ...]


def process_v31_call_signal(
    signal: V31CallSignal,
    snapshots: Sequence[DeltaMarketSnapshot],
    *,
    session: PaperLedgerSession,
    entries_enabled: bool,
    proposal_config: PaperProposalConfig | None = None,
    portfolio_config: PortfolioRiskConfig | None = None,
    observed_ns: int | None = None,
) -> PaperSignalEntryResult:
    if not entries_enabled:
        return PaperSignalEntryResult(
            status=PaperSignalEntryStatus.DISABLED,
            signal=signal,
            proposal=None,
            position=None,
            risk_decisions=(),
        )

    if not signal.active:
        return PaperSignalEntryResult(
            status=PaperSignalEntryStatus.WAIT,
            signal=signal,
            proposal=None,
            position=None,
            risk_decisions=(),
        )

    if session.has_consumed_signal(signal.signal_key):
        return PaperSignalEntryResult(
            status=PaperSignalEntryStatus.ALREADY_CONSUMED,
            signal=signal,
            proposal=None,
            position=None,
            risk_decisions=(),
        )

    snapshot = next(
        (candidate for candidate in snapshots if candidate.underlying == signal.underlying),
        None,
    )

    if snapshot is None:
        return PaperSignalEntryResult(
            status=PaperSignalEntryStatus.NO_MARKET_SNAPSHOT,
            signal=signal,
            proposal=None,
            position=None,
            risk_decisions=(),
        )

    resolved_proposal_config = proposal_config or PaperProposalConfig()
    resolved_observed_ns = (
        observed_ns
        if observed_ns is not None
        else max(
            snapshot.captured_ns,
            signal.candle_close_ms * 1_000_000,
        )
    )
    if not signal_is_fresh(
        candle_close_ms=signal.candle_close_ms,
        observed_ns=resolved_observed_ns,
        config=resolved_proposal_config.entry_safety,
    ):
        return PaperSignalEntryResult(
            status=PaperSignalEntryStatus.STALE_SIGNAL,
            signal=signal,
            proposal=None,
            position=None,
            risk_decisions=(),
        )

    ledger_snapshot = session.snapshot()

    proposals = build_ranked_paper_entry_proposals(
        snapshot,
        ledger=ledger_snapshot,
        config=resolved_proposal_config,
        contract_type="call_options",
    )

    if not proposals:
        return PaperSignalEntryResult(
            status=PaperSignalEntryStatus.NO_CALL_PROPOSAL,
            signal=signal,
            proposal=None,
            position=None,
            risk_decisions=(),
        )

    risk_decisions: list[PortfolioRiskDecision] = []

    for proposal in proposals:
        risk = evaluate_portfolio_entry(
            ledger_snapshot,
            proposal,
            config=portfolio_config,
        )
        risk_decisions.append(risk)

        if not risk.approved:
            continue

        try:
            position = session.open_long_for_signal(
                proposal.record,
                signal_key=signal.signal_key,
                signal_underlying=signal.underlying,
                candle_closed_ns=signal.candle_close_ms * 1_000_000,
                contracts=proposal.sizing.contracts,
                stop_exit_bid=proposal.levels.stop_exit_bid,
                target_exit_bid=proposal.levels.target_exit_bid,
                stop_spot=proposal.levels.stop_spot,
                target_spot=proposal.levels.target_spot,
            )
        except PaperSignalAlreadyConsumedError:
            return PaperSignalEntryResult(
                status=PaperSignalEntryStatus.ALREADY_CONSUMED,
                signal=signal,
                proposal=None,
                position=None,
                risk_decisions=tuple(risk_decisions),
            )

        return PaperSignalEntryResult(
            status=PaperSignalEntryStatus.OPENED,
            signal=signal,
            proposal=proposal,
            position=position,
            risk_decisions=tuple(risk_decisions),
        )

    return PaperSignalEntryResult(
        status=PaperSignalEntryStatus.RISK_REJECTED,
        signal=signal,
        proposal=proposals[0],
        position=None,
        risk_decisions=tuple(risk_decisions),
    )


def process_v31_call_signals(
    signals: Sequence[V31CallSignal],
    snapshots: Sequence[DeltaMarketSnapshot],
    *,
    session: PaperLedgerSession,
    entries_enabled: bool,
    proposal_config: PaperProposalConfig | None = None,
    portfolio_config: PortfolioRiskConfig | None = None,
    observed_ns: int | None = None,
) -> tuple[PaperSignalEntryResult, ...]:
    return tuple(
        process_v31_call_signal(
            signal,
            snapshots,
            session=session,
            entries_enabled=entries_enabled,
            proposal_config=proposal_config,
            portfolio_config=portfolio_config,
            observed_ns=observed_ns,
        )
        for signal in signals
    )
