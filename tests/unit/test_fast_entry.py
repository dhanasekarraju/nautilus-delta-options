from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import nautilus_delta_options.paper.fast_entry as fast_entry
from nautilus_delta_options.delta.models import (
    DeltaOptionContractType,
    DeltaOptionTicker,
)
from nautilus_delta_options.delta.product import DeltaOptionProduct
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaOptionProductsSnapshot,
    DeltaUnderlying,
)
from nautilus_delta_options.paper.persistence import (
    SQLitePaperLedgerStore,
)
from nautilus_delta_options.paper.proposals import (
    PaperProposalConfig,
)
from nautilus_delta_options.paper.session import (
    PaperLedgerSession,
)
from nautilus_delta_options.paper.signal_entries_v32 import (
    PaperSignalEntryStatus,
)
from nautilus_delta_options.selection.entry_safety import (
    EntrySafetyConfig,
)
from nautilus_delta_options.signals.binance import (
    BinanceCandleSnapshot,
)
from nautilus_delta_options.signals.v32 import (
    V32Signal,
    V32SignalDecision,
)

AS_OF = date(2026, 8, 28)
BASE_CANDLE_CLOSE_MS = 1_787_887_499_999


def _session(
    tmp_path: Path,
) -> tuple[
    PaperLedgerSession,
    SQLitePaperLedgerStore,
]:
    store = SQLitePaperLedgerStore(
        tmp_path / "paper-fast-entry.sqlite"
    )

    session = PaperLedgerSession.load_or_create(
        store,
        initial_cash=Decimal("250"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )

    return session, store


def _proposal_config() -> PaperProposalConfig:
    return PaperProposalConfig(
        entry_safety=EntrySafetyConfig(
            max_signal_age_seconds=Decimal("15"),
        ),
    )


def _signal(
    *,
    underlying: DeltaUnderlying,
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


def _candle_snapshot(
    underlying: DeltaUnderlying,
    candle_close_ms: int,
) -> BinanceCandleSnapshot:
    return BinanceCandleSnapshot(
        underlying=underlying,
        symbol=f"{underlying}USDT",
        interval="5m",
        captured_ms=candle_close_ms + 1,
        candles=(),
    )


class _SignalClient:
    def __init__(
        self,
        closes: dict[DeltaUnderlying, int],
    ) -> None:
        self._closes = closes
        self.calls: list[DeltaUnderlying] = []

    def fetch_v31_candles(
        self,
        underlying: DeltaUnderlying,
    ) -> BinanceCandleSnapshot:
        self.calls.append(underlying)

        return _candle_snapshot(
            underlying,
            self._closes[underlying],
        )


class _NoDeltaClient:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_option_products(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionProductsSnapshot:
        self.calls += 1
        raise AssertionError(
            f"Delta products must not be fetched for {underlying}"
        )

    def fetch_option_chain(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionChainSnapshot:
        self.calls += 1
        raise AssertionError(
            f"Delta chain must not be fetched for {underlying}"
        )


def _product(
    *,
    underlying: DeltaUnderlying,
    contract_type: DeltaOptionContractType,
    product_id: int,
) -> DeltaOptionProduct:
    prefix = (
        "C"
        if contract_type == "call_options"
        else "P"
    )

    symbol = (
        f"{prefix}-{underlying}-80000-300826"
    )

    return DeltaOptionProduct(
        product_id=product_id,
        symbol=symbol,
        contract_type=contract_type,
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


def _ticker(
    *,
    underlying: DeltaUnderlying,
    contract_type: DeltaOptionContractType,
    product_id: int,
    bid: str,
    observed_ns: int,
    ask: str = "1000",
) -> DeltaOptionTicker:
    prefix = (
        "C"
        if contract_type == "call_options"
        else "P"
    )

    symbol = (
        f"{prefix}-{underlying}-80000-300826"
    )

    return DeltaOptionTicker(
        product_id=product_id,
        symbol=symbol,
        underlying=underlying,
        contract_type=contract_type,
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
        delta=(
            Decimal("0.5")
            if contract_type == "call_options"
            else Decimal("-0.5")
        ),
        gamma=Decimal("0.0001"),
        theta=Decimal("-10"),
        rho=Decimal("1"),
        vega=Decimal("20"),
        open_interest_contracts=Decimal("100"),
        volume=Decimal("10"),
        exchange_timestamp=(
            observed_ns - 1_000_000_000
        )
        // 1_000,
        trading_status="operational",
    )


class _DeltaClient:
    def __init__(
        self,
        *,
        observed_ns: int,
        contract_types: dict[
            DeltaUnderlying,
            DeltaOptionContractType,
        ],
        bids: dict[DeltaUnderlying, str] | None = None,
        refresh_bids: dict[DeltaUnderlying, str] | None = None,
        refresh_asks: dict[DeltaUnderlying, str] | None = None,
    ) -> None:
        self._observed_ns = observed_ns
        self._contract_types = contract_types
        self._bids = bids or {
            "BTC": "995",
            "ETH": "995",
        }
        self._refresh_bids = refresh_bids or self._bids
        self._refresh_asks = refresh_asks or {
            "BTC": "1000",
            "ETH": "1000",
        }

        self.product_calls: list[DeltaUnderlying] = []
        self.chain_calls: list[DeltaUnderlying] = []
        self.ticker_calls: list[tuple[str, ...]] = []

    @staticmethod
    def _product_id(
        underlying: DeltaUnderlying,
    ) -> int:
        return 1 if underlying == "BTC" else 2

    def fetch_option_tickers(
        self,
        symbols: Sequence[str],
    ) -> tuple[DeltaOptionTicker, ...]:
        if len(symbols) != 1:
            raise AssertionError(
                "Fast-entry final refresh expects "
                "exactly one option symbol"
            )

        requested = tuple(symbols)
        self.ticker_calls.append(requested)

        symbol = requested[0]

        underlying: DeltaUnderlying

        if "-BTC-" in symbol:
            underlying = "BTC"
        elif "-ETH-" in symbol:
            underlying = "ETH"
        else:
            raise AssertionError(
                f"Unexpected option symbol: {symbol}"
            )

        ticker = _ticker(
            underlying=underlying,
            contract_type=(
                self._contract_types[underlying]
            ),
            product_id=self._product_id(
                underlying
            ),
            bid=self._refresh_bids[underlying],
            observed_ns=self._observed_ns,
            ask=self._refresh_asks[underlying],
        )

        if ticker.symbol != symbol:
            raise AssertionError(
                "Targeted refresh returned wrong symbol: "
                f"expected={symbol}, actual={ticker.symbol}"
            )

        return (ticker,)

    def fetch_option_products(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionProductsSnapshot:
        self.product_calls.append(underlying)

        return DeltaOptionProductsSnapshot(
            underlying=underlying,
            products=(
                _product(
                    underlying=underlying,
                    contract_type=(
                        self._contract_types[underlying]
                    ),
                    product_id=self._product_id(
                        underlying
                    ),
                ),
            ),
            rejected_records=(),
        )

    def fetch_option_chain(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionChainSnapshot:
        self.chain_calls.append(underlying)

        return DeltaOptionChainSnapshot(
            underlying=underlying,
            tickers=(
                _ticker(
                    underlying=underlying,
                    contract_type=(
                        self._contract_types[underlying]
                    ),
                    product_id=self._product_id(
                        underlying
                    ),
                    bid=self._bids[underlying],
                    observed_ns=self._observed_ns,
                ),
            ),
            rejected_records=(),
        )


def _install_signals(
    monkeypatch: pytest.MonkeyPatch,
    signals: dict[DeltaUnderlying, V32Signal],
) -> None:
    def fake_evaluate(
        snapshot: BinanceCandleSnapshot,
    ) -> V32Signal:
        return signals[snapshot.underlying]

    monkeypatch.setattr(
        fast_entry,
        "evaluate_v32_signal",
        fake_evaluate,
    )


def test_repeated_completed_candle_skips_delta_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = _session(tmp_path)

    signals = {
        "BTC": _signal(
            underlying="BTC",
            decision=V32SignalDecision.CALL,
        ),
        "ETH": _signal(
            underlying="ETH",
            decision=V32SignalDecision.NONE,
        ),
    }

    _install_signals(monkeypatch, signals)

    signal_client = _SignalClient(
        {
            "BTC": BASE_CANDLE_CLOSE_MS,
            "ETH": BASE_CANDLE_CLOSE_MS,
        }
    )
    delta_client = _NoDeltaClient()

    cycle = fast_entry.run_fast_entry_cycle(
        delta_client=delta_client,
        signal_client=signal_client,
        session=session,
        entries_enabled=True,
        proposal_config=_proposal_config(),
        previous_candle_close_ms=BASE_CANDLE_CLOSE_MS,
    )

    assert cycle.candle_close_ms == BASE_CANDLE_CLOSE_MS
    assert cycle.entry_results == ()
    assert cycle.warnings == ()
    assert delta_client.calls == 0
    assert session.ledger.open_positions == ()


def test_misaligned_completed_candles_do_not_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = _session(tmp_path)

    signals = {
        "BTC": _signal(
            underlying="BTC",
            decision=V32SignalDecision.CALL,
            candle_close_ms=BASE_CANDLE_CLOSE_MS,
        ),
        "ETH": _signal(
            underlying="ETH",
            decision=V32SignalDecision.NONE,
            candle_close_ms=(
                BASE_CANDLE_CLOSE_MS + 300_000
            ),
        ),
    }

    _install_signals(monkeypatch, signals)

    signal_client = _SignalClient(
        {
            "BTC": BASE_CANDLE_CLOSE_MS,
            "ETH": (
                BASE_CANDLE_CLOSE_MS + 300_000
            ),
        }
    )
    delta_client = _NoDeltaClient()

    cycle = fast_entry.run_fast_entry_cycle(
        delta_client=delta_client,
        signal_client=signal_client,
        session=session,
        entries_enabled=True,
        proposal_config=_proposal_config(),
    )

    assert cycle.candle_close_ms is None
    assert cycle.entry_results == ()
    assert len(cycle.warnings) == 1
    assert "not aligned" in cycle.warnings[0]
    assert delta_client.calls == 0
    assert session.ledger.open_positions == ()


def test_wait_only_candle_skips_delta_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = _session(tmp_path)

    signals = {
        "BTC": _signal(
            underlying="BTC",
            decision=V32SignalDecision.NONE,
        ),
        "ETH": _signal(
            underlying="ETH",
            decision=V32SignalDecision.NONE,
        ),
    }

    _install_signals(monkeypatch, signals)

    signal_client = _SignalClient(
        {
            "BTC": BASE_CANDLE_CLOSE_MS,
            "ETH": BASE_CANDLE_CLOSE_MS,
        }
    )
    delta_client = _NoDeltaClient()

    cycle = fast_entry.run_fast_entry_cycle(
        delta_client=delta_client,
        signal_client=signal_client,
        session=session,
        entries_enabled=True,
        proposal_config=_proposal_config(),
    )

    assert [
        result.status
        for result in cycle.entry_results
    ] == [
        PaperSignalEntryStatus.WAIT,
        PaperSignalEntryStatus.WAIT,
    ]

    assert delta_client.calls == 0
    assert session.ledger.open_positions == ()


def test_signal_older_than_15_seconds_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, store = _session(tmp_path)

    btc = _signal(
        underlying="BTC",
        decision=V32SignalDecision.CALL,
    )
    eth = _signal(
        underlying="ETH",
        decision=V32SignalDecision.NONE,
    )

    _install_signals(
        monkeypatch,
        {
            "BTC": btc,
            "ETH": eth,
        },
    )

    observed_ns = (
        BASE_CANDLE_CLOSE_MS * 1_000_000
        + 16_000_000_000
    )

    delta_client = _DeltaClient(
        observed_ns=observed_ns,
        contract_types={
            "BTC": "call_options",
            "ETH": "call_options",
        },
    )

    cycle = fast_entry.run_fast_entry_cycle(
        delta_client=delta_client,
        signal_client=_SignalClient(
            {
                "BTC": BASE_CANDLE_CLOSE_MS,
                "ETH": BASE_CANDLE_CLOSE_MS,
            }
        ),
        session=session,
        entries_enabled=True,
        proposal_config=_proposal_config(),
        clock_ns=lambda: observed_ns,
        as_of=AS_OF,
    )

    assert cycle.entry_results[0].status is (
        PaperSignalEntryStatus.STALE_SIGNAL
    )
    assert cycle.entry_results[1].status is (
        PaperSignalEntryStatus.WAIT
    )

    assert session.ledger.open_positions == ()
    assert not store.has_consumed_signal(
        btc.episode_key
    )


def test_signal_at_15_seconds_remains_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = _session(tmp_path)

    _install_signals(
        monkeypatch,
        {
            "BTC": _signal(
                underlying="BTC",
                decision=V32SignalDecision.CALL,
            ),
            "ETH": _signal(
                underlying="ETH",
                decision=V32SignalDecision.NONE,
            ),
        },
    )

    observed_ns = (
        BASE_CANDLE_CLOSE_MS * 1_000_000
        + 15_000_000_000
    )

    delta_client = _DeltaClient(
        observed_ns=observed_ns,
        contract_types={
            "BTC": "call_options",
            "ETH": "call_options",
        },
    )

    cycle = fast_entry.run_fast_entry_cycle(
        delta_client=delta_client,
        signal_client=_SignalClient(
            {
                "BTC": BASE_CANDLE_CLOSE_MS,
                "ETH": BASE_CANDLE_CLOSE_MS,
            }
        ),
        session=session,
        entries_enabled=True,
        proposal_config=_proposal_config(),
        clock_ns=lambda: observed_ns,
        as_of=AS_OF,
    )

    assert cycle.entry_results[0].status is (
        PaperSignalEntryStatus.OPENED
    )
    assert cycle.entry_results[1].status is (
        PaperSignalEntryStatus.WAIT
    )

    assert len(session.ledger.open_positions) == 1
    assert (
        session.ledger.open_positions[0].underlying
        == "BTC"
    )


def test_same_candle_correlation_arbitration_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    _install_signals(
        monkeypatch,
        {
            "BTC": btc,
            "ETH": eth,
        },
    )

    observed_ns = (
        BASE_CANDLE_CLOSE_MS * 1_000_000
        + 2_000_000_000
    )

    delta_client = _DeltaClient(
        observed_ns=observed_ns,
        contract_types={
            "BTC": "call_options",
            "ETH": "call_options",
        },
        bids={
            "BTC": "995",
            "ETH": "980",
        },
    )

    cycle = fast_entry.run_fast_entry_cycle(
        delta_client=delta_client,
        signal_client=_SignalClient(
            {
                "BTC": BASE_CANDLE_CLOSE_MS,
                "ETH": BASE_CANDLE_CLOSE_MS,
            }
        ),
        session=session,
        entries_enabled=True,
        proposal_config=_proposal_config(),
        clock_ns=lambda: observed_ns,
        as_of=AS_OF,
    )

    assert cycle.entry_results[0].status is (
        PaperSignalEntryStatus.OPENED
    )
    assert cycle.entry_results[1].status is (
        PaperSignalEntryStatus.CORRELATED_SIGNAL_SKIPPED
    )

    assert len(session.ledger.open_positions) == 1
    assert (
        session.ledger.open_positions[0].underlying
        == "BTC"
    )

    assert btc.episode_key == eth.episode_key
    assert store.has_consumed_signal(
        btc.episode_key
    )



def test_fresh_premium_jump_is_rejected_without_chasing_payoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, store = _session(tmp_path)

    btc = _signal(
        underlying="BTC",
        decision=V32SignalDecision.CALL,
    )
    eth = _signal(
        underlying="ETH",
        decision=V32SignalDecision.NONE,
    )

    _install_signals(
        monkeypatch,
        {
            "BTC": btc,
            "ETH": eth,
        },
    )

    observed_ns = (
        BASE_CANDLE_CLOSE_MS * 1_000_000
        + 2_000_000_000
    )

    delta_client = _DeltaClient(
        observed_ns=observed_ns,
        contract_types={
            "BTC": "call_options",
            "ETH": "call_options",
        },
        # Initial proposal:
        # bid 995 / ask 1000.
        bids={
            "BTC": "995",
            "ETH": "995",
        },
        # Immediately before opening, BTC premium has
        # already run to bid 1095 / ask 1100.
        refresh_bids={
            "BTC": "1095",
            "ETH": "995",
        },
        refresh_asks={
            "BTC": "1100",
            "ETH": "1000",
        },
    )

    cycle = fast_entry.run_fast_entry_cycle(
        delta_client=delta_client,
        signal_client=_SignalClient(
            {
                "BTC": BASE_CANDLE_CLOSE_MS,
                "ETH": BASE_CANDLE_CLOSE_MS,
            }
        ),
        session=session,
        entries_enabled=True,
        proposal_config=_proposal_config(),
        clock_ns=lambda: observed_ns,
        as_of=AS_OF,
    )

    btc_result = cycle.entry_results[0]

    assert btc_result.status is (
        PaperSignalEntryStatus.REVALIDATION_REJECTED
    )

    # The originally approved payoff structure was based
    # on ask=1000. Those levels must NOT move upward just
    # because the fresh premium has already run to 1100.
    assert btc_result.proposal is not None
    assert (
        btc_result.proposal.record.ticker.best_ask
        == Decimal("1000")
    )
    assert (
        btc_result.proposal.levels.stop_exit_bid
        == Decimal("900")
    )
    assert (
        btc_result.proposal.levels.target_exit_bid
        == Decimal("1200")
    )

    assert session.ledger.open_positions == ()

    # Rejected revalidation must not consume the episode.
    assert not store.has_consumed_signal(
        btc.episode_key
    )

    assert delta_client.ticker_calls == [
        ("C-BTC-80000-300826",),
    ]

    assert any(
        "final payoff revalidation rejected"
        in warning
        for warning in cycle.warnings
    )
