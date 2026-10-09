from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import zlib
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from test_service import _history, _option_chain, _signal

from nautilus_delta_options.delta.history import DeltaCandle, DeltaCandleSnapshot
from nautilus_delta_options.research.experiment1 import capture, collector
from nautilus_delta_options.research.experiment1.archive import ArchivedMarketData, EvidenceArchive
from nautilus_delta_options.research.experiment1.capture import (
    CapturedHistorySnapshot,
    RawPublicResponse,
)
from nautilus_delta_options.research.experiment1.endpoints import build_endpoint_statements
from nautilus_delta_options.research.experiment1.journal import ExperimentJournal
from nautilus_delta_options.research.experiment1.service import enqueue_completed_evaluation
from nautilus_delta_options.signals.v34 import V34Config, V34Decision

START = 1_800_000_000 * 10**9


def manifest():
    return collector.make_manifest(
        "test-exp",
        "test-build",
        START,
        START + 300 * 10**9,
        START + 2400 * 10**9,
        b"sealed protocol",
        START - 10**9,
    )


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "observations.sqlite"
    m = manifest()
    collector.initialize(path, m, START - 10**9)
    return path, m


@pytest.mark.parametrize(
    "body",
    [
        b"invalid JSON",
        b"\xff",
        b'{"success":true,"result":[]}',
        b'{"success":false,"error":"unavailable"}',
    ],
)
def test_actual_failing_history_path_durably_preserves_bytes(database, monkeypatch, body):
    path, m = database
    monkeypatch.setattr(capture, "_RAW_WORKER", f"import sys;sys.stdout.buffer.write({body!r})")
    journal = ExperimentJournal(path)
    archive = EvidenceArchive(
        journal, m.experiment_id, "r", set(), m.endpoint_acquisition_deadline_utc_ns
    )
    market = ArchivedMarketData(
        archive,
        stop=threading.Event(),
        utc_ns=lambda: START,
        start_ns=START,
        end_ns=m.window_end_utc_ns,
    )
    with pytest.raises((ValueError, UnicodeError)):
        market.fetch_5m_candles("BTC", count=60, now_s=START // 10**9)
    assert journal.close()
    with sqlite3.connect(path) as con:
        data, digest = con.execute("SELECT body,sha256 FROM raw_payloads").fetchone()
        assert zlib.decompress(data) == body
        assert digest == hashlib.sha256(body).hexdigest()
        assert (
            con.execute(
                "SELECT count(*) FROM journal_events WHERE event_type='source_parse_failed'"
            ).fetchone()[0]
            == 1
        )
        assert con.execute("SELECT count(*) FROM population_bindings").fetchone()[0] == 0


@pytest.mark.parametrize("status", [429, 500])
def test_http_error_body_preserved_before_raise(monkeypatch, status):
    # Substitute transport worker, exercising real parent subprocess framing.
    body = b'{"error":"rate limit"}'
    meta = json.dumps({"status": status, "complete": True, "error": f"HTTP status {status}"})
    monkeypatch.setattr(
        capture,
        "_RAW_WORKER",
        f"import sys;sys.stdout.buffer.write({body!r});sys.stderr.write({meta!r})",
    )
    received = []
    with pytest.raises(RuntimeError, match="HTTP status"):
        capture.raw_delta_public_get(
            "https://api.india.delta.exchange/v2/tickers", timeout=2, on_response=received.append
        )
    assert received[0].body == body
    assert received[0].http_status == status
    assert received[0].body_complete
    assert received[0].transport_error


def test_timeout_retains_partial_body_and_kills_worker(monkeypatch):
    monkeypatch.setattr(
        capture,
        "_RAW_WORKER",
        "import sys,time;sys.stdout.buffer.write(b'partial');"
        "sys.stdout.buffer.flush();time.sleep(60)",
    )
    received = []
    with pytest.raises(TimeoutError):
        capture.raw_delta_public_get(
            "https://api.india.delta.exchange/v2/tickers", timeout=0.3, on_response=received.append
        )
    assert received[0].body == b"partial"
    assert not received[0].body_complete


def test_inflight_memory_remains_charged_until_commit(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = ExperimentJournal(
        path,
        create_new=True,
        max_queue_bytes=1000,
        flush_interval_seconds=0.01,
        busy_timeout_ms=2000,
    )
    with sqlite3.connect(path) as locked:
        locked.execute("BEGIN IMMEDIATE")
        assert journal.enqueue("SELECT ?", (b"a" * 700,))
        time.sleep(0.05)
        assert journal.stats().queued_bytes >= 700
        assert not journal.enqueue("SELECT ?", (b"b" * 700,))
        assert not journal.flush(0.03)
    assert journal.flush(3)
    assert journal.stats().queued_bytes == 0
    assert journal.close()


def _evaluation(
    journal,
    m,
    *,
    attempt="a",
    decision=V34Decision.CALL,
    status="canonical",
    ordinal=0,
    event_time=START + 2 * 10**9,
):
    h, o, signal = _history(), _option_chain(), _signal()
    o = replace(
        o,
        response=replace(
            o.response, request_started_utc_ns=START, response_received_utc_ns=START + 10**9
        ),
    )
    h = replace(
        h,
        response=replace(
            h.response, request_started_utc_ns=START, response_received_utc_ns=START + 10**9
        ),
        snapshot=replace(h.snapshot, captured_ns=START),
    )
    signal = replace(
        signal,
        decision=decision,
        chain_state=replace(signal.chain_state, captured_ns=START + 10**9),
    )
    return enqueue_completed_evaluation(
        journal,
        experiment_id=m.experiment_id,
        run_id="r",
        slot=collector.schedule_for(m)[0],
        attempt_id=attempt,
        history=h,
        option_chain=o,
        evaluated_option_chain=o.snapshot,
        signal=signal,
        population_status=status,
        retry_ordinal=ordinal,
        previous_chain=None,
        config=V34Config(),
        as_of=date(2026, 9, 28),
        event_time_utc_ns=event_time,
        canonical_decision=V34Decision.CALL,
        canonical_captured_ns=START + 10**9,
    )


def _endpoint(archive, *, offset=1800, price="110", asset="BTC", received=None):
    boundary = START + offset * 10**9
    candle = DeltaCandle(
        (boundary // 10**9) - 300,
        Decimal(price),
        Decimal(price),
        Decimal(price),
        Decimal(price),
        Decimal("1"),
    )
    raw = json.dumps(
        {
            "success": True,
            "result": [
                {
                    "time": candle.time_s,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": "1",
                }
            ],
        }
    ).encode()
    r = RawPublicResponse(
        "https://api.india.delta.exchange/v2/history/candles",
        boundary,
        boundary + 10**9 if received is None else received,
        10**9,
        raw,
        hashlib.sha256(raw).hexdigest(),
    )
    archive.history(
        CapturedHistorySnapshot(
            r, DeltaCandleSnapshot(asset, asset + "USD", "5m", (candle,), boundary)
        )
    )


def _finalize(path, journal, m):
    assert journal.flush()
    with sqlite3.connect(path) as con:
        statements = build_endpoint_statements(
            con, m.experiment_id, m.endpoint_acquisition_deadline_utc_ns
        )
    assert journal.enqueue_batch(statements)
    assert journal.flush()
    with sqlite3.connect(path) as con:
        return con.execute(
            "SELECT status,reason,p30_observation_id FROM signal_endpoints"
        ).fetchone()


def test_exact_endpoint_uses_first_revision_and_original_p0(database):
    path, m = database
    journal = ExperimentJournal(path)
    refs = _evaluation(journal, m)
    archive = EvidenceArchive(
        journal,
        m.experiment_id,
        "r",
        {START + 1800 * 10**9},
        m.endpoint_acquisition_deadline_utc_ns,
    )
    _endpoint(archive)
    # Restarted archive sees revised data; original durable observation must still win.
    archive = EvidenceArchive(
        journal,
        m.experiment_id,
        "r2",
        {START + 1800 * 10**9},
        m.endpoint_acquisition_deadline_utc_ns,
    )
    _endpoint(archive, price="999")
    status, _, p30 = _finalize(path, journal, m)
    assert status == "complete"
    with sqlite3.connect(path) as con:
        assert (
            con.execute(
                "SELECT close_decimal FROM candle_observations WHERE observation_id=?", (p30,)
            ).fetchone()[0]
            == "110"
        )
        assert (
            con.execute("SELECT p0_observation_id FROM signal_endpoints").fetchone()[0]
            == refs.p0_observation_id
        )
        assert (
            build_endpoint_statements(con, m.experiment_id, m.endpoint_acquisition_deadline_utc_ns)
            == ()
        )
    assert journal.close()


@pytest.mark.parametrize(
    "case,expected",
    [
        ("adjacent", "missing"),
        ("wrong_asset", "missing"),
        ("late", "missing"),
        ("clock", "missing"),
        ("conflict", "ambiguous"),
        ("late_signal", "invalid"),
    ],
)
def test_endpoint_rejects_substitutions_and_ambiguity(database, case, expected):
    path, m = database
    journal = ExperimentJournal(path)
    _evaluation(journal, m, event_time=START + (301 if case == "late_signal" else 2) * 10**9)
    archive = EvidenceArchive(
        journal,
        m.experiment_id,
        "r",
        {START + 1800 * 10**9},
        m.endpoint_acquisition_deadline_utc_ns,
    )
    _endpoint(
        archive,
        offset=2100 if case == "adjacent" else 1800,
        asset="ETH" if case == "wrong_asset" else "BTC",
        received=m.endpoint_acquisition_deadline_utc_ns + 1
        if case == "late"
        else START
        if case == "clock"
        else None,
    )
    if case == "conflict":
        _evaluation(
            journal,
            m,
            attempt="retry",
            decision=V34Decision.PUT,
            status="conflicting_retry",
            ordinal=1,
        )
    assert _finalize(path, journal, m)[0] == expected
    assert journal.close()


def test_manifest_and_schedule_mismatch_rejected(database):
    path, m = database
    collector.verify_database(path, m)
    with pytest.raises(RuntimeError, match="manifest mismatch"):
        collector.verify_database(path, replace(m, collector_build_id="other"))
    with pytest.raises(ValueError, match="incompatible manifest"):
        collector.validate_manifest(m, "other")


def test_path_aliases_are_rejected(database, tmp_path):
    path, _ = database
    symlink = tmp_path / "alias.sqlite"
    symlink.symlink_to(path)
    with pytest.raises(ValueError):
        collector.checked_path(tmp_path, symlink)
    import os

    hardlink = tmp_path / "hard.sqlite"
    os.link(path, hardlink)
    with pytest.raises(ValueError):
        collector.checked_path(tmp_path, hardlink)
    with pytest.raises(ValueError):
        collector.checked_path(tmp_path, tmp_path / ".." / "outside.sqlite")


def test_inflight_marker_prevents_any_market_access(database, monkeypatch):
    path, m = database
    path.with_name(path.name + ".inflight").write_text("ambiguous")
    monkeypatch.setattr(capture, "raw_delta_public_get", lambda *a, **k: pytest.fail("network"))
    with pytest.raises(RuntimeError, match="ambiguous prior"):
        collector.run_collection(path, m, threading.Event())


def test_shutdown_without_collection_is_clean_and_offline(database, monkeypatch):
    path, m = database
    monkeypatch.setattr(capture, "raw_delta_public_get", lambda *a, **k: pytest.fail("network"))
    stop = threading.Event()
    stop.set()
    collector.run_collection(path, m, stop)
    assert not path.with_name(path.name + ".inflight").exists()
    with sqlite3.connect(path) as con:
        assert [r[0] for r in con.execute("SELECT event_type FROM journal_events")] == [
            "run_started",
            "run_stopped",
        ]


def test_full_collector_cycle_tail_and_finalization_without_network(database, monkeypatch):
    """Exercise real orchestration, journal/intent/adapter/recovery and endpoint tail."""
    from nautilus_delta_options.research.experiment1 import state_machine
    from nautilus_delta_options.research.experiment1.market_data import (
        Experiment1CapturedMarketData,
    )

    path, m = database
    wall = [START + 2 * 10**9]
    monkeypatch.setattr(collector.time, "time_ns", lambda: wall[0])
    monkeypatch.setattr(collector.time, "monotonic_ns", lambda: wall[0])
    monkeypatch.setattr(capture, "raw_delta_public_get", lambda *a, **k: pytest.fail("network"))
    calls = []

    class Market(Experiment1CapturedMarketData):
        tail = False

        def __init__(self, archive, **kwargs):
            super().__init__()
            self.archive = archive

        def fetch_5m_candles(self, underlying, **kwargs):
            calls.append(("history", underlying, self.tail))
            h = _history()
            response = replace(
                h.response, request_started_utc_ns=wall[0], response_received_utc_ns=wall[0]
            )
            snapshot = replace(
                h.snapshot, underlying=underlying, symbol=underlying + "USD", captured_ns=wall[0]
            )
            self._history[underlying] = replace(h, response=response, snapshot=snapshot)
            if self.tail:
                _endpoint(self.archive, asset=underlying)
            return snapshot

        def fetch_option_chain(self, underlying):
            calls.append(("chain", underlying, self.tail))
            o = _option_chain()
            snapshot = replace(
                o.snapshot,
                underlying=underlying,
                tickers=tuple(replace(t, underlying=underlying) for t in o.snapshot.tickers),
            )
            self._option_chains[underlying] = replace(
                o,
                snapshot=snapshot,
                response=replace(
                    o.response, request_started_utc_ns=wall[0], response_received_utc_ns=wall[0]
                ),
            )
            return snapshot

    class Stop:
        calls = 0

        def is_set(self):
            return False

        def wait(self, seconds):
            self.calls += 1
            wall[0] = START + (1801 if self.calls == 1 else 2401) * 10**9

    def evaluate(candles, chain, **kwargs):
        signal = _signal()
        return replace(
            signal,
            underlying=candles.underlying,
            chain_state=replace(
                signal.chain_state, underlying=candles.underlying, captured_ns=wall[0]
            ),
        )

    monkeypatch.setattr(collector, "ArchivedMarketData", Market)
    monkeypatch.setattr(state_machine.quote_safety, "fresh_chain", lambda c, n: c)
    monkeypatch.setattr(state_machine, "rank_v34_contract_quality", lambda *a, **k: ())
    monkeypatch.setattr(state_machine, "evaluate_v34_shadow_signal", evaluate)
    collector.run_collection(path, m, Stop())
    assert calls == [
        ("history", "BTC", False),
        ("history", "ETH", False),
        ("chain", "BTC", False),
        ("chain", "ETH", False),
        ("history", "BTC", True),
        ("history", "ETH", True),
    ]
    assert not path.with_name(path.name + ".inflight").exists()
    with sqlite3.connect(path) as con:
        assert (
            con.execute("SELECT count(*) FROM signal_endpoints WHERE status='complete'").fetchone()[
                0
            ]
            == 2
        )
        assert (
            con.execute(
                "SELECT count(*) FROM journal_events WHERE event_type='evaluation_started'"
            ).fetchone()[0]
            == 2
        )
    from nautilus_delta_options.research.experiment1.rolling_state import (
        load_last_committed_observer_state,
    )

    recovered = load_last_committed_observer_state(path, experiment_id=m.experiment_id)
    assert recovered.candle_close_boundary_ns == START
    assert len(recovered.chains) == 2


def test_schema_change_rejected_on_verify(database):
    path, m = database
    with sqlite3.connect(path) as con:
        con.execute("DROP TRIGGER evaluation_slots_no_delete")
    with pytest.raises(RuntimeError, match="schema descriptor"):
        collector.verify_database(path, m)


def test_live_run_requires_explicit_authorization_before_network(database, monkeypatch, tmp_path):
    import subprocess
    import sys
    from dataclasses import asdict

    path, m = database
    protocol = tmp_path / "protocol.md"
    protocol.write_bytes(b"sealed protocol")
    sealed = tmp_path / "manifest.json"
    sealed.write_text(json.dumps(asdict(m)))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "nautilus_delta_options.research.experiment1",
            "run",
            "--root",
            str(tmp_path),
            "--database",
            str(path),
            "--manifest",
            str(sealed),
            "--manifest-sha256",
            hashlib.sha256(sealed.read_bytes()).hexdigest(),
            "--protocol",
            str(protocol),
            "--build-id",
            "test-build",
        ],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(Path("src").resolve())},
        timeout=10,
    )
    assert result.returncode != 0
    assert "requires explicit" in result.stderr
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT count(*) FROM journal_events").fetchone()[0] == 0
