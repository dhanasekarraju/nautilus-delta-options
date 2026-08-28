from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

from nautilus_delta_options.delta.snapshot import DeltaOptionMarketRecord
from nautilus_delta_options.paper.ledger import PaperLedger, PaperPosition
from nautilus_delta_options.paper.portfolio_risk import (
    PortfolioRiskConfig,
    PortfolioRiskReason,
    evaluate_portfolio_entry,
)
from nautilus_delta_options.paper.proposals import (
    PaperEntryProposal,
    PaperExitLevels,
)
from nautilus_delta_options.paper.sizing import (
    PositionSizingDecision,
    SizingLimit,
)


def _position(
    trade_id: int,
    *,
    underlying: str = "BTC",
    planned_loss: str = "5",
    entry_debit: str = "40",
) -> PaperPosition:
    return PaperPosition(
        trade_id=trade_id,
        product_id=trade_id,
        symbol=f"C-{underlying}-80000-300826",
        underlying=underlying,
        contract_type="call_options",
        contracts=Decimal("10"),
        contract_value=Decimal("0.001"),
        entry_price=Decimal("1000"),
        entry_spot=Decimal("80000"),
        entry_premium=Decimal("10"),
        entry_fee=Decimal("0.1"),
        entry_debit=Decimal(entry_debit),
        stop_price=Decimal("900"),
        target_price=Decimal("1200"),
        planned_reward_risk=Decimal("1.5"),
        opened_ns=trade_id,
        stop_spot=Decimal("79200"),
        target_spot=Decimal("80800"),
        planned_loss=Decimal(planned_loss),
        planned_reward=Decimal("8"),
    )


def _ledger(
    *positions: PaperPosition,
    cash: str = "250",
    max_positions: int = 3,
) -> PaperLedger:
    return PaperLedger.from_state(
        initial_cash=Decimal("250"),
        cash=Decimal(cash),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=max_positions,
        gst_rate=Decimal("0.18"),
        next_trade_id=max(
            (position.trade_id for position in positions),
            default=0,
        )
        + 1,
        open_positions=positions,
        closed_trades=(),
    )


def _proposal(
    *,
    underlying: str = "BTC",
    planned_loss: str = "5",
    entry_debit: str = "40",
    approved: bool = True,
) -> PaperEntryProposal:
    sizing = PositionSizingDecision(
        approved=approved,
        contracts=Decimal("10"),
        limiting_factor=SizingLimit.RISK,
        risk_budget=Decimal("5"),
        premium_budget=Decimal("50"),
        planned_loss_per_contract=Decimal("0.5"),
        planned_reward_per_contract=Decimal("0.8"),
        entry_debit_per_contract=Decimal("4"),
        total_planned_loss=Decimal(planned_loss),
        total_planned_reward=Decimal("8"),
        total_entry_debit=Decimal(entry_debit),
        reward_risk_ratio=Decimal("1.6"),
    )
    record = cast(
        DeltaOptionMarketRecord,
        SimpleNamespace(
            ticker=SimpleNamespace(underlying=underlying),
        ),
    )

    return PaperEntryProposal(
        record=record,
        levels=PaperExitLevels(
            stop_exit_bid=Decimal("900"),
            target_exit_bid=Decimal("1200"),
            stop_spot=Decimal("79200"),
            target_spot=Decimal("80800"),
        ),
        sizing=sizing,
    )


def test_approves_entry_within_portfolio_limits() -> None:
    decision = evaluate_portfolio_entry(
        _ledger(),
        _proposal(),
    )

    assert decision.approved
    assert decision.reasons == ()
    assert decision.planned_loss_limit == Decimal("15.00")
    assert decision.premium_exposure_limit == Decimal("125.00")
    assert decision.prospective_planned_loss == Decimal("5")
    assert decision.prospective_premium_exposure == Decimal("40")


def test_rejects_total_planned_loss_above_six_percent() -> None:
    decision = evaluate_portfolio_entry(
        _ledger(
            _position(
                1,
                planned_loss="11",
                entry_debit="20",
            ),
        ),
        _proposal(
            planned_loss="5",
            entry_debit="30",
        ),
    )

    assert not decision.approved
    assert PortfolioRiskReason.PLANNED_LOSS_LIMIT in decision.reasons
    assert decision.prospective_planned_loss == Decimal("16")


def test_rejects_total_premium_exposure_above_fifty_percent() -> None:
    decision = evaluate_portfolio_entry(
        _ledger(
            _position(
                1,
                planned_loss="3",
                entry_debit="100",
            ),
            cash="150",
        ),
        _proposal(
            planned_loss="5",
            entry_debit="30",
        ),
    )

    assert not decision.approved
    assert PortfolioRiskReason.PREMIUM_EXPOSURE_LIMIT in decision.reasons
    assert decision.prospective_premium_exposure == Decimal("130")


def test_rejects_third_position_for_same_underlying() -> None:
    decision = evaluate_portfolio_entry(
        _ledger(
            _position(1, planned_loss="3", entry_debit="20"),
            _position(2, planned_loss="3", entry_debit="20"),
        ),
        _proposal(
            underlying="BTC",
            planned_loss="5",
            entry_debit="30",
        ),
    )

    assert not decision.approved
    assert PortfolioRiskReason.UNDERLYING_POSITION_LIMIT in decision.reasons
    assert decision.prospective_underlying_positions == 3


def test_rejects_when_maximum_positions_are_already_open() -> None:
    decision = evaluate_portfolio_entry(
        _ledger(
            _position(
                1,
                underlying="BTC",
                planned_loss="1",
                entry_debit="10",
            ),
            _position(
                2,
                underlying="ETH",
                planned_loss="1",
                entry_debit="10",
            ),
            _position(
                3,
                underlying="SOL",
                planned_loss="1",
                entry_debit="10",
            ),
        ),
        _proposal(
            planned_loss="5",
            entry_debit="30",
        ),
    )

    assert not decision.approved
    assert PortfolioRiskReason.MAXIMUM_OPEN_POSITIONS in decision.reasons


def test_rejects_entry_above_available_cash() -> None:
    decision = evaluate_portfolio_entry(
        _ledger(cash="25"),
        _proposal(
            planned_loss="5",
            entry_debit="40",
        ),
    )

    assert not decision.approved
    assert PortfolioRiskReason.INSUFFICIENT_CASH in decision.reasons


def test_legacy_position_uses_full_debit_as_conservative_risk() -> None:
    decision = evaluate_portfolio_entry(
        _ledger(
            _position(
                1,
                planned_loss="0",
                entry_debit="40",
            ),
        ),
        _proposal(),
    )

    assert decision.current_planned_loss == Decimal("40")
    assert PortfolioRiskReason.PLANNED_LOSS_LIMIT in decision.reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_planned_loss_fraction", Decimal("0")),
        ("max_planned_loss_fraction", Decimal("1.01")),
        ("max_premium_exposure_fraction", Decimal("0")),
        ("max_premium_exposure_fraction", Decimal("1.01")),
        ("max_positions_per_underlying", 0),
    ],
)
def test_invalid_config_is_rejected(
    field: str,
    value: Decimal | int,
) -> None:
    kwargs = {field: value}

    with pytest.raises(ValueError, match=field):
        PortfolioRiskConfig(**kwargs)  # type: ignore[arg-type]
