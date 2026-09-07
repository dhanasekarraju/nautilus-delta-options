from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from nautilus_delta_options.delta.models import (
    DeltaOptionContractType,
)
from nautilus_delta_options.delta.snapshot import (
    DeltaMarketSnapshot,
)
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
    paper_proposal_quality_key,
)
from nautilus_delta_options.paper.session import (
    PaperLedgerSession,
    PaperSignalAlreadyConsumedError,
)
from nautilus_delta_options.selection.entry_safety import (
    signal_is_fresh,
)
from nautilus_delta_options.signals.v32 import (
    V32Signal,
    V32SignalDecision,
)


class PaperSignalEntryStatus(StrEnum):
    DISABLED = "disabled"
    WAIT = "wait"
    ALREADY_CONSUMED = "already_consumed"
    NO_MARKET_SNAPSHOT = "no_market_snapshot"
    NO_CALL_PROPOSAL = "no_call_proposal"
    NO_PUT_PROPOSAL = "no_put_proposal"
    RISK_REJECTED = "risk_rejected"
    REVALIDATION_REJECTED = "revalidation_rejected"
    STALE_SIGNAL = "stale_signal"
    DIRECTION_BLOCKED = "direction_blocked"
    CORRELATED_SIGNAL_SKIPPED = "correlated_signal_skipped"
    OPENED = "opened"


@dataclass(frozen=True, slots=True)
class PaperSignalEntryResult:
    status: PaperSignalEntryStatus
    signal: V32Signal
    proposal: PaperEntryProposal | None
    position: PaperPosition | None
    risk_decisions: tuple[
        PortfolioRiskDecision,
        ...,
    ]


type CandidateRevalidator = Callable[
    [V32Signal, PaperEntryProposal],
    PaperEntryProposal | None,
]


@dataclass(frozen=True, slots=True)
class _PreparedCandidate:
    index: int
    signal: V32Signal
    proposal: PaperEntryProposal
    risk_decisions: tuple[
        PortfolioRiskDecision,
        ...,
    ]


def process_v32_signal(
    signal: V32Signal,
    snapshots: Sequence[DeltaMarketSnapshot],
    *,
    session: PaperLedgerSession,
    entries_enabled: bool,
    proposal_config: PaperProposalConfig | None = None,
    portfolio_config: PortfolioRiskConfig | None = None,
    observed_ns: int | None = None,
    candidate_revalidator: CandidateRevalidator | None = None,
) -> PaperSignalEntryResult:
    return process_v32_signals(
        (signal,),
        snapshots,
        session=session,
        entries_enabled=entries_enabled,
        proposal_config=proposal_config,
        portfolio_config=portfolio_config,
        observed_ns=observed_ns,
        candidate_revalidator=candidate_revalidator,
    )[0]


def process_v32_signals(
    signals: Sequence[V32Signal],
    snapshots: Sequence[DeltaMarketSnapshot],
    *,
    session: PaperLedgerSession,
    entries_enabled: bool,
    proposal_config: PaperProposalConfig | None = None,
    portfolio_config: PortfolioRiskConfig | None = None,
    observed_ns: int | None = None,
    candidate_revalidator: CandidateRevalidator | None = None,
) -> tuple[PaperSignalEntryResult, ...]:
    results: list[PaperSignalEntryResult | None] = [None] * len(signals)

    if not entries_enabled:
        return tuple(
            _result(
                PaperSignalEntryStatus.DISABLED,
                signal,
            )
            for signal in signals
        )

    # Signals are grouped by completed candle + direction.
    # BTC and ETH therefore compete against each other
    # before either is allowed to open.
    groups: dict[
        tuple[int, V32SignalDecision],
        list[tuple[int, V32Signal]],
    ] = {}

    for index, signal in enumerate(signals):
        if not signal.active:
            results[index] = _result(
                PaperSignalEntryStatus.WAIT,
                signal,
            )
            continue

        if session.has_consumed_signal(signal.episode_key):
            results[index] = _result(
                PaperSignalEntryStatus.ALREADY_CONSUMED,
                signal,
            )
            continue

        groups.setdefault(
            (
                signal.candle_close_ms,
                signal.decision,
            ),
            [],
        ).append((index, signal))

    for group in groups.values():
        contract_type = _contract_type_for(group[0][1])

        # A CALL already open means no additional
        # BTC/ETH CALL. Same for PUT.
        if _has_directional_position(
            session.snapshot().open_positions,
            contract_type,
        ):
            for index, signal in group:
                if results[index] is None:
                    results[index] = _result(
                        PaperSignalEntryStatus.DIRECTION_BLOCKED,
                        signal,
                    )
            continue

        candidates: list[_PreparedCandidate] = []

        for index, signal in group:
            prepared = _prepare_candidate(
                index=index,
                signal=signal,
                snapshots=snapshots,
                session=session,
                proposal_config=proposal_config,
                portfolio_config=portfolio_config,
                observed_ns=observed_ns,
            )

            if isinstance(
                prepared,
                PaperSignalEntryResult,
            ):
                results[index] = prepared
            else:
                candidates.append(prepared)

        # Reuse the same execution-quality ordering
        # already used elsewhere:
        # spread -> OI -> volume -> symbol.
        candidates.sort(key=lambda candidate: paper_proposal_quality_key(candidate.proposal))

        winner_index: int | None = None

        for candidate in candidates:
            signal = candidate.signal

            # A concurrent/repeated processing attempt may
            # have consumed the shared episode key.
            if session.has_consumed_signal(signal.episode_key):
                for unresolved in candidates:
                    if results[unresolved.index] is None:
                        results[unresolved.index] = _result(
                            PaperSignalEntryStatus.ALREADY_CONSUMED,
                            unresolved.signal,
                            proposal=(unresolved.proposal),
                            risk_decisions=(unresolved.risk_decisions),
                        )
                break

            ledger_snapshot = session.snapshot()

            if _has_directional_position(
                ledger_snapshot.open_positions,
                contract_type,
            ):
                for unresolved in candidates:
                    if results[unresolved.index] is None:
                        results[unresolved.index] = _result(
                            PaperSignalEntryStatus.DIRECTION_BLOCKED,
                            unresolved.signal,
                            proposal=(unresolved.proposal),
                            risk_decisions=(unresolved.risk_decisions),
                        )
                break

            proposal = candidate.proposal

            # Fast-entry may optionally replace the originally
            # ranked proposal with one rebuilt from a targeted
            # fresh quote immediately before mutation.
            #
            # The callback returns None when the refreshed
            # contract no longer satisfies the existing
            # payoff / safety / sizing requirements. In that
            # case the next ranked candidate may be tried.
            if candidate_revalidator is not None:
                refreshed = candidate_revalidator(
                    signal,
                    proposal,
                )

                if refreshed is None:
                    results[candidate.index] = _result(
                        PaperSignalEntryStatus.REVALIDATION_REJECTED,
                        signal,
                        proposal=proposal,
                        risk_decisions=(
                            candidate.risk_decisions
                        ),
                    )
                    continue

                proposal = refreshed

            # Re-snapshot after optional revalidation so the
            # final portfolio gate uses synchronized ledger
            # state immediately before mutation.
            ledger_snapshot = session.snapshot()

            if _has_directional_position(
                ledger_snapshot.open_positions,
                contract_type,
            ):
                for unresolved in candidates:
                    if results[unresolved.index] is None:
                        results[unresolved.index] = _result(
                            PaperSignalEntryStatus.DIRECTION_BLOCKED,
                            unresolved.signal,
                            proposal=(unresolved.proposal),
                            risk_decisions=(unresolved.risk_decisions),
                        )
                break

            current_risk = evaluate_portfolio_entry(
                ledger_snapshot,
                proposal,
                config=portfolio_config,
            )

            risk_decisions = (
                candidate.risk_decisions
                + (current_risk,)
            )

            if not current_risk.approved:
                results[candidate.index] = _result(
                    PaperSignalEntryStatus.RISK_REJECTED,
                    signal,
                    proposal=proposal,
                    risk_decisions=risk_decisions,
                )
                continue

            try:
                position = session.open_long_for_signal(
                    proposal.record,
                    signal_key=(signal.episode_key),
                    signal_underlying=(signal.underlying),
                    candle_closed_ns=(
                        signal.candle_close_ms
                        * 1_000_000
                    ),
                    contracts=(
                        proposal.sizing.contracts
                    ),
                    stop_exit_bid=(
                        proposal.levels.stop_exit_bid
                    ),
                    target_exit_bid=(
                        proposal.levels.target_exit_bid
                    ),
                    stop_spot=(
                        proposal.levels.stop_spot
                    ),
                    target_spot=(
                        proposal.levels.target_spot
                    ),
                )
            except PaperSignalAlreadyConsumedError:
                for unresolved in candidates:
                    if results[unresolved.index] is None:
                        results[unresolved.index] = _result(
                            PaperSignalEntryStatus.ALREADY_CONSUMED,
                            unresolved.signal,
                            proposal=(unresolved.proposal),
                            risk_decisions=(unresolved.risk_decisions),
                        )
                break

            results[candidate.index] = _result(
                PaperSignalEntryStatus.OPENED,
                signal,
                proposal=proposal,
                position=position,
                risk_decisions=risk_decisions,
            )

            winner_index = candidate.index
            break

        # Once the best approved opportunity opens,
        # every other BTC/ETH signal from this candle
        # and direction is intentionally skipped.
        if winner_index is not None:
            for candidate in candidates:
                if candidate.index == winner_index:
                    continue

                if results[candidate.index] is None:
                    results[candidate.index] = _result(
                        PaperSignalEntryStatus.CORRELATED_SIGNAL_SKIPPED,
                        candidate.signal,
                        proposal=(candidate.proposal),
                        risk_decisions=(candidate.risk_decisions),
                    )

    if any(result is None for result in results):
        raise RuntimeError("V3.2 signal batch left an unresolved result")

    return tuple(
        cast(
            PaperSignalEntryResult,
            result,
        )
        for result in results
    )


def _prepare_candidate(
    *,
    index: int,
    signal: V32Signal,
    snapshots: Sequence[DeltaMarketSnapshot],
    session: PaperLedgerSession,
    proposal_config: (PaperProposalConfig | None),
    portfolio_config: (PortfolioRiskConfig | None),
    observed_ns: int | None,
) -> _PreparedCandidate | PaperSignalEntryResult:
    if session.has_consumed_signal(signal.episode_key):
        return _result(
            PaperSignalEntryStatus.ALREADY_CONSUMED,
            signal,
        )

    snapshot = next(
        (candidate for candidate in snapshots if candidate.underlying == signal.underlying),
        None,
    )

    if snapshot is None:
        return _result(
            PaperSignalEntryStatus.NO_MARKET_SNAPSHOT,
            signal,
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
        candle_close_ms=(signal.candle_close_ms),
        observed_ns=resolved_observed_ns,
        config=(resolved_proposal_config.entry_safety),
    ):
        return _result(
            PaperSignalEntryStatus.STALE_SIGNAL,
            signal,
        )

    contract_type = _contract_type_for(signal)

    ledger_snapshot = session.snapshot()

    if _has_directional_position(
        ledger_snapshot.open_positions,
        contract_type,
    ):
        return _result(
            PaperSignalEntryStatus.DIRECTION_BLOCKED,
            signal,
        )

    proposals = build_ranked_paper_entry_proposals(
        snapshot,
        ledger=ledger_snapshot,
        config=(resolved_proposal_config),
        contract_type=contract_type,
    )

    if not proposals:
        return _result(
            (
                PaperSignalEntryStatus.NO_CALL_PROPOSAL
                if contract_type == "call_options"
                else PaperSignalEntryStatus.NO_PUT_PROPOSAL
            ),
            signal,
        )

    risk_decisions: list[PortfolioRiskDecision] = []

    for proposal in proposals:
        risk = evaluate_portfolio_entry(
            ledger_snapshot,
            proposal,
            config=portfolio_config,
        )
        risk_decisions.append(risk)

        if risk.approved:
            return _PreparedCandidate(
                index=index,
                signal=signal,
                proposal=proposal,
                risk_decisions=tuple(risk_decisions),
            )

    return _result(
        PaperSignalEntryStatus.RISK_REJECTED,
        signal,
        proposal=proposals[0],
        risk_decisions=tuple(risk_decisions),
    )


def _contract_type_for(
    signal: V32Signal,
) -> DeltaOptionContractType:
    contract_type = signal.contract_type

    if contract_type is None:
        raise ValueError("Inactive V3.2 signal has no option direction")

    return contract_type


def _has_directional_position(
    positions: Sequence[PaperPosition],
    contract_type: (DeltaOptionContractType),
) -> bool:
    return any(
        position.contract_type == contract_type and position.underlying in {"BTC", "ETH"}
        for position in positions
    )


def _result(
    status: PaperSignalEntryStatus,
    signal: V32Signal,
    *,
    proposal: (PaperEntryProposal | None) = None,
    position: (PaperPosition | None) = None,
    risk_decisions: tuple[
        PortfolioRiskDecision,
        ...,
    ] = (),
) -> PaperSignalEntryResult:
    return PaperSignalEntryResult(
        status=status,
        signal=signal,
        proposal=proposal,
        position=position,
        risk_decisions=risk_decisions,
    )
