"""Read-only consistency report for a stopped paper validation account."""

import argparse
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from nautilus_delta_options.paper.ledger import PaperPosition
from nautilus_delta_options.paper.payoff_research import _state
from nautilus_delta_options.paper.persistence import _ledger_from_payload, _position_from_payload


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    return db


def _position_errors(position: PaperPosition) -> list[str]:
    errors = []
    if position.contracts <= 0 or position.contract_value <= 0 or position.entry_price <= 0:
        errors.append("nonpositive size, multiplier or entry price")
    if position.contracts != position.contracts.to_integral_value():
        errors.append("nonintegral contract count")
    if position.entry_fee < 0:
        errors.append("negative entry fee")
    if (
        position.entry_premium
        != position.entry_price * position.contracts * position.contract_value
    ):
        errors.append("entry premium mismatch")
    if position.entry_debit != position.entry_premium + position.entry_fee:
        errors.append("entry debit mismatch")
    return [f"trade {position.trade_id}: {error}" for error in errors]


def reconcile(database: Path, research_database: Path | None = None) -> dict[str, object]:
    """Never creates a database or changes its contents. Use after writers stop."""
    issues: list[str] = []
    with closing(_connect(database)) as db, db:
        db.execute("BEGIN")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            issues.append("primary SQLite integrity check failed")
        row = db.execute(
            "SELECT schema_version, payload FROM paper_ledger_state WHERE id=1",
        ).fetchone()
        if row is None or row[0] != 1:
            raise ValueError("Missing or unsupported primary ledger schema")
        ledger = _ledger_from_payload(json.loads(row[1]))
        positions = {p.trade_id: p for p in ledger.open_positions}
        positions.update({t.position.trade_id: t.position for t in ledger.closed_trades})
        for position in positions.values():
            issues.extend(_position_errors(position))
        for trade in ledger.closed_trades:
            p = trade.position
            gross = (trade.exit_price - p.entry_price) * p.contracts * p.contract_value
            if trade.exit_price < 0 or trade.exit_fee < 0:
                issues.append(f"trade {p.trade_id}: negative exit price or fee")
            if trade.gross_pnl != gross or trade.net_pnl != gross - p.entry_fee - trade.exit_fee:
                issues.append(f"trade {p.trade_id}: closed P&L mismatch")
            if trade.closed_ns < p.opened_ns:
                issues.append(f"trade {p.trade_id}: exit precedes entry")
        expected_cash = (
            ledger.initial_cash
            + ledger.realized_pnl
            - sum(
                (p.entry_debit for p in ledger.open_positions),
                Decimal("0"),
            )
        )
        if ledger.cash != expected_cash:
            issues.append("cash does not reconcile with positions and realized P&L")
        for trade_id, underlying in db.execute(
            "SELECT trade_id, underlying FROM paper_signal_receipts",
        ):
            receipt_position = positions.get(trade_id)
            if receipt_position is None or receipt_position.underlying != underlying:
                issues.append(f"signal receipt {trade_id}: missing or mismatched trade")
    research_rows = 0
    if research_database is not None:
        with closing(_connect(research_database)) as db, db:
            db.execute("BEGIN")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                issues.append("research SQLite integrity check failed")
            for trade_id, profile, position_json, state_json, closed in db.execute(
                "SELECT trade_id, profile, position, state, closed FROM cohorts",
            ):
                research_rows += 1
                recorded = _position_from_payload(json.loads(position_json))
                primary = positions.get(trade_id)
                state = _state(state_json)
                if primary is None or (
                    primary.symbol,
                    primary.opened_ns,
                    primary.entry_debit,
                    primary.planned_loss,
                ) != (
                    recorded.symbol,
                    recorded.opened_ns,
                    recorded.entry_debit,
                    recorded.planned_loss,
                ):
                    issues.append(f"research {trade_id}/{profile}: primary identity mismatch")
                if state.profile.value != profile or int(state.closed) != closed:
                    issues.append(f"research {trade_id}/{profile}: state mismatch")
                count = db.execute(
                    "SELECT COUNT(*) FROM events WHERE trade_id=? AND profile=?",
                    (trade_id, profile),
                ).fetchone()[0]
                if not count:
                    issues.append(f"research {trade_id}/{profile}: missing event history")
    return {
        "ok": not issues,
        "issues": issues,
        "cash": str(ledger.cash),
        "realized_pnl": str(ledger.realized_pnl),
        "open_positions": len(ledger.open_positions),
        "closed_trades": len(ledger.closed_trades),
        "research_copies": research_rows,
        "scope": "accounting consistency only; not fill realism or profitability",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--research-database", type=Path)
    args = parser.parse_args()
    try:
        result = reconcile(args.database, args.research_database)
    except (ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError, ArithmeticError) as error:
        result = {"ok": False, "issues": [str(error)]}
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
