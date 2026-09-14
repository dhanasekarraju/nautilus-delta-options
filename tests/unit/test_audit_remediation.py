from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import Response
from test_paper_ledger import TIMESTAMP_US, _ledger, _record, _ticker

from nautilus_delta_options.delta.history import parse_history_candles_payload
from nautilus_delta_options.paper.persistence import (
    PaperLedgerPersistenceError,
    SQLitePaperLedgerStore,
)
from nautilus_delta_options.paper.quote_safety import validate_quote_time
from nautilus_delta_options.paper.session import PaperLedgerSession

NOW = TIMESTAMP_US * 1000 + 1


def session_at(path: Path) -> PaperLedgerSession:
    return PaperLedgerSession.load_or_create(
        SQLitePaperLedgerStore(path), initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.5"), max_positions=3,
    )


def open_position(session):
    return session.open_long(
        _record(), contracts=Decimal("10"), stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"), stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )


@pytest.mark.parametrize("offset", [-15_000_000_001, 5_000_000_001])
def test_quote_rejects_stale_and_future(offset):
    with pytest.raises(ValueError):
        validate_quote_time(NOW + offset, NOW)


def test_exit_failure_does_not_publish_memory(tmp_path, monkeypatch):
    session = session_at(tmp_path / "paper.sqlite")
    position = open_position(session)
    before = session.snapshot()
    def fail(*args, **kwargs):
        raise OSError("injected disk failure")
    monkeypatch.setattr(session._store, "save", fail)
    with pytest.raises(OSError):
        session.process_exit_ticker(
            position.trade_id, _ticker(bid="880", ask="890"), observed_ns=NOW,
        )
    assert session.snapshot().open_positions == before.open_positions
    assert session.snapshot().cash == before.cash
    assert session.snapshot().closed_trades == ()
    assert session._store.load().open_positions == before.open_positions


def test_stale_writer_cannot_overwrite_entry(tmp_path):
    path = tmp_path / "paper.sqlite"
    first = session_at(path)
    second = session_at(path)
    position = open_position(first)
    with pytest.raises(PaperLedgerPersistenceError, match="another writer"):
        open_position(second)
    assert second.snapshot().open_positions == ()
    assert session_at(path).snapshot().open_positions == (position,)


def test_stale_exit_does_not_change_position_or_cash(tmp_path):
    session = session_at(tmp_path / "paper.sqlite")
    position = open_position(session)
    before = session.snapshot()
    with pytest.raises(ValueError, match="Stale"):
        session.process_exit_ticker(
            position.trade_id, _ticker(bid="880", ask="890"),
            observed_ns=NOW + 16_000_000_000,
        )
    assert session.snapshot().open_positions == before.open_positions
    assert session.snapshot().cash == before.cash


def test_liquidation_value_includes_spread_and_exit_fees(tmp_path):
    session = session_at(tmp_path / "paper.sqlite")
    position = open_position(session)
    ledger = session.snapshot()
    value, complete = ledger.liquidation_equity(observed_ns=NOW)
    assert complete
    assert value == ledger.cash + Decimal("990") * Decimal("0.01") - position.last_exit_fee
    assert value < ledger.initial_cash
    _, complete = ledger.liquidation_equity(observed_ns=NOW + 16_000_000_000)
    assert not complete


def test_settlement_requires_reference_and_expiry(tmp_path):
    session = session_at(tmp_path / "paper.sqlite")
    position = open_position(session)
    with pytest.raises(ValueError):
        session.settle_long(
            position.trade_id, settlement_spot=Decimal("81000"),
            settlement_fee=Decimal("0"), observed_ns=NOW, reference="official-test",
        )
    trade = session.settle_long(
        position.trade_id, settlement_spot=Decimal("81000"),
        settlement_fee=Decimal("0.1"), observed_ns=position.settlement_ns,
        reference="verified fixture settlement",
    )
    assert trade.exit_price == Decimal("1000")
    assert session.snapshot().cash == Decimal("100") + trade.net_pnl
    assert session_at(tmp_path / "paper.sqlite").snapshot().closed_trades == (trade,)


@pytest.mark.parametrize("times", [(301,), (300, 900)])
def test_history_rejects_unaligned_or_gapped_candles(times):
    payload = {"success": True, "result": [
        {"time": t, "open": 10, "close": 10, "high": 11, "low": 9, "volume": 1}
        for t in times
    ]}
    with pytest.raises(ValueError):
        parse_history_candles_payload(payload, now_s=2000)


def test_out_of_order_exit_is_rejected():
    ledger = _ledger()
    # No mutation from a quote preceding the entry observation.
    from test_paper_ledger import _record
    position = ledger.open_long(
        _record(), contracts=Decimal("10"), stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"), stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )
    with pytest.raises(ValueError, match="predates"):
        ledger.process_exit_ticker(
            position.trade_id, replace(_ticker(), exchange_timestamp=TIMESTAMP_US - 1),
            observed_ns=NOW,
        )


def test_time_exit_needs_no_greeks(tmp_path):
    session = session_at(tmp_path / "paper.sqlite")
    position = open_position(session)
    later = NOW + 240 * 60_000_000_000
    ticker = replace(
        _ticker(bid="950", ask="960"), exchange_timestamp=later // 1000,
        delta=None, gamma=None, theta=None, vega=None,
    )
    closed = session.process_exit_ticker(
        position.trade_id, ticker, observed_ns=later, apply_time_policy=True,
    )
    assert closed is not None
    assert closed.reason.value == "time"
    assert closed.exit_price == Decimal("950")


def test_v34_health_exposes_uninitialized_loops(tmp_path):
    from test_app_fast_entry_wiring import _route_endpoint

    from nautilus_delta_options.web.v34_paper_app import create_v34_paper_app
    app = create_v34_paper_app(database=tmp_path / "paper.sqlite", entries_enabled=False)
    health = _route_endpoint(app, "/health")(Response())
    assert health["status"] == "error"
    assert len(health["errors"]) == 3
    assert health["real_orders_enabled"] is False
    dashboard = _route_endpoint(app, "/api/dashboard")()
    assert dashboard["valuation_complete"] is True
    assert dashboard["unrealized_pnl"] == "0"


def test_nonfinite_interval_is_rejected(monkeypatch):
    from nautilus_delta_options.web.v34_paper_app import _positive_float_env
    monkeypatch.setenv("V34_FAST_EXIT_SECONDS", "nan")
    with pytest.raises(ValueError):
        _positive_float_env("V34_FAST_EXIT_SECONDS", 5)
