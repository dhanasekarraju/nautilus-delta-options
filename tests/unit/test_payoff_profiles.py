from dataclasses import replace
from decimal import Decimal

import pytest
from test_audit_remediation import NOW, open_position, session_at
from test_paper_ledger import TIMESTAMP_US, _ledger, _record, _ticker

from nautilus_delta_options.paper.payoff_profiles import (
    PayoffProfile,
    initial_state,
    observe,
)
from nautilus_delta_options.paper.payoff_research import PayoffResearch


def admitted():
    ledger = _ledger()
    position = ledger.open_long(
        _record(), contracts=Decimal("10"), stop_exit_bid=Decimal("900"),
        target_exit_bid=Decimal("1200"), stop_spot=Decimal("79500"),
        target_spot=Decimal("81000"),
    )
    return ledger, position


def quote(bid, step=1, **changes):
    ticker = _ticker(
        bid=str(bid), ask=str(Decimal(str(bid)) + 10),
        timestamp_us=TIMESTAMP_US + step * 1_000_000,
    )
    return replace(ticker, **changes)


def run(position, state, ticker, **kwargs):
    return observe(
        position, state, ticker, observed_ns=ticker.exchange_timestamp * 1000 + 1, **kwargs,
    )


@pytest.mark.parametrize("bids", [
    [990, 1050, 1200], [990, 880], [1300], [400], [1000, 950, 1100, 890],
])
def test_baseline_matches_native_remediation_exit_prices_fees_and_reasons(bids):
    ledger, position = admitted()
    state = initial_state(position, PayoffProfile.BASELINE)
    for i, bid in enumerate(bids):
        ticker = quote(bid, i + 1)
        state, event = run(position, state, ticker)
        trade = ledger.process_exit_ticker(
            position.trade_id, ticker,
            observed_ns=ticker.exchange_timestamp * 1000 + 1, apply_time_policy=True,
        )
        assert state.closed == (trade is not None)
        assert state.stop == position.stop_price
        assert state.target == position.target_price
        if trade:
            assert event["exit_trigger"] == trade.reason.value
            assert Decimal(event["executable_exit_price"]) == trade.exit_price
            assert Decimal(event["net_pnl"]) == trade.net_pnl
            assert Decimal(event["estimated_exit_fee"]) == trade.exit_fee
            break


def test_protection_activates_on_fee_adjusted_1r_not_gross_premium():
    _, position = admitted()
    state = initial_state(position, PayoffProfile.EXPERIMENTAL)
    # Gross gain equals R, but fees still leave net gain below R.
    bid = position.entry_price + state.risk / (position.contracts * position.contract_value)
    state, event = run(position, state, quote(bid))
    assert Decimal(event["net_pnl"]) < state.risk
    assert not state.activated
    state, event = run(position, state, quote(1300, 2))
    assert state.activated and not state.closed
    assert state.stop > position.entry_price
    assert state.target is None


def test_trail_is_nondecreasing_and_no_fixed_target():
    _, position = admitted()
    state = initial_state(position, PayoffProfile.EXPERIMENTAL)
    stops = []
    for step, bid in enumerate([1300, 1600, 2000], 1):
        state, event = run(position, state, quote(bid, step))
        assert not state.closed
        stops.append(state.stop)
    assert stops == sorted(stops)
    state, event = run(position, state, quote(1500, 4))
    assert state.closed
    assert event["exit_trigger"] == "stop"
    assert Decimal(event["executable_exit_price"]) == Decimal("1500")
    assert Decimal(event["theoretical_exit_price"]) > Decimal("1500")


@pytest.mark.parametrize("offset", [16_000_000_000, -6_000_000_000])
def test_stale_or_future_does_not_mutate_any_payoff_state(offset):
    _, position = admitted()
    state = initial_state(position, PayoffProfile.EXPERIMENTAL)
    ticker = quote(2000)
    after, event = observe(
        position, state, ticker, observed_ns=ticker.exchange_timestamp * 1000 + offset,
    )
    assert after == state
    assert event["exit_trigger"] is None
    assert event["status"] in ("Stale quote", "Future quote")


def test_thin_bid_cannot_activate_trail_or_fill():
    _, position = admitted()
    state = initial_state(position, PayoffProfile.EXPERIMENTAL)
    for bid in (2000, 400):
        after, event = run(position, state, quote(bid, bid_size=Decimal("1")))
        assert after == state
        assert event["exit_trigger"] is None
        assert event["status"].startswith("deferred")


def test_iv_crush_uses_executable_bid_not_mark_or_greeks():
    _, position = admitted()
    state = initial_state(position, PayoffProfile.EXPERIMENTAL)
    ticker = quote(700, mark_price=Decimal("1300"), mark_iv=Decimal("0.15"),
                   vega=Decimal("20"))
    state, event = run(position, state, ticker)
    assert state.closed
    assert event["executable_exit_price"] == "700"
    assert event["market"]["mark_iv"] == "0.15"


def test_theta_decay_is_observed_not_fabricated_from_vendor_greek_units():
    _, position = admitted()
    state = initial_state(position, PayoffProfile.EXPERIMENTAL)
    state, event = run(position, state, quote(990, theta=Decimal("-10000")))
    assert not state.closed
    state, event = run(position, state, quote(890, 2, theta=Decimal("-10000")))
    assert state.closed and event["exit_trigger"] == "stop"


@pytest.mark.parametrize("profile", list(PayoffProfile))
def test_expiry_proximity_and_max_hold_keep_existing_limits(profile):
    _, position = admitted()
    state = initial_state(position, profile)
    near = replace(position, settlement_ns=NOW + 120 * 60_000_000_000)
    state, event = run(near, state, quote(1000))
    assert state.closed and event["exit_trigger"] == "time"
    _, position = admitted()
    state = initial_state(position, profile)
    ticker = quote(1000, step=240 * 60)
    state, event = run(position, state, ticker)
    assert state.closed and event["exit_trigger"] == "time"


def test_expired_contract_stays_unresolved_without_invented_settlement():
    _, position = admitted()
    position = replace(position, settlement_ns=NOW)
    state = initial_state(position, PayoffProfile.EXPERIMENTAL)
    after, event = run(position, state, quote(1000))
    assert after == state
    assert "verified reconciliation" in event["status"]


def test_fee_adjusted_stop_covers_entry_and_exit_costs_with_penalty():
    _, position = admitted()
    state = initial_state(position, PayoffProfile.EXPERIMENTAL)
    state, _ = run(position, state, quote(1300), penalty=Decimal("5"))
    _, event = run(position, state, quote(state.stop, 2), penalty=Decimal("5"))
    assert Decimal(event["net_pnl"]) >= 0
    assert event["execution_penalty_total"] == "0.050"


def test_pair_survives_restart_and_continues_after_baseline_target(tmp_path):
    session = session_at(tmp_path / "primary.sqlite")
    position = open_position(session)
    path = tmp_path / "research.sqlite"
    research = PayoffResearch(path, profile=PayoffProfile.EXPERIMENTAL)
    research.sync_entries(session.snapshot())
    research.sync_entries(session.snapshot())
    assert len(research.events()) == 2
    research.on_quote(quote(1300), NOW + 1_000_000_000)
    rows = research.summary()["profiles"]
    assert {row["profile"]: row["closed"] for row in rows} == {
        "baseline_v34": 1, "astra_payoff_experimental": 0,
    }
    restored = PayoffResearch(path, profile=PayoffProfile.EXPERIMENTAL)
    assert restored.symbols() == (position.symbol,)
    restored.on_quote(quote(1000, 2), NOW + 2_000_000_000)
    assert restored.symbols() == ()
    # Neither companion can debit, close or mutate primary-account trades.
    assert session.snapshot().open_positions == (position,)
    assert session.snapshot().closed_trades == ()


def test_research_configuration_is_immutable_and_default_is_baseline(tmp_path):
    path = tmp_path / "research.sqlite"
    research = PayoffResearch(path)
    assert research.profile == PayoffProfile.BASELINE
    with pytest.raises(ValueError, match="configuration changed"):
        PayoffResearch(path, profile=PayoffProfile.EXPERIMENTAL)


def test_research_disk_failure_rolls_back_state_and_event(tmp_path, monkeypatch):
    session = session_at(tmp_path / "primary.sqlite")
    open_position(session)
    research = PayoffResearch(tmp_path / "research.sqlite", profile=PayoffProfile.EXPERIMENTAL)
    research.sync_entries(session.snapshot())
    before = research.summary()
    def fail(*args, **kwargs):
        raise OSError("injected journal disk failure")
    monkeypatch.setattr(research, "_append", fail)
    with pytest.raises(OSError):
        research.on_quote(quote(1300), NOW + 1_000_000_000)
    assert research.summary() == before
    assert len(research.events()) == 2


def test_default_fast_exit_same_account_result_with_research_sink(tmp_path):
    from nautilus_delta_options.paper.fast_exit import run_fast_exit_cycle
    class Client:
        def fetch_option_tickers(self, symbols):
            return (quote(1200),)
    sessions = [session_at(tmp_path / f"{i}.sqlite") for i in range(2)]
    for session in sessions:
        open_position(session)
    research = PayoffResearch(tmp_path / "research.sqlite")
    research.sync_entries(sessions[1].snapshot())
    for index, session in enumerate(sessions):
        run_fast_exit_cycle(
            client=Client(), session=session,
            clock_ns=lambda: NOW + 1_000_000_000,
            on_quote=research.on_quote if index else None,
        )
    assert sessions[0].snapshot().cash == sessions[1].snapshot().cash
    assert sessions[0].snapshot().closed_trades == sessions[1].snapshot().closed_trades


def test_stale_after_activation_preserves_excursions_and_trail():
    _, position = admitted()
    state = initial_state(position, PayoffProfile.EXPERIMENTAL)
    state, _ = run(position, state, quote(1500))
    assert state.activated
    for bid in (100, 9000):
        ticker = quote(bid, 2)
        after, event = observe(position, state, ticker, observed_ns=NOW + 30_000_000_000)
        assert after == state
        assert event["exit_trigger"] is None


def test_entry_excursions_include_spread_and_round_trip_fees():
    _, position = admitted()
    state = initial_state(position, PayoffProfile.EXPERIMENTAL)
    assert state.mfe is not None and state.mae is not None
    assert state.mfe == state.mae < 0


def test_extra_research_failure_does_not_prevent_primary_stop(tmp_path):
    from nautilus_delta_options.paper.fast_exit import run_fast_exit_cycle
    class Client:
        def fetch_option_tickers(self, symbols):
            if symbols == ("missing-research-contract",):
                raise OSError("market data failure")
            return (quote(800),)
    session = session_at(tmp_path / "paper.sqlite")
    open_position(session)
    cycle = run_fast_exit_cycle(
        client=Client(), session=session, clock_ns=lambda: NOW + 1_000_000_000,
        extra_symbols=("missing-research-contract",), on_quote=lambda ticker, stamp: None,
    )
    assert len(cycle.closed_trades) == 1
    assert cycle.closed_trades[0].exit_price == Decimal("800")
    assert len(cycle.warnings) == 1


def test_app_profile_is_opt_in_and_companion_only(tmp_path, monkeypatch):
    from test_app_fast_entry_wiring import _route_endpoint

    from nautilus_delta_options.web.v34_paper_app import create_v34_paper_app
    monkeypatch.delenv("V34_PAYOFF_PROFILE", raising=False)
    app = create_v34_paper_app(database=tmp_path / "baseline.sqlite")
    assert _route_endpoint(app, "/api/payoff-research")()["selected_profile"] == "baseline_v34"
    monkeypatch.setenv("V34_PAYOFF_PROFILE", "astra_payoff_experimental")
    app = create_v34_paper_app(database=tmp_path / "experiment.sqlite")
    result = _route_endpoint(app, "/api/payoff-research")()
    assert result["selected_profile"] == "astra_payoff_experimental"
    assert result["primary_paper_profile"] == "baseline_v34"
