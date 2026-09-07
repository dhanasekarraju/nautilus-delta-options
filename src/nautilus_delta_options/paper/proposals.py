from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from nautilus_delta_options.delta.models import DeltaOptionContractType
from nautilus_delta_options.delta.snapshot import (
    DeltaMarketSnapshot,
    DeltaOptionMarketRecord,
)
from nautilus_delta_options.paper.ledger import PaperLedger
from nautilus_delta_options.paper.sizing import (
    PositionSizingConfig,
    PositionSizingDecision,
    size_long_option_position,
)
from nautilus_delta_options.selection.entry_safety import (
    EntrySafetyConfig,
    evaluate_option_entry_safety,
)


@dataclass(frozen=True, slots=True)
class PaperProposalConfig:
    stop_premium_fraction: Decimal = Decimal("0.10")
    target_premium_fraction: Decimal = Decimal("0.20")
    spot_move_fraction: Decimal = Decimal("0.01")
    max_proposals: int = 3
    sizing: PositionSizingConfig = field(default_factory=PositionSizingConfig)
    entry_safety: EntrySafetyConfig = field(default_factory=EntrySafetyConfig)

    def __post_init__(self) -> None:
        if not 0 < self.stop_premium_fraction < 1:
            raise ValueError("stop_premium_fraction must be between zero and one")
        if self.target_premium_fraction <= 0:
            raise ValueError("target_premium_fraction must be positive")
        if not 0 < self.spot_move_fraction < 1:
            raise ValueError("spot_move_fraction must be between zero and one")
        if self.max_proposals <= 0:
            raise ValueError("max_proposals must be positive")


@dataclass(frozen=True, slots=True)
class PaperExitLevels:
    stop_exit_bid: Decimal
    target_exit_bid: Decimal
    stop_spot: Decimal
    target_spot: Decimal


@dataclass(frozen=True, slots=True)
class PaperEntryProposal:
    record: DeltaOptionMarketRecord
    levels: PaperExitLevels
    sizing: PositionSizingDecision


def plan_paper_exit_levels(
    record: DeltaOptionMarketRecord,
    *,
    config: PaperProposalConfig | None = None,
) -> PaperExitLevels:
    resolved = config or PaperProposalConfig()
    ticker = record.ticker
    entry_ask = ticker.best_ask

    if entry_ask is None or entry_ask <= 0:
        raise ValueError("A positive entry ask is required to plan exits")
    if ticker.tick_size <= 0:
        raise ValueError("tick_size must be positive")
    if ticker.spot_price <= 0:
        raise ValueError("spot_price must be positive")

    raw_stop = entry_ask * (Decimal("1") - resolved.stop_premium_fraction)
    raw_target = entry_ask * (Decimal("1") + resolved.target_premium_fraction)

    stop_exit_bid = max(
        ticker.tick_size,
        _round_to_tick(
            raw_stop,
            ticker.tick_size,
            rounding=ROUND_FLOOR,
        ),
    )
    target_exit_bid = _round_to_tick(
        raw_target,
        ticker.tick_size,
        rounding=ROUND_CEILING,
    )

    downward_spot = ticker.spot_price * (Decimal("1") - resolved.spot_move_fraction)
    upward_spot = ticker.spot_price * (Decimal("1") + resolved.spot_move_fraction)

    if ticker.contract_type == "call_options":
        stop_spot = downward_spot
        target_spot = upward_spot
    elif ticker.contract_type == "put_options":
        stop_spot = upward_spot
        target_spot = downward_spot
    else:
        raise ValueError(f"Unsupported option contract type: {ticker.contract_type}")

    return PaperExitLevels(
        stop_exit_bid=stop_exit_bid,
        target_exit_bid=target_exit_bid,
        stop_spot=stop_spot,
        target_spot=target_spot,
    )


def build_paper_entry_proposal(
    record: DeltaOptionMarketRecord,
    *,
    ledger: PaperLedger,
    config: PaperProposalConfig | None = None,
) -> PaperEntryProposal:
    resolved = config or PaperProposalConfig()

    if not record.eligibility.eligible:
        raise ValueError("Cannot propose an ineligible option contract")
    if len(ledger.open_positions) >= ledger.max_positions:
        raise ValueError("Maximum open positions reached")
    if any(position.product_id == record.product.product_id for position in ledger.open_positions):
        raise ValueError("An open position already exists for this product")
    if record.quote is None:
        raise ValueError("A current tradeable quote is required")

    safety = evaluate_option_entry_safety(
        record,
        observed_ns=record.quote.ts_init,
        config=resolved.entry_safety,
    )
    if not safety.approved:
        reasons = ", ".join(reason.value for reason in safety.reasons)
        raise ValueError(f"Trade rejected by entry safety gate: {reasons}")

    levels = plan_paper_exit_levels(
        record,
        config=resolved,
    )
    account_equity = ledger.cash + sum(
        (position.entry_debit for position in ledger.open_positions),
        Decimal("0"),
    )
    sizing = size_long_option_position(
        record,
        account_equity=account_equity,
        available_cash=ledger.cash,
        stop_exit_bid=levels.stop_exit_bid,
        target_exit_bid=levels.target_exit_bid,
        stop_spot=levels.stop_spot,
        target_spot=levels.target_spot,
        minimum_reward_risk=ledger.minimum_reward_risk,
        gst_rate=ledger.gst_rate,
        config=resolved.sizing,
    )

    return PaperEntryProposal(
        record=record,
        levels=levels,
        sizing=sizing,
    )


def revalidate_paper_entry_proposal(
    proposal: PaperEntryProposal,
    fresh_record: DeltaOptionMarketRecord,
    *,
    ledger: PaperLedger,
    config: PaperProposalConfig | None = None,
) -> PaperEntryProposal | None:
    """Re-price an approved proposal without moving its payoff levels.

    The original stop/target levels remain frozen. A fresh entry ask
    must still satisfy the same safety, sizing, fees, and minimum net
    reward/risk requirements. This prevents a late premium move from
    being chased by simply moving the target higher.
    """

    resolved = config or PaperProposalConfig()

    original = proposal.record

    identity = (
        fresh_record.product.product_id,
        fresh_record.product.symbol,
        fresh_record.ticker.underlying,
        fresh_record.ticker.contract_type,
    )
    expected = (
        original.product.product_id,
        original.product.symbol,
        original.ticker.underlying,
        original.ticker.contract_type,
    )

    if identity != expected:
        raise ValueError(
            "Fresh proposal record does not match "
            "the original contract"
        )

    if not fresh_record.eligibility.eligible:
        return None

    if fresh_record.quote is None:
        return None

    if len(ledger.open_positions) >= ledger.max_positions:
        return None

    if any(
        position.product_id
        == fresh_record.product.product_id
        for position in ledger.open_positions
    ):
        return None

    safety = evaluate_option_entry_safety(
        fresh_record,
        observed_ns=fresh_record.quote.ts_init,
        config=resolved.entry_safety,
    )

    if not safety.approved:
        return None

    levels = proposal.levels

    account_equity = ledger.cash + sum(
        (
            position.entry_debit
            for position in ledger.open_positions
        ),
        Decimal("0"),
    )

    sizing = size_long_option_position(
        fresh_record,
        account_equity=account_equity,
        available_cash=ledger.cash,
        stop_exit_bid=levels.stop_exit_bid,
        target_exit_bid=levels.target_exit_bid,
        stop_spot=levels.stop_spot,
        target_spot=levels.target_spot,
        minimum_reward_risk=ledger.minimum_reward_risk,
        gst_rate=ledger.gst_rate,
        config=resolved.sizing,
    )

    if not sizing.approved:
        return None

    return PaperEntryProposal(
        record=fresh_record,
        levels=levels,
        sizing=sizing,
    )


def build_ranked_paper_entry_proposals(
    snapshot: DeltaMarketSnapshot,
    *,
    ledger: PaperLedger,
    config: PaperProposalConfig | None = None,
    contract_type: DeltaOptionContractType | None = None,
) -> tuple[PaperEntryProposal, ...]:
    resolved = config or PaperProposalConfig()
    available_slots = ledger.max_positions - len(ledger.open_positions)

    if available_slots <= 0:
        return ()

    open_product_ids = {position.product_id for position in ledger.open_positions}
    candidates = sorted(
        (
            record
            for record in snapshot.records
            if record.eligibility.eligible
            and record.quote is not None
            and (contract_type is None or record.ticker.contract_type == contract_type)
            and record.product.product_id not in open_product_ids
            and evaluate_option_entry_safety(
                record,
                observed_ns=record.quote.ts_init,
                config=resolved.entry_safety,
            ).approved
        ),
        key=_execution_quality_key,
    )

    approved: list[PaperEntryProposal] = []
    proposal_limit = min(
        resolved.max_proposals,
        available_slots,
    )

    for record in candidates:
        proposal = build_paper_entry_proposal(
            record,
            ledger=ledger,
            config=resolved,
        )

        if proposal.sizing.approved:
            approved.append(proposal)

        if len(approved) >= proposal_limit:
            break

    return tuple(approved)


def paper_proposal_quality_key(
    proposal: PaperEntryProposal,
) -> tuple[Decimal, Decimal, Decimal, str]:
    return _execution_quality_key(proposal.record)


def _execution_quality_key(
    record: DeltaOptionMarketRecord,
) -> tuple[Decimal, Decimal, Decimal, str]:
    spread = record.ticker.spread_fraction
    volume = record.ticker.volume or Decimal("0")

    return (
        spread if spread is not None else Decimal("Infinity"),
        -record.ticker.open_interest_contracts,
        -volume,
        record.product.symbol,
    )


def _round_to_tick(
    value: Decimal,
    tick_size: Decimal,
    *,
    rounding: str,
) -> Decimal:
    ticks = (value / tick_size).to_integral_value(rounding=rounding)
    return ticks * tick_size
