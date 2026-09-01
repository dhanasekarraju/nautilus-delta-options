from dataclasses import replace
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
from nautilus_delta_options.paper.exit_policy import PaperExitPolicyConfig
from nautilus_delta_options.paper.ledger import ExitReason, PaperLedger
from nautilus_delta_options.paper.observer import PaperDryRunObserver
from nautilus_delta_options.paper.persistence import SQLitePaperLedgerStore
from nautilus_delta_options.paper.portfolio_risk import PortfolioRiskConfig
from nautilus_delta_options.paper.proposals import (
    build_paper_entry_proposal,
    build_ranked_paper_entry_proposals,
    plan_paper_exit_levels,
)
from nautilus_delta_options.paper.session import (
    PaperLedgerConfigurationError,
    PaperLedgerSession,
    PaperSignalAlreadyConsumedError,
)
from nautilus_delta_options.paper.signal_entries import (
    PaperSignalEntryStatus,
    process_v31_call_signal,
)
from nautilus_delta_options.selection.eligibility import EligibilityConfig
from nautilus_delta_options.selection.entry_safety import (
    EntrySafetyReason,
    evaluate_option_entry_safety,
)
from nautilus_delta_options.signals.v31 import (
    V31CallSignal,
    V31SignalDecision,
)

AS_OF = date(2026, 8, 28)
TIMESTAMP_US = 1_787_878_544_804_095
TIMESTAMP_NS = TIMESTAMP_US * 1_000
PAPER_SIGNAL_KEY = "BTC:1787887499999:call"
PAPER_SIGNAL_CLOSED_NS = 1_787_887_499_999_000_000


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
    assert position.taker_fee == Decimal("0.0001")
    assert position.premium_cap_rate == Decimal("0.035")
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
    assert restored.open_positions[0].taker_fee == Decimal("0.0001")
    assert restored.open_positions[0].premium_cap_rate == Decimal("0.035")
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


def test_default_option_entry_safety_accepts_liquid_atm_contract() -> None:
    result = evaluate_option_entry_safety(
        _record(),
        observed_ns=TIMESTAMP_NS + 1,
    )

    assert result.approved
    assert result.reasons == ()


def test_option_entry_safety_rejects_bad_delta_and_moneyness() -> None:
    record = _record(spot="60000")
    record = replace(
        record,
        ticker=replace(record.ticker, delta=Decimal("0.10")),
    )

    result = evaluate_option_entry_safety(
        record,
        observed_ns=TIMESTAMP_NS + 1,
    )

    assert not result.approved
    assert EntrySafetyReason.DELTA_OUT_OF_RANGE in result.reasons
    assert EntrySafetyReason.MONEYNESS_TOO_WIDE in result.reasons
    with pytest.raises(ValueError, match="delta_out_of_range"):
        build_paper_entry_proposal(record, ledger=_ledger())


def test_option_entry_safety_rejects_stale_quote_and_near_expiry() -> None:
    stale = evaluate_option_entry_safety(
        _record(),
        observed_ns=TIMESTAMP_NS + 16_000_000_000,
    )
    settlement_ns = int(_product().settlement_time.timestamp()) * 1_000_000_000
    near_expiry = evaluate_option_entry_safety(
        _record(),
        observed_ns=settlement_ns - 35 * 3_600_000_000_000,
    )

    assert EntrySafetyReason.STALE_QUOTE in stale.reasons
    assert EntrySafetyReason.TOO_CLOSE_TO_SETTLEMENT in near_expiry.reasons


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


class _StaticMarketClient:
    def __init__(self, ticker: DeltaOptionTicker) -> None:
        self._ticker = ticker

    def fetch_option_products(
        self,
        underlying: object,
    ) -> DeltaOptionProductsSnapshot:
        assert underlying == "BTC"
        return DeltaOptionProductsSnapshot(
            underlying="BTC",
            products=(_product(),),
            rejected_records=(),
        )

    def fetch_option_chain(
        self,
        underlying: object,
    ) -> DeltaOptionChainSnapshot:
        assert underlying == "BTC"
        return DeltaOptionChainSnapshot(
            underlying="BTC",
            tickers=(self._ticker,),
            rejected_records=(),
        )


def test_observer_builds_proposal_without_opening_trade(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")
    session = PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )
    observer = PaperDryRunObserver(
        client=_StaticMarketClient(_ticker()),
        session=session,
        underlyings=("BTC",),
        clock_ns=lambda: TIMESTAMP_NS + 1,
    )

    cycle = observer.run_cycle(as_of=AS_OF)

    assert len(cycle.snapshots) == 1
    assert cycle.closed_trades == ()
    assert cycle.warnings == ()
    assert len(cycle.proposals) == 1
    assert cycle.proposals[0].sizing.approved is True
    assert cycle.proposals[0].sizing.contracts == Decimal("16")
    assert session.ledger.open_positions == ()


def test_observer_closes_and_persists_target_before_proposals(
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
    observer = PaperDryRunObserver(
        client=_StaticMarketClient(
            _ticker(
                bid="1200",
                ask="1210",
                spot="81000",
                timestamp_us=TIMESTAMP_US + 1,
            )
        ),
        session=session,
        underlyings=("BTC",),
        clock_ns=lambda: TIMESTAMP_NS + 1_001,
    )

    cycle = observer.run_cycle(as_of=AS_OF)
    restored = store.load()

    assert len(cycle.closed_trades) == 1
    assert cycle.closed_trades[0].position.trade_id == (position.trade_id)
    assert cycle.closed_trades[0].reason == ExitReason.TARGET
    assert cycle.proposals == ()
    assert restored is not None
    assert restored.open_positions == ()
    assert restored.closed_trades == cycle.closed_trades
    assert restored.cash == Decimal("101.810020000")


def test_observer_forces_time_exit_and_persists_it(
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
    exit_timestamp_us = TIMESTAMP_US + 241 * 60 * 1_000_000
    observer = PaperDryRunObserver(
        client=_StaticMarketClient(_ticker(timestamp_us=exit_timestamp_us)),
        session=session,
        underlyings=("BTC",),
        exit_policy_config=PaperExitPolicyConfig(max_hold_minutes=240),
        clock_ns=lambda: exit_timestamp_us * 1_000 + 1,
    )

    cycle = observer.run_cycle(as_of=AS_OF)
    restored = store.load()

    assert len(cycle.closed_trades) == 1
    assert cycle.closed_trades[0].position.trade_id == position.trade_id
    assert cycle.closed_trades[0].reason is ExitReason.TIME
    assert restored is not None
    assert restored.open_positions == ()
    assert restored.closed_trades == cycle.closed_trades


def test_observer_rejects_exchange_event_ahead_of_local_clock(
    tmp_path: Path,
) -> None:
    future_timestamp_us = 2_000_000_000_000_000
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")
    session = PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )
    observer = PaperDryRunObserver(
        client=_StaticMarketClient(_ticker(timestamp_us=future_timestamp_us)),
        session=session,
        underlyings=("BTC",),
        clock_ns=lambda: TIMESTAMP_NS,
    )

    with pytest.raises(ValueError, match="ahead of the VPS clock"):
        observer.run_cycle(as_of=AS_OF)


def _paper_signal_session(
    tmp_path: Path,
) -> tuple[PaperLedgerSession, SQLitePaperLedgerStore]:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")
    session = PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )
    return session, store


def test_signal_entry_atomically_persists_position_and_receipt(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)

    position = session.open_long_for_signal(
        _record(),
        signal_key=PAPER_SIGNAL_KEY,
        signal_underlying="BTC",
        candle_closed_ns=PAPER_SIGNAL_CLOSED_NS,
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    assert store.has_consumed_signal(PAPER_SIGNAL_KEY)
    assert position.planned_loss > 0
    assert position.planned_reward > 0
    assert position.stop_spot == Decimal("79500")
    assert position.target_spot == Decimal("81000")

    restarted = PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )

    assert restarted.ledger.open_positions == (position,)
    assert restarted.has_consumed_signal(PAPER_SIGNAL_KEY)


def test_duplicate_signal_does_not_mutate_session_ledger(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)

    position = session.open_long_for_signal(
        _record(),
        signal_key=PAPER_SIGNAL_KEY,
        signal_underlying="BTC",
        candle_closed_ns=PAPER_SIGNAL_CLOSED_NS,
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )
    cash_after_first_entry = session.ledger.cash

    with pytest.raises(
        PaperSignalAlreadyConsumedError,
        match="already consumed",
    ):
        session.open_long_for_signal(
            _record(),
            signal_key=PAPER_SIGNAL_KEY,
            signal_underlying="BTC",
            candle_closed_ns=PAPER_SIGNAL_CLOSED_NS,
            contracts=Decimal("10"),
            stop_exit_bid=Decimal("900"),
            target_exit_bid=Decimal("1200"),
            stop_spot=Decimal("79500"),
            target_spot=Decimal("81000"),
        )

    assert session.ledger.cash == cash_after_first_entry
    assert session.ledger.open_positions == (position,)

    restored = store.load()

    assert restored is not None
    assert restored.cash == cash_after_first_entry
    assert restored.open_positions == (position,)


def test_failed_signal_entry_is_not_consumed(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)

    with pytest.raises(ValueError, match="Expected stop"):
        session.open_long_for_signal(
            _record(),
            signal_key=PAPER_SIGNAL_KEY,
            signal_underlying="BTC",
            candle_closed_ns=PAPER_SIGNAL_CLOSED_NS,
            contracts=Decimal("10"),
            stop_exit_bid=Decimal("900"),
            target_exit_bid=Decimal("950"),
            stop_spot=Decimal("79500"),
            target_spot=Decimal("81000"),
        )

    assert not store.has_consumed_signal(PAPER_SIGNAL_KEY)
    assert session.ledger.cash == Decimal("100")
    assert session.ledger.open_positions == ()


def test_signal_underlying_mismatch_is_not_consumed(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)

    with pytest.raises(ValueError, match="Signal underlying"):
        session.open_long_for_signal(
            _record(),
            signal_key=PAPER_SIGNAL_KEY,
            signal_underlying="ETH",
            candle_closed_ns=PAPER_SIGNAL_CLOSED_NS,
            contracts=Decimal("10"),
            stop_exit_bid=Decimal("900"),
            target_exit_bid=Decimal("1200"),
            stop_spot=Decimal("79500"),
            target_spot=Decimal("81000"),
        )

    assert not store.has_consumed_signal(PAPER_SIGNAL_KEY)
    assert session.ledger.cash == Decimal("100")
    assert session.ledger.open_positions == ()


def _v31_signal(
    *,
    underlying: str = "BTC",
    active: bool = True,
) -> V31CallSignal:
    return V31CallSignal(
        underlying=underlying,
        symbol=f"{underlying}USDT",
        candle_open_ms=1_787_887_200_000,
        candle_close_ms=1_787_887_499_999,
        close_price=80000.0,
        rsi=30.0 if active else 45.0,
        ema20=80100.0 if active else 79900.0,
        ema50=80000.0,
        atr=200.0,
        atr_pct=0.0025,
        volume=100.0,
        rsi_below_35=active,
        ema20_above_ema50=active,
        atr_pct_below_035=True,
        positive_volume=True,
        decision=(V31SignalDecision.CALL if active else V31SignalDecision.NONE),
    )


def _signal_snapshot(
    record: DeltaOptionMarketRecord | None = None,
) -> DeltaMarketSnapshot:
    resolved = record or _record()

    return DeltaMarketSnapshot(
        underlying=resolved.ticker.underlying,
        captured_ns=TIMESTAMP_US * 1_000,
        product_count=1,
        ticker_count=1,
        records=(resolved,),
        unmatched_product_ids=(),
        errors=(),
    )


def _put_record() -> DeltaOptionMarketRecord:
    call_record = _record()
    symbol = "P-BTC-80000-300826"

    return replace(
        call_record,
        product=replace(
            call_record.product,
            symbol=symbol,
            contract_type="put_options",
        ),
        ticker=replace(
            call_record.ticker,
            symbol=symbol,
            contract_type="put_options",
            delta=Decimal("-0.5"),
        ),
    )


def test_signal_entries_remain_disabled_without_opt_in(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)
    signal = _v31_signal()

    result = process_v31_call_signal(
        signal,
        (_signal_snapshot(),),
        session=session,
        entries_enabled=False,
    )

    assert result.status is PaperSignalEntryStatus.DISABLED
    assert result.position is None
    assert session.ledger.open_positions == ()
    assert not store.has_consumed_signal(signal.signal_key)


def test_wait_signal_does_not_open_or_consume(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)
    signal = _v31_signal(active=False)

    result = process_v31_call_signal(
        signal,
        (_signal_snapshot(),),
        session=session,
        entries_enabled=True,
    )

    assert result.status is PaperSignalEntryStatus.WAIT
    assert result.position is None
    assert session.ledger.open_positions == ()
    assert not store.has_consumed_signal(signal.signal_key)


def test_stale_call_signal_does_not_open_or_consume(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)
    signal = _v31_signal()

    result = process_v31_call_signal(
        signal,
        (_signal_snapshot(),),
        session=session,
        entries_enabled=True,
        observed_ns=PAPER_SIGNAL_CLOSED_NS + 361_000_000_000,
    )

    assert result.status is PaperSignalEntryStatus.STALE_SIGNAL
    assert result.position is None
    assert session.ledger.open_positions == ()
    assert not store.has_consumed_signal(signal.signal_key)


def test_active_call_signal_opens_matching_call_atomically(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)
    signal = _v31_signal()

    result = process_v31_call_signal(
        signal,
        (_signal_snapshot(),),
        session=session,
        entries_enabled=True,
    )

    assert result.status is PaperSignalEntryStatus.OPENED
    assert result.position is not None
    assert result.position.underlying == "BTC"
    assert result.position.contract_type == "call_options"
    assert result.proposal is not None
    assert result.risk_decisions[-1].approved
    assert session.ledger.open_positions == (result.position,)
    assert store.has_consumed_signal(signal.signal_key)


def test_consumed_call_signal_cannot_open_twice(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)
    signal = _v31_signal()
    snapshots = (_signal_snapshot(),)

    first = process_v31_call_signal(
        signal,
        snapshots,
        session=session,
        entries_enabled=True,
    )
    second = process_v31_call_signal(
        signal,
        snapshots,
        session=session,
        entries_enabled=True,
    )

    assert first.status is PaperSignalEntryStatus.OPENED
    assert second.status is PaperSignalEntryStatus.ALREADY_CONSUMED
    assert len(session.ledger.open_positions) == 1
    assert store.has_consumed_signal(signal.signal_key)


def test_portfolio_rejection_does_not_consume_signal(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)
    signal = _v31_signal()

    result = process_v31_call_signal(
        signal,
        (_signal_snapshot(),),
        session=session,
        entries_enabled=True,
        portfolio_config=PortfolioRiskConfig(
            max_planned_loss_fraction=Decimal("0.001"),
        ),
    )

    assert result.status is PaperSignalEntryStatus.RISK_REJECTED
    assert result.position is None
    assert result.risk_decisions
    assert not result.risk_decisions[0].approved
    assert session.ledger.open_positions == ()
    assert not store.has_consumed_signal(signal.signal_key)


def test_call_signal_never_selects_put_proposal(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)
    signal = _v31_signal()

    result = process_v31_call_signal(
        signal,
        (_signal_snapshot(_put_record()),),
        session=session,
        entries_enabled=True,
    )

    assert result.status is PaperSignalEntryStatus.NO_CALL_PROPOSAL
    assert result.position is None
    assert session.ledger.open_positions == ()
    assert not store.has_consumed_signal(signal.signal_key)


def test_call_signal_requires_same_underlying_snapshot(
    tmp_path: Path,
) -> None:
    session, store = _paper_signal_session(tmp_path)
    signal = _v31_signal(underlying="ETH")

    result = process_v31_call_signal(
        signal,
        (_signal_snapshot(),),
        session=session,
        entries_enabled=True,
    )

    assert result.status is PaperSignalEntryStatus.NO_MARKET_SNAPSHOT
    assert result.position is None
    assert session.ledger.open_positions == ()
    assert not store.has_consumed_signal(signal.signal_key)


class _StaticSignalClient:
    def fetch_v31_candles(self, underlying: object) -> object:
        assert underlying == "BTC"
        return object()


def _install_static_call_signal(
    monkeypatch: pytest.MonkeyPatch,
    signal: V31CallSignal,
) -> None:
    def fake_evaluate(_: object) -> V31CallSignal:
        return signal

    monkeypatch.setattr(
        "nautilus_delta_options.paper.observer.evaluate_v31_call_signal",
        fake_evaluate,
    )


def test_observer_entry_mode_defaults_to_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, store = _paper_signal_session(tmp_path)
    signal = _v31_signal()
    _install_static_call_signal(monkeypatch, signal)
    observer = PaperDryRunObserver(
        client=_StaticMarketClient(_ticker(timestamp_us=signal.candle_close_ms * 1_000)),
        session=session,
        underlyings=("BTC",),
        signal_client=_StaticSignalClient(),
        clock_ns=lambda: PAPER_SIGNAL_CLOSED_NS,
    )

    cycle = observer.run_cycle(as_of=AS_OF)

    assert not observer.entries_enabled
    assert not cycle.entries_enabled
    assert cycle.signals == (signal,)
    assert len(cycle.entry_results) == 1
    assert cycle.entry_results[0].status is PaperSignalEntryStatus.DISABLED
    assert session.ledger.open_positions == ()
    assert not store.has_consumed_signal(signal.signal_key)


def test_observer_enabled_mode_opens_once_per_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, store = _paper_signal_session(tmp_path)
    signal = _v31_signal()
    _install_static_call_signal(monkeypatch, signal)
    observer = PaperDryRunObserver(
        client=_StaticMarketClient(_ticker(timestamp_us=signal.candle_close_ms * 1_000)),
        session=session,
        underlyings=("BTC",),
        signal_client=_StaticSignalClient(),
        entries_enabled=True,
        clock_ns=lambda: PAPER_SIGNAL_CLOSED_NS,
    )

    first_cycle = observer.run_cycle(as_of=AS_OF)
    second_cycle = observer.run_cycle(as_of=AS_OF)

    assert observer.entries_enabled
    assert first_cycle.entries_enabled
    assert first_cycle.entry_results[0].status is PaperSignalEntryStatus.OPENED
    assert second_cycle.entry_results[0].status is PaperSignalEntryStatus.ALREADY_CONSUMED
    assert len(session.ledger.open_positions) == 1
    assert store.has_consumed_signal(signal.signal_key)

    restored = store.load()

    assert restored is not None
    assert restored.open_positions == session.ledger.open_positions


def test_ticker_only_target_matches_full_record_exit() -> None:
    record_ledger = _ledger()
    ticker_ledger = _ledger()

    record_position = record_ledger.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )
    ticker_position = ticker_ledger.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    exit_record = _record(
        bid="1200",
        ask="1210",
        spot="81000",
        timestamp_us=TIMESTAMP_US + 1,
    )

    record_closed = record_ledger.process_exit(
        record_position.trade_id,
        exit_record,
    )
    ticker_closed = ticker_ledger.process_exit_ticker(
        ticker_position.trade_id,
        exit_record.ticker,
    )

    assert ticker_closed == record_closed
    assert ticker_ledger.cash == record_ledger.cash
    assert ticker_ledger.realized_pnl == record_ledger.realized_pnl


def test_ticker_only_stop_uses_current_executable_bid() -> None:
    ledger = _ledger()
    position = ledger.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    closed = ledger.process_exit_ticker(
        position.trade_id,
        _ticker(
            bid="880",
            ask="890",
            spot="79000",
            timestamp_us=TIMESTAMP_US + 1,
        ),
    )

    assert closed is not None
    assert closed.reason == ExitReason.STOP
    assert closed.exit_price == Decimal("880")
    assert ledger.open_positions == ()


def test_ticker_only_exit_rejects_stale_ticker() -> None:
    ledger = _ledger()
    position = ledger.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    with pytest.raises(
        ValueError,
        match="predates the position",
    ):
        ledger.process_exit_ticker(
            position.trade_id,
            _ticker(
                bid="880",
                ask="890",
                timestamp_us=TIMESTAMP_US - 1,
            ),
        )


def test_ticker_only_exit_rejects_mismatched_product() -> None:
    ledger = _ledger()
    position = ledger.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    mismatched = replace(
        _ticker(
            bid="880",
            ask="890",
            timestamp_us=TIMESTAMP_US + 1,
        ),
        product_id=999,
    )

    with pytest.raises(
        ValueError,
        match="product_id mismatch",
    ):
        ledger.process_exit_ticker(
            position.trade_id,
            mismatched,
        )


def test_ticker_only_exit_rejects_insufficient_bid_depth() -> None:
    ledger = _ledger()
    position = ledger.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    shallow = replace(
        _ticker(
            bid="880",
            ask="890",
            timestamp_us=TIMESTAMP_US + 1,
        ),
        bid_size=Decimal("5"),
    )

    with pytest.raises(
        ValueError,
        match="available bid depth",
    ):
        ledger.process_exit_ticker(
            position.trade_id,
            shallow,
        )


def test_persistent_session_saves_ticker_exit_automatically(
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

    closed = session.process_exit_ticker(
        position.trade_id,
        _ticker(
            bid="880",
            ask="890",
            spot="79000",
            timestamp_us=TIMESTAMP_US + 1,
        ),
    )

    restored = store.load()

    assert closed is not None
    assert closed.reason == ExitReason.STOP
    assert restored is not None
    assert restored.open_positions == ()
    assert restored.closed_trades == (closed,)


def test_session_ledger_returns_detached_snapshot(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")
    session = PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )

    before_entry = session.ledger

    position = session.open_long(
        _record(),
        contracts=Decimal("10"),
        stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"),
        stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )

    after_entry = session.snapshot()

    assert before_entry.open_positions == ()
    assert before_entry.cash == Decimal("100")

    assert after_entry.open_positions == (position,)
    assert after_entry.cash == session.ledger.cash

    assert before_entry is not after_entry
    assert after_entry is not session.ledger
