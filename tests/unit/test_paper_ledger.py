from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.product import DeltaOptionProduct
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaOptionProductsSnapshot,
)
from nautilus_delta_options.delta.snapshot import (
    DeltaMarketSnapshot,
    DeltaOptionMarketRecord,
    build_market_snapshot,
)
from nautilus_delta_options.paper.ledger import ExitReason, PaperLedger
from nautilus_delta_options.paper.persistence import SQLitePaperLedgerStore
from nautilus_delta_options.paper.proposals import (
    build_paper_entry_proposal,
    build_ranked_paper_entry_proposals,
    plan_paper_exit_levels,
)
from nautilus_delta_options.paper.session import (
    PaperLedgerConfigurationError,
    PaperLedgerSession,
)
from nautilus_delta_options.selection.eligibility import EligibilityConfig

AS_OF = date(2026, 8, 28)
TIMESTAMP_US = 1_787_878_544_804_095


def _product() -> DeltaOptionProduct:
    return DeltaOptionProduct(
        product_id=1,
        symbol="C-BTC-80000-300826",
        contract_type="call_options",
        underlying="BTC",
        underlying_precision=8,
        quote_currency="USD",
        quote_precision=8,
        settlement_currency="USD",
        settlement_precision=8,
        contract_unit_currency="BTC",
        strike_price=Decimal("80000"),
        contract_value=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        launch_time=datetime(2026, 8, 25, tzinfo=UTC),
        settlement_time=datetime(2026, 8, 30, 12, tzinfo=UTC),
        maker_fee=Decimal("0.0001"),
        taker_fee=Decimal("0.0001"),
        premium_cap_rate=Decimal("0.035"),
        position_size_limit=Decimal("50000"),
        is_quanto=False,
        notional_type="vanilla",
        trading_status="operational",
        state="live",
    )


def _ticker(
    *,
    bid: str = "990",
    ask: str = "1000",
    spot: str = "80000",
    timestamp_us: int = TIMESTAMP_US,
) -> DeltaOptionTicker:
    return DeltaOptionTicker(
        product_id=1,
        symbol="C-BTC-80000-300826",
        underlying="BTC",
        contract_type="call_options",
        strike_price=Decimal("80000"),
        expiry=date(2026, 8, 30),
        mark_price=Decimal(bid),
        spot_price=Decimal(spot),
        contract_value=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        bid_size=Decimal("1000"),
        ask_size=Decimal("1000"),
        mark_iv=Decimal("0.5"),
        bid_iv=Decimal("0.49"),
        ask_iv=Decimal("0.51"),
        delta=Decimal("0.5"),
        gamma=Decimal("0.0001"),
        theta=Decimal("-10"),
        rho=Decimal("1"),
        vega=Decimal("20"),
        open_interest_contracts=Decimal("100"),
        volume=Decimal("10"),
        exchange_timestamp=timestamp_us,
        trading_status="operational",
    )


def _record(
    *,
    bid: str = "990",
    ask: str = "1000",
    spot: str = "80000",
    timestamp_us: int = TIMESTAMP_US,
) -> DeltaOptionMarketRecord:
    ticker = _ticker(
        bid=bid,
        ask=ask,
        spot=spot,
        timestamp_us=timestamp_us,
    )
    snapshot = build_market_snapshot(
        catalog=DeltaOptionProductsSnapshot(
            underlying="BTC",
            products=(_product(),),
            rejected_records=(),
        ),
        chain=DeltaOptionChainSnapshot(
            underlying="BTC",
            tickers=(ticker,),
            rejected_records=(),
        ),
        as_of=AS_OF,
        captured_ns=timestamp_us * 1_000 + 1,
        eligibility_config=EligibilityConfig(),
    )
    return snapshot.records[0]


def _ledger(minimum: str = "1.5") -> PaperLedger:
    return PaperLedger(
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal(minimum),
        max_positions=3,
    )


def test_opens_approved_long_at_ask() -> None:
    ledger = _ledger()

    position = ledger.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    assert position.entry_price == Decimal("1000")
    assert position.entry_premium == Decimal("10.000")
    assert position.entry_fee == Decimal("0.094400000")
    assert position.planned_reward_risk > Decimal("1.52")
    assert ledger.cash == Decimal("89.905600000")
    assert len(ledger.open_positions) == 1


def test_rejects_trade_below_required_payoff() -> None:
    ledger = _ledger("1.6")

    with pytest.raises(ValueError, match="payoff gate"):
        ledger.open_long(
            _record(),
            contracts=Decimal("10"),
            stop_exit_bid=Decimal("900"),
            target_exit_bid=Decimal("1200"),
            stop_spot=Decimal("79500"),
            target_spot=Decimal("81000"),
        )


def test_rejects_fractional_contracts() -> None:
    ledger = _ledger()

    with pytest.raises(ValueError, match="whole number"):
        ledger.open_long(
            _record(),
            contracts=Decimal("1.5"),
            stop_exit_bid=Decimal("900"),
            target_exit_bid=Decimal("1200"),
            stop_spot=Decimal("79500"),
            target_spot=Decimal("81000"),
        )


def test_target_exit_credits_net_proceeds() -> None:
    ledger = _ledger()
    position = ledger.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    closed = ledger.process_exit(
        position.trade_id,
        _record(
            bid="1200",
            ask="1210",
            spot="81000",
            timestamp_us=TIMESTAMP_US + 1,
        ),
    )

    assert closed is not None
    assert closed.reason == ExitReason.TARGET
    assert closed.gross_pnl == Decimal("2.000")
    assert closed.net_pnl == Decimal("1.810020000")
    assert ledger.cash == Decimal("101.810020000")
    assert ledger.realized_pnl == Decimal("1.810020000")
    assert ledger.open_positions == ()


def test_blocks_entry_when_maximum_positions_reached() -> None:
    ledger = PaperLedger(
        initial_cash=Decimal("250"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=1,
    )

    ledger.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    with pytest.raises(ValueError, match="Maximum open positions"):
        ledger.open_long(
            _record(),
            contracts=Decimal("10"),
            stop_exit_bid=Decimal("900"),
            target_exit_bid=Decimal("1200"),
            stop_spot=Decimal("79500"),
            target_spot=Decimal("81000"),
        )


def test_persistent_session_creates_and_reloads_wallet(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")

    created = PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )
    restarted = PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )

    assert created.ledger.cash == Decimal("100")
    assert restarted.ledger.cash == Decimal("100")
    assert restarted.ledger.next_trade_id == 1


def test_persistent_session_rejects_configuration_change(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")

    PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )

    with pytest.raises(
        PaperLedgerConfigurationError,
        match="minimum_reward_risk",
    ):
        PaperLedgerSession.load_or_create(
            store,
            initial_cash=Decimal("100"),
            minimum_reward_risk=Decimal("2.0"),
            max_positions=3,
        )


def test_persistent_session_saves_entry_automatically(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")
    session = PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )

    position = session.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    restored = store.load()

    assert restored is not None
    assert restored.open_positions == (position,)
    assert restored.cash == Decimal("89.905600000")
    assert restored.next_trade_id == 2


def test_persistent_session_saves_exit_automatically(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")
    session = PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )
    position = session.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    closed = session.process_exit(
        position.trade_id,
        _record(
            bid="1200",
            ask="1210",
            spot="81000",
            timestamp_us=TIMESTAMP_US + 1,
        ),
    )
    restored = store.load()

    assert closed is not None
    assert closed.reason == ExitReason.TARGET
    assert restored is not None
    assert restored.open_positions == ()
    assert restored.closed_trades == (closed,)
    assert restored.cash == Decimal("101.810020000")


def test_plans_standard_call_exit_levels() -> None:
    levels = plan_paper_exit_levels(_record())

    assert levels.stop_exit_bid == Decimal("900")
    assert levels.target_exit_bid == Decimal("1200")
    assert levels.stop_spot == Decimal("79200")
    assert levels.target_spot == Decimal("80800")


def test_builds_payoff_approved_sized_proposal() -> None:
    proposal = build_paper_entry_proposal(
        _record(),
        ledger=_ledger(),
    )

    assert proposal.sizing.approved is True
    assert proposal.sizing.contracts == Decimal("16")
    assert proposal.sizing.reward_risk_ratio >= Decimal("1.5")
    assert proposal.sizing.total_planned_loss <= Decimal("2")
    assert proposal.sizing.total_entry_debit <= Decimal("20")


def test_proposal_preserves_net_payoff_gate() -> None:
    proposal = build_paper_entry_proposal(
        _record(),
        ledger=_ledger("1.6"),
    )

    assert proposal.sizing.approved is False
    assert proposal.sizing.contracts == Decimal("0")
    assert proposal.sizing.reward_risk_ratio < Decimal("1.6")


def test_ranked_proposals_exclude_open_product() -> None:
    record = _record()
    ledger = _ledger()
    ledger.open_long(
        record,
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )
    snapshot = DeltaMarketSnapshot(
        underlying="BTC",
        captured_ns=TIMESTAMP_US * 1_000,
        product_count=1,
        ticker_count=1,
        records=(record,),
        unmatched_product_ids=(),
        errors=(),
    )

    proposals = build_ranked_paper_entry_proposals(
        snapshot,
        ledger=ledger,
    )

    assert proposals == ()
