import json
from decimal import Decimal
from pathlib import Path

from test_payoff_profiles import admitted, quote


def native_results():
    results = []
    for bids, seconds in (
        ([990, 1050, 1200], 1),
        ([880], 1),
        ([400], 1),
        ([1400], 1),
        ([950], 240 * 60),
    ):
        ledger, position = admitted()
        for step, bid in enumerate(bids, 1):
            ticker = quote(bid, step * seconds)
            trade = ledger.process_exit_ticker(
                position.trade_id, ticker,
                observed_ns=ticker.exchange_timestamp * 1000 + 1,
                apply_time_policy=True,
            )
            if trade is not None:
                results.append({
                    "cash": str(ledger.cash), "entry": str(position.entry_price),
                    "entry_fee": str(position.entry_fee), "risk": str(position.planned_loss),
                    "stop": str(position.stop_price), "target": str(position.target_price),
                    "size": str(position.contracts), "exit": str(trade.exit_price),
                    "fee": str(trade.exit_fee), "net": str(trade.net_pnl),
                    "reason": trade.reason.value, "closed_ns": trade.closed_ns,
                })
                break
        assert ledger.cash == Decimal("100") + ledger.realized_pnl
    return results


def test_native_baseline_matches_frozen_remediation_golden():
    path = Path(__file__).parents[1] / "fixtures" / "baseline_v34_payoff_golden.json"
    expected = json.loads(path.read_text())
    assert expected["base_sha"] == "9a9dd9554e9f985b2c3a3fca0280acaea89b8254"
    assert native_results() == expected["results"]
