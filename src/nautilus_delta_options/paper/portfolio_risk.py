from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from nautilus_delta_options.paper.ledger import PaperLedger, PaperPosition
from nautilus_delta_options.paper.proposals import PaperEntryProposal


class PortfolioRiskReason(StrEnum):
    SIZING_NOT_APPROVED = "sizing_not_approved"
    NON_POSITIVE_EQUITY = "non_positive_equity"
    MAXIMUM_OPEN_POSITIONS = "maximum_open_positions"
    UNDERLYING_POSITION_LIMIT = "underlying_position_limit"
    PLANNED_LOSS_LIMIT = "planned_loss_limit"
    PREMIUM_EXPOSURE_LIMIT = "premium_exposure_limit"
    INSUFFICIENT_CASH = "insufficient_cash"


@dataclass(frozen=True, slots=True)
class PortfolioRiskConfig:
    max_planned_loss_fraction: Decimal = Decimal("0.06")
    max_premium_exposure_fraction: Decimal = Decimal("0.50")
    max_positions_per_underlying: int = 2

    def __post_init__(self) -> None:
        fractions = (
            (
                "max_planned_loss_fraction",
                self.max_planned_loss_fraction,
            ),
            (
                "max_premium_exposure_fraction",
                self.max_premium_exposure_fraction,
            ),
        )

        for name, value in fractions:
            if value <= 0 or value > 1:
                raise ValueError(f"{name} must be greater than 0 and at most 1")

        if self.max_positions_per_underlying <= 0:
            raise ValueError("max_positions_per_underlying must be positive")


@dataclass(frozen=True, slots=True)
class PortfolioRiskDecision:
    approved: bool
    reasons: tuple[PortfolioRiskReason, ...]
    account_equity: Decimal
    planned_loss_limit: Decimal
    premium_exposure_limit: Decimal
    current_planned_loss: Decimal
    prospective_planned_loss: Decimal
    current_premium_exposure: Decimal
    prospective_premium_exposure: Decimal
    current_underlying_positions: int
    prospective_underlying_positions: int


def evaluate_portfolio_entry(
    ledger: PaperLedger,
    proposal: PaperEntryProposal,
    *,
    config: PortfolioRiskConfig | None = None,
) -> PortfolioRiskDecision:
    resolved_config = config or PortfolioRiskConfig()
    positions = ledger.open_positions
    account_equity = ledger.initial_cash + ledger.realized_pnl
    planned_loss_limit = account_equity * resolved_config.max_planned_loss_fraction
    premium_exposure_limit = account_equity * resolved_config.max_premium_exposure_fraction

    current_planned_loss = sum(
        (_position_planned_loss(position) for position in positions),
        Decimal("0"),
    )
    current_premium_exposure = sum(
        (position.entry_debit for position in positions),
        Decimal("0"),
    )

    candidate_planned_loss = proposal.sizing.total_planned_loss
    candidate_premium = proposal.sizing.total_entry_debit
    prospective_planned_loss = current_planned_loss + candidate_planned_loss
    prospective_premium_exposure = current_premium_exposure + candidate_premium

    underlying = proposal.record.ticker.underlying
    current_underlying_positions = sum(position.underlying == underlying for position in positions)
    prospective_underlying_positions = current_underlying_positions + 1

    reasons: list[PortfolioRiskReason] = []

    if not proposal.sizing.approved:
        reasons.append(PortfolioRiskReason.SIZING_NOT_APPROVED)
    if account_equity <= 0:
        reasons.append(PortfolioRiskReason.NON_POSITIVE_EQUITY)
    if len(positions) >= ledger.max_positions:
        reasons.append(PortfolioRiskReason.MAXIMUM_OPEN_POSITIONS)
    if prospective_underlying_positions > resolved_config.max_positions_per_underlying:
        reasons.append(PortfolioRiskReason.UNDERLYING_POSITION_LIMIT)
    if prospective_planned_loss > planned_loss_limit:
        reasons.append(PortfolioRiskReason.PLANNED_LOSS_LIMIT)
    if prospective_premium_exposure > premium_exposure_limit:
        reasons.append(PortfolioRiskReason.PREMIUM_EXPOSURE_LIMIT)
    if candidate_premium > ledger.cash:
        reasons.append(PortfolioRiskReason.INSUFFICIENT_CASH)

    return PortfolioRiskDecision(
        approved=not reasons,
        reasons=tuple(reasons),
        account_equity=account_equity,
        planned_loss_limit=planned_loss_limit,
        premium_exposure_limit=premium_exposure_limit,
        current_planned_loss=current_planned_loss,
        prospective_planned_loss=prospective_planned_loss,
        current_premium_exposure=current_premium_exposure,
        prospective_premium_exposure=prospective_premium_exposure,
        current_underlying_positions=current_underlying_positions,
        prospective_underlying_positions=prospective_underlying_positions,
    )


def _position_planned_loss(position: PaperPosition) -> Decimal:
    if position.planned_loss > 0:
        return position.planned_loss

    # Legacy snapshots did not retain planned loss. Treat the complete
    # entry debit as exposed risk until that position closes.
    return position.entry_debit
