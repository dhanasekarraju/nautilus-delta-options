import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import Response
from test_app_fast_entry_wiring import _route_endpoint
from test_audit_remediation import NOW, open_position, session_at
from test_payoff_profiles import quote

from nautilus_delta_options.paper.reconcile import reconcile
from nautilus_delta_options.web.lifecycle import DatabaseLease, run_blocking
from nautilus_delta_options.web.v34_paper_app import (
    V34PaperServiceState,
    _health_errors,
    create_v34_paper_app,
)


def test_database_lease_excludes_second_service_and_releases(tmp_path):
    path = tmp_path / "paper.sqlite"
    with (
        DatabaseLease(path),
        pytest.raises(RuntimeError, match="Another service"),
        DatabaseLease(path),
    ):
        pytest.fail("second owner admitted")
    with DatabaseLease(path):
        pass


def test_cancellation_drains_worker_even_after_repeated_cancel():
    async def scenario():
        started, finished, release = threading.Event(), threading.Event(), threading.Event()

        def work():
            started.set()
            assert release.wait(timeout=3)
            finished.set()

        task = asyncio.create_task(run_blocking(work))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, 0.0])
def test_explicit_polling_intervals_are_validated(tmp_path, bad):
    with pytest.raises(ValueError, match="finite and positive"):
        create_v34_paper_app(database=tmp_path / "paper.sqlite", fast_exit_interval_seconds=bad)
    assert not (tmp_path / "paper.sqlite").exists()


def test_research_cannot_share_primary_file(tmp_path, monkeypatch):
    path = tmp_path / "paper.sqlite"
    monkeypatch.setenv("V34_PAYOFF_DATABASE", str(path))
    with pytest.raises(ValueError, match="distinct paths"):
        create_v34_paper_app(database=path)
    assert not path.exists()


def test_actual_asgi_health_returns_503_without_successful_loops(tmp_path):
    app = create_v34_paper_app(database=tmp_path / "paper.sqlite")

    async def request():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/health",
                "raw_path": b"/health",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "server": ("test", 80),
                "client": ("test", 1),
            },
            receive,
            send,
        )
        return messages

    messages = asyncio.run(request())
    assert messages[0]["status"] == 503
    payload = json.loads(b"".join(m.get("body", b"") for m in messages))
    assert payload["real_orders_enabled"] is False
    assert payload["status"] == "error"


def test_service_lifespan_owns_both_databases_without_network(tmp_path, monkeypatch):
    import nautilus_delta_options.web.v34_paper_app as module

    started = []

    async def idle(**kwargs):
        started.append(True)
        await asyncio.Event().wait()

    for name in ("_poll_v34_signals", "_poll_fast_exits", "_poll_slow_exits"):
        monkeypatch.setattr(module, name, idle)
    path = tmp_path / "primary.sqlite"
    app = create_v34_paper_app(database=path)

    async def scenario():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            assert len(started) == 3
            for database in (path, tmp_path / "primary.sqlite.payoff.sqlite"):
                with pytest.raises(RuntimeError), DatabaseLease(database):
                    pytest.fail("unleased active database")
        with DatabaseLease(path):
            pass

    asyncio.run(scenario())


def test_startup_rejects_snapshot_changed_after_factory(tmp_path, monkeypatch):
    import nautilus_delta_options.web.v34_paper_app as module

    async def forbidden(**kwargs):
        pytest.fail("polling started from stale snapshot")

    for name in ("_poll_v34_signals", "_poll_fast_exits", "_poll_slow_exits"):
        monkeypatch.setattr(module, name, forbidden)
    path = tmp_path / "paper.sqlite"
    monkeypatch.setenv("V34_PAPER_INITIAL_CASH", "100")
    app = create_v34_paper_app(database=path)
    open_position(session_at(path))

    async def scenario():
        with pytest.raises(RuntimeError, match="changed before startup"):
            async with app.router.lifespan_context(app):
                pytest.fail("stale startup admitted")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"best_bid": Decimal("1300"), "best_ask": Decimal("1200")},
        {"spot_price": Decimal("NaN")},
        {"bid_size": Decimal("Infinity")},
    ],
)
def test_invalid_exit_quote_cannot_mutate_primary_ledger(tmp_path, change):
    session = session_at(tmp_path / "paper.sqlite")
    position = open_position(session)
    before = session.snapshot()
    with pytest.raises(ValueError):
        session.process_exit_ticker(
            position.trade_id,
            replace(quote(1200), **change),
            observed_ns=NOW + 1_000_000_000,
        )
    assert session.snapshot().open_positions == before.open_positions
    assert session.snapshot().cash == before.cash


def test_post_expiry_quote_never_fabricates_primary_exit(tmp_path):
    session = session_at(tmp_path / "paper.sqlite")
    position = open_position(session)
    ticker = replace(quote(1200), exchange_timestamp=position.settlement_ns // 1000)
    with pytest.raises(ValueError, match="verified settlement"):
        session.process_exit_ticker(
            position.trade_id,
            ticker,
            observed_ns=position.settlement_ns,
            apply_time_policy=True,
        )
    assert session.snapshot().open_positions == (position,)


def test_reconciliation_reads_without_changing_primary_bytes(tmp_path):
    path = tmp_path / "paper.sqlite"
    session = session_at(path)
    open_position(session)
    before = path.read_bytes()
    result = reconcile(path)
    assert result["ok"]
    assert result["open_positions"] == 1
    assert path.read_bytes() == before


def test_reconciliation_detects_corrupt_cash_and_receipt(tmp_path):
    path = tmp_path / "paper.sqlite"
    session = session_at(path)
    open_position(session)
    with sqlite3.connect(path) as db:
        payload = json.loads(db.execute("SELECT payload FROM paper_ledger_state").fetchone()[0])
        payload["cash"] = "999"
        db.execute("UPDATE paper_ledger_state SET payload=?", (json.dumps(payload),))
        db.execute(
            "INSERT INTO paper_signal_receipts VALUES ('invalid', 'BTC', 1, 99, 1)",
        )
    result = reconcile(path)
    assert not result["ok"]
    assert any("cash" in issue for issue in result["issues"])
    assert any("receipt" in issue for issue in result["issues"])


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        reconcile(path)
    assert not path.exists()


def test_fresh_loop_health_and_deferred_exit_health():
    stamp = datetime.now(UTC).isoformat()
    state = V34PaperServiceState(
        started_at=stamp,
        last_signal_success_at=stamp,
        latest_fast_exit_at=stamp,
        latest_slow_exit_at=stamp,
    )
    assert _health_errors(state) == []
    state.fast_exit_warnings = ("unresolved quote",)
    assert _health_errors(state) == ["unresolved quote"]


def test_health_response_has_explicit_status(tmp_path):
    app = create_v34_paper_app(database=tmp_path / "paper.sqlite")
    response = Response()
    result = _route_endpoint(app, "/health")(response)
    assert result["status"] == "error"
    assert response.status_code == 503


@pytest.mark.parametrize("healthy", [False, True])
def test_entry_poller_requires_healthy_exit_loops(tmp_path, monkeypatch, healthy):
    import nautilus_delta_options.web.v34_paper_app as module
    from nautilus_delta_options.paper.v34_paper_live import V34PaperEntryCycle
    from nautilus_delta_options.paper.v34_shadow import V34ShadowCycle

    stamp = datetime.now(UTC).isoformat()
    state = V34PaperServiceState(
        started_at=stamp,
        last_signal_success_at=stamp,
        latest_fast_exit_at=stamp,
        latest_slow_exit_at=stamp,
        fast_exit_warnings=() if healthy else ("exit loop unavailable",),
    )
    calls = []

    class Observer:
        def run_cycle(self):
            return V34ShadowCycle(1, True, (), ())

    def entry(**kwargs):
        calls.append(True)
        return V34PaperEntryCycle(1, True, (), ())

    class StopPolling(Exception):
        pass

    async def stop(_):
        raise StopPolling

    monkeypatch.setattr(module, "run_v34_paper_entry_cycle", entry)
    monkeypatch.setattr(module.asyncio, "sleep", stop)
    with pytest.raises(StopPolling):
        asyncio.run(
            module._poll_v34_signals(
                observer=Observer(),
                delta_client=object(),
                session=session_at(tmp_path / "paper.sqlite"),
                state=state,
                entries_enabled=True,
                interval_seconds=1,
            )
        )
    assert bool(calls) is healthy
