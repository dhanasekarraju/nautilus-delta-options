from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.product import DeltaOptionProduct
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaOptionProductsSnapshot,
)
from nautilus_delta_options.delta.snapshot import (
    DeltaMarketSnapshot,
    build_market_snapshot,
)
from nautilus_delta_options.paper.persistence import SQLitePaperLedgerStore
from nautilus_delta_options.paper.proposals import PaperEntryProposal
from nautilus_delta_options.paper.session import PaperLedgerSession
from nautilus_delta_options.paper.signal_entries_v32 import (
    PaperSignalEntryStatus,
    process_v32_signal,
    process_v32_signals,
)
from nautilus_delta_options.selection.eligibility import EligibilityConfig
from nautilus_delta_options.signals.v32 import (
    V32Signal,
    V32SignalDecision,
)

AS_OF = date(2026, 8, 28)
TIMESTAMP_US = 1_787_878_544_804_095
BASE_CANDLE_CLOSE_MS = 1_787_887_499_999


def _session(
    tmp_path: Path,
) -> tuple[
    PaperLedgerSession,
    SQLitePaperLedgerStore,
]:
    store = SQLitePaperLedgerStore(tmp_path / "paper-v32.sqlite")

    session = PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("250"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )

    return session, store


def _signal(
    *,
    underlying: str,
    decision: V32SignalDecision,
    candle_close_ms: int = BASE_CANDLE_CLOSE_MS,
) -> V32Signal:
    if decision is V32SignalDecision.CALL:
        rsi = 30.0
        ema20 = 80100.0
        ema50 = 80000.0
    elif decision is V32SignalDecision.PUT:
        rsi = 70.0
        ema20 = 79900.0
        ema50 = 80000.0
    else:
        rsi = 50.0
        ema20 = 80000.0
        ema50 = 80000.0

    return V32Signal(
        underlying=underlying,
        symbol=f"{underlying}USDT",
        candle_open_ms=candle_close_ms - 299_999,
        candle_close_ms=candle_close_ms,
        close_price=80000.0,
        rsi=rsi,
        ema20=ema20,
        ema50=ema50,
        atr=200.0,
        atr_pct=0.0025,
        volume=100.0,
        rsi_below_35=rsi < 35,
        rsi_above_65=rsi > 65,
        ema20_above_ema50=ema20 > ema50,
        ema20_below_ema50=ema20 < ema50,
        atr_pct_below_035=True,
        positive_volume=True,
        decision=decision,
    )


def _snapshot(
    *,
    underlying: str,
    contract_type: str,
    product_id: int,
    bid: str = "990",
    ask: str = "1000",
    open_interest: str = "100",
    volume: str = "10",
) -> DeltaMarketSnapshot:
    prefix = "C" if contract_type == "call_options" else "P"

    symbol = f"{prefix}-{underlying}-80000-300826"

    product = DeltaOptionProduct(
        product_id=product_id,
        symbol=symbol,
        contract_type=contract_type,  # type: ignore[arg-type]
        underlying=underlying,
        underlying_precision=8,
        quote_currency="USD",
        quote_precision=8,
        settlement_currency="USD",
        settlement_precision=8,
        contract_unit_currency=underlying,
        strike_price=Decimal("80000"),
        contract_value=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        launch_time=datetime(
            2026,
            8,
            25,
            tzinfo=UTC,
        ),
        settlement_time=datetime(
            2026,
            8,
            30,
            12,
            tzinfo=UTC,
        ),
        maker_fee=Decimal("0.0001"),
        taker_fee=Decimal("0.0001"),
        premium_cap_rate=Decimal("0.035"),
        position_size_limit=Decimal("50000"),
        is_quanto=False,
        notional_type="vanilla",
        trading_status="operational",
        state="live",
    )

    ticker = DeltaOptionTicker(
        product_id=product_id,
        symbol=symbol,
        underlying=underlying,
        contract_type=contract_type,  # type: ignore[arg-type]
        strike_price=Decimal("80000"),
        expiry=date(2026, 8, 30),
        mark_price=Decimal(bid),
        spot_price=Decimal("80000"),
        contract_value=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        bid_size=Decimal("1000"),
        ask_size=Decimal("1000"),
        mark_iv=Decimal("0.5"),
        bid_iv=Decimal("0.49"),
        ask_iv=Decimal("0.51"),
        delta=(Decimal("0.5") if contract_type == "call_options" else Decimal("-0.5")),
        gamma=Decimal("0.0001"),
        theta=Decimal("-10"),
        rho=Decimal("1"),
        vega=Decimal("20"),
        open_interest_contracts=Decimal(open_interest),
        volume=Decimal(volume),
        exchange_timestamp=TIMESTAMP_US,
        trading_status="operational",
    )

    return build_market_snapshot(
        catalog=DeltaOptionProductsSnapshot(
            underlying=underlying,  # type: ignore[arg-type]
            products=(product,),
            rejected_records=(),
        ),
        chain=DeltaOptionChainSnapshot(
            underlying=underlying,  # type: ignore[arg-type]
            tickers=(ticker,),
            rejected_records=(),
        ),
        as_of=AS_OF,
        captured_ns=TIMESTAMP_US * 1_000 + 1,
        eligibility_config=EligibilityConfig(),
    )


def test_same_candle_btc_eth_call_opens_only_best(
    tmp_path: Path,
) -> None:
    session, store = _session(tmp_path)

    btc = _signal(
        underlying="BTC",
        decision=V32SignalDecision.CALL,
    )
    eth = _signal(
        underlying="ETH",
        decision=V32SignalDecision.CALL,
    )

    # BTC has the tighter spread, so it must win.
    btc_snapshot = _snapshot(
        underlying="BTC",
        contract_type="call_options",
        product_id=1,
        bid="995",
        ask="1000",
    )
    eth_snapshot = _snapshot(
        underlying="ETH",
        contract_type="call_options",
        product_id=2,
        bid="980",
        ask="1000",
    )

    results = process_v32_signals(
        (btc, eth),
        (btc_snapshot, eth_snapshot),
        session=session,
        entries_enabled=True,
        observed_ns=(BASE_CANDLE_CLOSE_MS * 1_000_000),
    )

    assert results[0].status is (PaperSignalEntryStatus.OPENED)
    assert results[1].status is (PaperSignalEntryStatus.CORRELATED_SIGNAL_SKIPPED)

    assert len(session.ledger.open_positions) == 1

    position = session.ledger.open_positions[0]

    assert position.underlying == "BTC"
    assert position.contract_type == "call_options"

    assert store.has_consumed_signal(btc.episode_key)
    assert btc.episode_key == eth.episode_key


def test_same_candle_btc_eth_put_opens_only_best(
    tmp_path: Path,
) -> None:
    session, store = _session(tmp_path)

    btc = _signal(
        underlying="BTC",
        decision=V32SignalDecision.PUT,
    )
    eth = _signal(
        underlying="ETH",
        decision=V32SignalDecision.PUT,
    )

    # ETH gets the tighter spread this time.
    btc_snapshot = _snapshot(
        underlying="BTC",
        contract_type="put_options",
        product_id=11,
        bid="980",
        ask="1000",
    )
    eth_snapshot = _snapshot(
        underlying="ETH",
        contract_type="put_options",
        product_id=12,
        bid="995",
        ask="1000",
    )

    results = process_v32_signals(
        (btc, eth),
        (btc_snapshot, eth_snapshot),
        session=session,
        entries_enabled=True,
        observed_ns=(BASE_CANDLE_CLOSE_MS * 1_000_000),
    )

    assert results[0].status is (PaperSignalEntryStatus.CORRELATED_SIGNAL_SKIPPED)
    assert results[1].status is (PaperSignalEntryStatus.OPENED)

    assert len(session.ledger.open_positions) == 1

    position = session.ledger.open_positions[0]

    assert position.underlying == "ETH"
    assert position.contract_type == "put_options"

    assert store.has_consumed_signal(eth.episode_key)
    assert btc.episode_key == eth.episode_key


def test_existing_call_blocks_later_call(
    tmp_path: Path,
) -> None:
    session, _ = _session(tmp_path)

    first = _signal(
        underlying="BTC",
        decision=V32SignalDecision.CALL,
    )

    first_result = process_v32_signal(
        first,
        (
            _snapshot(
                underlying="BTC",
                contract_type="call_options",
                product_id=21,
            ),
        ),
        session=session,
        entries_enabled=True,
        observed_ns=(first.candle_close_ms * 1_000_000),
    )

    assert first_result.status is (PaperSignalEntryStatus.OPENED)

    later = _signal(
        underlying="ETH",
        decision=V32SignalDecision.CALL,
        candle_close_ms=(BASE_CANDLE_CLOSE_MS + 300_000),
    )

    later_result = process_v32_signal(
        later,
        (
            _snapshot(
                underlying="ETH",
                contract_type="call_options",
                product_id=22,
            ),
        ),
        session=session,
        entries_enabled=True,
        observed_ns=(later.candle_close_ms * 1_000_000),
    )

    assert later_result.status is (PaperSignalEntryStatus.DIRECTION_BLOCKED)
    assert len(session.ledger.open_positions) == 1


def test_existing_put_blocks_later_put(
    tmp_path: Path,
) -> None:
    session, _ = _session(tmp_path)

    first = _signal(
        underlying="BTC",
        decision=V32SignalDecision.PUT,
    )

    first_result = process_v32_signal(
        first,
        (
            _snapshot(
                underlying="BTC",
                contract_type="put_options",
                product_id=31,
            ),
        ),
        session=session,
        entries_enabled=True,
        observed_ns=(first.candle_close_ms * 1_000_000),
    )

    assert first_result.status is (PaperSignalEntryStatus.OPENED)

    later = _signal(
        underlying="ETH",
        decision=V32SignalDecision.PUT,
        candle_close_ms=(BASE_CANDLE_CLOSE_MS + 300_000),
    )

    later_result = process_v32_signal(
        later,
        (
            _snapshot(
                underlying="ETH",
                contract_type="put_options",
                product_id=32,
            ),
        ),
        session=session,
        entries_enabled=True,
        observed_ns=(later.candle_close_ms * 1_000_000),
    )

    assert later_result.status is (PaperSignalEntryStatus.DIRECTION_BLOCKED)
    assert len(session.ledger.open_positions) == 1


def test_call_signal_never_selects_put(
    tmp_path: Path,
) -> None:
    session, _ = _session(tmp_path)

    signal = _signal(
        underlying="BTC",
        decision=V32SignalDecision.CALL,
    )

    result = process_v32_signal(
        signal,
        (
            _snapshot(
                underlying="BTC",
                contract_type="put_options",
                product_id=41,
            ),
        ),
        session=session,
        entries_enabled=True,
        observed_ns=(signal.candle_close_ms * 1_000_000),
    )

    assert result.status is (PaperSignalEntryStatus.NO_CALL_PROPOSAL)
    assert result.position is None
    assert session.ledger.open_positions == ()


def test_put_signal_never_selects_call(
    tmp_path: Path,
) -> None:
    session, _ = _session(tmp_path)

    signal = _signal(
        underlying="BTC",
        decision=V32SignalDecision.PUT,
    )

    result = process_v32_signal(
        signal,
        (
            _snapshot(
                underlying="BTC",
                contract_type="call_options",
                product_id=51,
            ),
        ),
        session=session,
        entries_enabled=True,
        observed_ns=(signal.candle_close_ms * 1_000_000),
    )

    assert result.status is (PaperSignalEntryStatus.NO_PUT_PROPOSAL)
    assert result.position is None
    assert session.ledger.open_positions == ()


def test_revalidation_rejection_falls_through_to_next_candidate(
    tmp_path: Path,
) -> None:
    session, store = _session(tmp_path)

    btc = _signal(
        underlying="BTC",
        decision=V32SignalDecision.CALL,
    )
    eth = _signal(
        underlying="ETH",
        decision=V32SignalDecision.CALL,
    )

    # BTC is initially ranked first because its spread is
    # tighter. Revalidation rejects BTC, so ETH must be
    # allowed to become the episode winner.
    btc_snapshot = _snapshot(
        underlying="BTC",
        contract_type="call_options",
        product_id=101,
        bid="995",
        ask="1000",
    )
    eth_snapshot = _snapshot(
        underlying="ETH",
        contract_type="call_options",
        product_id=102,
        bid="980",
        ask="1000",
    )

    seen: list[str] = []

    def revalidate(
        signal: V32Signal,
        proposal: PaperEntryProposal,
    ) -> PaperEntryProposal | None:
        seen.append(signal.underlying)

        if signal.underlying == "BTC":
            return None

        return proposal

    results = process_v32_signals(
        (btc, eth),
        (btc_snapshot, eth_snapshot),
        session=session,
        entries_enabled=True,
        observed_ns=(
            BASE_CANDLE_CLOSE_MS * 1_000_000
        ),
        candidate_revalidator=revalidate,
    )

    assert results[0].status is (
        PaperSignalEntryStatus.REVALIDATION_REJECTED
    )
    assert results[1].status is (
        PaperSignalEntryStatus.OPENED
    )

    assert seen == ["BTC", "ETH"]

    assert len(session.ledger.open_positions) == 1
    assert (
        session.ledger.open_positions[0].underlying
        == "ETH"
    )

    assert btc.episode_key == eth.episode_key
    assert store.has_consumed_signal(
        eth.episode_key
    )
