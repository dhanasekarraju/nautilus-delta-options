import asyncio
import json
import random
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from test_audit_remediation import NOW, open_position, session_at
from test_paper_ledger import AS_OF
from test_payoff_profiles import admitted, quote, run
from test_rc_v34_entry import Market, cycle, signal

from nautilus_delta_options.paper.payoff_profiles import PayoffProfile, initial_state
from nautilus_delta_options.paper.payoff_research import PayoffResearch
from nautilus_delta_options.paper.persistence import SQLitePaperLedgerStore
from nautilus_delta_options.paper.provenance import bind_provenance, make_provenance
from nautilus_delta_options.paper.v34_paper_live import run_v34_paper_entry_cycle
from nautilus_delta_options.web.lifecycle import run_blocking
from nautilus_delta_options.web.v34_paper_app import (
    V34PaperServiceState,
    _health_errors,
    _required_readiness,
    create_v34_paper_app,
)


def ready():
    stamp = datetime.now(UTC).isoformat()
    return V34PaperServiceState(
        started_at=stamp, last_signal_success_at=stamp,
        latest_fast_exit_at=stamp, latest_slow_exit_at=stamp,
    )


def enter(session, market, state):
    return run_v34_paper_entry_cycle(
        cycle=cycle([signal()]), delta_client=market, session=session,
        clock_ns=lambda: NOW, as_of=AS_OF,
        readiness_guard=lambda: _required_readiness(state),
    )


@pytest.mark.parametrize("loop", ["signal_warnings", "fast_exit_warnings", "slow_exit_warnings"])
def test_health_lost_during_exact_refresh_rejects_without_any_mutation(tmp_path, loop):
    state = ready()
    assert not _health_errors(state)
    path = tmp_path / "primary.sqlite"
    session = session_at(path)
    research = PayoffResearch(tmp_path / "research.sqlite")
    before = session.snapshot()
    research.sync_entries(before)
    original_events = research.events()

    class DeterioratingMarket(Market):
        def fetch_option_tickers(self, symbols):
            value = super().fetch_option_tickers(symbols)
            if self.calls == 2:
                with state.admission_lock:
                    setattr(state, loop, ("required loop failed during refresh",))
            return value

    with pytest.raises(ValueError, match="readiness failed"):
        enter(session, DeterioratingMarket(), state)
    assert session.snapshot().cash == before.cash
    assert session.snapshot().open_positions == before.open_positions
    assert session.snapshot().closed_trades == before.closed_trades
    assert session_at(path).snapshot().cash == before.cash
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM paper_signal_receipts").fetchone()[0] == 0
    research.sync_entries(session.snapshot())
    assert research.events() == original_events
    assert research.summary()["profiles"] == []


@pytest.mark.parametrize("failure", ["busy", "full", "io", "commit"])
def test_transaction_failure_rolls_back_receipt_snapshot_and_memory(tmp_path, monkeypatch, failure):
    path = tmp_path / "paper.sqlite"
    session = session_at(path)
    before = session.snapshot()
    original = SQLitePaperLedgerStore._connect
    held = sqlite3.connect(path)
    if failure == "busy":
        held.execute("BEGIN IMMEDIATE")

    class Fault(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "INSERT INTO paper_ledger_state" in sql and failure != "commit":
                raise sqlite3.OperationalError(
                    "database or disk is full" if failure == "full" else "disk I/O error"
                )
            return super().execute(sql, parameters)

        def commit(self):
            if failure == "commit":
                raise sqlite3.OperationalError("disk full at commit")
            return super().commit()

    def connection(store):
        if failure == "busy":
            return sqlite3.connect(store.path, timeout=0.01)
        return sqlite3.connect(store.path, factory=Fault)

    monkeypatch.setattr(SQLitePaperLedgerStore, "_connect", connection)
    try:
        with pytest.raises(sqlite3.OperationalError):
            enter(session, Market(), ready())
    finally:
        held.rollback()
        held.close()
    assert session.snapshot().cash == before.cash
    assert session.snapshot().open_positions == ()
    monkeypatch.setattr(SQLitePaperLedgerStore, "_connect", original)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM paper_signal_receipts").fetchone()[0] == 0
    restored = session_at(path)
    assert restored.snapshot().cash == before.cash
    assert enter(restored, Market(), ready()).results[0].status.value == "opened"
    replay = enter(session_at(path), Market(), ready())
    assert replay.results[0].status.value == "already_consumed"


def test_shutdown_during_blocking_refresh_cannot_admit(tmp_path):
    state = ready()
    session = session_at(tmp_path / "paper.sqlite")
    started, release = threading.Event(), threading.Event()

    class WaitingMarket(Market):
        def fetch_option_tickers(self, symbols):
            value = super().fetch_option_tickers(symbols)
            if self.calls == 2:
                started.set()
                assert release.wait(3)
            return value

    async def scenario():
        task = asyncio.create_task(run_blocking(enter, session, WaitingMarket(), state))
        assert await asyncio.to_thread(started.wait, 3)
        with state.admission_lock:
            state.stopping.set()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert session.snapshot().cash == Decimal("100")
    assert session.snapshot().open_positions == ()
    assert session_at(tmp_path / "paper.sqlite").snapshot().open_positions == ()


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "relative"])
def test_database_aliases_rejected_before_mutation(tmp_path, monkeypatch, alias):
    path = tmp_path / "paper.sqlite"
    session_at(path)
    other = tmp_path / "other.sqlite"
    if alias == "symlink":
        other.symlink_to(path)
    elif alias == "hardlink":
        other.hardlink_to(path)
    else:
        other = tmp_path / "child" / ".." / "paper.sqlite"
        (tmp_path / "child").mkdir()
    before = path.read_bytes()
    monkeypatch.setenv("V34_PAYOFF_DATABASE", str(other))
    with pytest.raises(ValueError, match="distinct paths"):
        create_v34_paper_app(database=path)
    assert path.read_bytes() == before


def test_wall_clock_backward_and_monotonic_health_expiry():
    state = ready()
    state.latest_fast_exit_at = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
    assert _health_errors(state)
    state = ready()
    state.loop_monotonic["fast exit"] = time.monotonic() - 61
    assert _health_errors(state)


def test_provenance_restart_and_configuration_incompatibility(tmp_path):
    path = tmp_path / "paper.sqlite"
    session_at(path)
    config = {"profile": "baseline_v34", "penalty": "0"}
    first, second = make_provenance(config), make_provenance(config)
    assert first["run_id"] != second["run_id"]
    assert first["fingerprint"] == second["fingerprint"]
    bind_provenance(path, first)
    bind_provenance(path, second)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM validation_runs").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM validation_identity").fetchone()[0] == 1
    incompatible = make_provenance({**config, "penalty": "1"})
    with pytest.raises(ValueError, match="identity changed"):
        bind_provenance(path, incompatible)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM validation_runs").fetchone()[0] == 2


def test_unknown_historical_trades_are_not_relabelled(tmp_path):
    path = tmp_path / "paper.sqlite"
    open_position(session_at(path))
    with pytest.raises(ValueError, match="Legacy trades"):
        bind_provenance(path, make_provenance({}))
    with sqlite3.connect(path) as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='validation_identity'"
        ).fetchone()


def test_provenance_is_atomic_entry_data(tmp_path):
    path = tmp_path / "paper.sqlite"
    session = session_at(path)
    # Public factory binds the store; use its underlying store in this isolated unit test.
    provenance = make_provenance({"profile": "baseline_v34"})
    session._store.run_provenance = provenance
    enter(session, Market(), ready())
    position = session_at(path).snapshot().open_positions[0]
    recorded = json.loads(position.entry_observation)["provenance"]
    assert recorded["run_id"] == provenance["run_id"]
    assert recorded["fingerprint"] == provenance["fingerprint"]


@pytest.mark.parametrize("penalty", [Decimal("1000"), Decimal("2000")])
def test_no_synthetic_zero_fill_or_initial_excursion(penalty):
    _, position = admitted()
    state = initial_state(position, PayoffProfile.EXPERIMENTAL, penalty=penalty)
    assert state.mfe is None and state.mae is None and state.best_net is None
    after, event = run(position, state, quote(900), penalty=penalty)
    assert after == state
    assert event["executable_exit_price"] is None
    assert event["exit_trigger"] is None


def test_seeded_payoff_invariants_for_100_market_paths():
    rng = random.Random(3492)
    _, position = admitted()
    for _ in range(100):
        state = initial_state(position, PayoffProfile.EXPERIMENTAL)
        for step in range(1, 40):
            before = state
            bid = Decimal(rng.randint(400, 2500))
            thin = rng.randrange(4) == 0
            ticker = quote(bid, step, bid_size=Decimal("0") if thin else Decimal("100"))
            state, event = run(position, state, ticker, penalty=Decimal("2"))
            assert state.risk == position.planned_loss
            assert state.stop >= before.stop
            if thin:
                assert state == before
                assert event["exit_trigger"] is None
                continue
            if state.activated and not before.activated:
                assert Decimal(event["net_pnl"]) >= state.risk
            if state.closed:
                assert Decimal(event["executable_exit_price"]) == bid - 2 > 0
                break


def test_seeded_baseline_cash_conservation_and_restart(tmp_path):
    rng = random.Random(492)
    path = tmp_path / "paper.sqlite"
    ids = []
    for _ in range(30):
        session = session_at(path)
        position = open_position(session)
        bid = rng.choice([450, 800, 1300, 2000])
        trade = session.process_exit_ticker(
            position.trade_id, quote(bid), observed_ns=NOW + 1_000_000_000,
        )
        assert trade is not None
        ids.append(trade.position.trade_id)
        ledger = session_at(path).snapshot()
        assert ledger.cash == ledger.initial_cash + ledger.realized_pnl
        assert trade.net_pnl == trade.gross_pnl - trade.position.entry_fee - trade.exit_fee
    assert len(ids) == len(set(ids))


def test_post_settlement_event_with_small_clock_skew_is_not_executable(tmp_path):
    from dataclasses import replace

    session = session_at(tmp_path / "paper.sqlite")
    position = open_position(session)
    ticker = replace(quote(1400), exchange_timestamp=position.settlement_ns // 1000)
    receipt_ns = position.settlement_ns - 1_000_000_000
    with pytest.raises(ValueError, match="verified settlement"):
        session.process_exit_ticker(position.trade_id, ticker, observed_ns=receipt_ns)
    assert session.snapshot().open_positions == (position,)
    from nautilus_delta_options.paper.payoff_profiles import observe
    state = initial_state(position, PayoffProfile.EXPERIMENTAL)
    after, event = observe(position, state, ticker, observed_ns=receipt_ns)
    assert after == state
    assert event["exit_trigger"] is None


def test_signal_loop_failure_stays_unready_until_successful_recovery(tmp_path, monkeypatch):
    import nautilus_delta_options.web.v34_paper_app as module

    state = ready()
    seen = []

    class Observer:
        calls = 0

        def run_cycle(self):
            self.calls += 1
            if self.calls < 3:
                raise TimeoutError("public API deadline")
            return cycle([signal()])

    class Finished(Exception):
        pass

    async def checkpoint(_):
        seen.append(_health_errors(state))
        if len(seen) == 3:
            raise Finished

    monkeypatch.setattr(module.asyncio, "sleep", checkpoint)
    with pytest.raises(Finished):
        asyncio.run(module._poll_v34_signals(
            observer=Observer(), delta_client=Market(),
            session=session_at(tmp_path / "paper.sqlite"), state=state,
            entries_enabled=False, interval_seconds=5,
        ))
    assert seen[0] and seen[1] and not seen[2]


def test_exit_commit_failure_rolls_back_then_restart_can_close(tmp_path, monkeypatch):
    path = tmp_path / "paper.sqlite"
    session = session_at(path)
    position = open_position(session)
    original = SQLitePaperLedgerStore._connect

    class CommitFailure(sqlite3.Connection):
        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.rollback()
                raise sqlite3.OperationalError("disk full at commit")
            return super().__exit__(exc_type, exc, tb)

    monkeypatch.setattr(
        SQLitePaperLedgerStore, "_connect",
        lambda store: sqlite3.connect(store.path, factory=CommitFailure),
    )
    with pytest.raises(sqlite3.OperationalError, match="commit"):
        session.process_exit_ticker(
            position.trade_id, quote(400), observed_ns=NOW + 1_000_000_000,
        )
    assert session.snapshot().open_positions == (position,)
    monkeypatch.setattr(SQLitePaperLedgerStore, "_connect", original)
    restored = session_at(path)
    assert restored.snapshot().open_positions == (position,)
    trade = restored.process_exit_ticker(
        position.trade_id, quote(400), observed_ns=NOW + 1_000_000_000,
    )
    assert trade.exit_price == Decimal("400")
    assert len(session_at(path).snapshot().closed_trades) == 1
