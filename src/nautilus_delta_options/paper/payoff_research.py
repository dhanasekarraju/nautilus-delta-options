"""Durable paired exit cohorts; does not debit or mutate the primary paper account."""

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import cast

from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.paper.ledger import PaperLedger, PaperPosition
from nautilus_delta_options.paper.payoff_profiles import (
    PayoffProfile,
    PayoffState,
    initial_state,
    observe,
)
from nautilus_delta_options.paper.persistence import _position_from_payload, _position_to_payload


def _json(value: object) -> str:
    return json.dumps(value, default=str, sort_keys=True, allow_nan=False)


def _state(text: str) -> PayoffState:
    data = json.loads(text)
    return PayoffState(
        profile=PayoffProfile(data["profile"]),
        stop=Decimal(data["stop"]),
        target=Decimal(data["target"]) if data["target"] is not None else None,
        risk=Decimal(data["risk"]),
        activated=data["activated"],
        best_net=Decimal(data["best_net"]) if data["best_net"] is not None else None,
        mfe=Decimal(data["mfe"]) if data["mfe"] is not None else None,
        mae=Decimal(data["mae"]) if data["mae"] is not None else None,
        last_event_ns=data["last_event_ns"],
        closed=data["closed"],
    )


class PayoffResearch:
    """SQLite transaction couples each profile state update to its immutable event."""

    def __init__(
        self,
        path: Path,
        *,
        profile: PayoffProfile = PayoffProfile.BASELINE,
        gst: Decimal = Decimal("0.18"),
        penalty: Decimal = Decimal("0"),
    ) -> None:
        if not penalty.is_finite() or penalty < 0:
            raise ValueError("Penalty must be finite and nonnegative")
        self.path = path
        self.profile = profile
        self.gst = gst
        self.penalty = penalty
        self.profiles = (
            (PayoffProfile.BASELINE, PayoffProfile.EXPERIMENTAL)
            if profile == PayoffProfile.EXPERIMENTAL
            else (PayoffProfile.BASELINE,)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, payload TEXT)")
            config = _json({"profile": profile, "gst": gst, "penalty": penalty, "schema": 1})
            old = db.execute("SELECT payload FROM config WHERE id=1").fetchone()
            if old is not None and old[0] != config:
                raise ValueError("Research configuration changed; use a new cohort database")
            db.execute("INSERT OR IGNORE INTO config VALUES (1, ?)", (config,))
            db.execute("""CREATE TABLE IF NOT EXISTS cohorts (
                trade_id INTEGER, profile TEXT, symbol TEXT, position TEXT, state TEXT,
                closed INTEGER NOT NULL, PRIMARY KEY(trade_id,profile))""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, trade_id INTEGER, profile TEXT,
                observed_ns INTEGER, payload TEXT)""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def sync_entries(self, ledger: PaperLedger) -> None:
        positions = (*ledger.open_positions, *(t.position for t in ledger.closed_trades))
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for position in positions:
                for profile in self.profiles:
                    existing = db.execute(
                        "SELECT position FROM cohorts WHERE trade_id=? AND profile=?",
                        (position.trade_id, profile),
                    ).fetchone()
                    if existing is not None:
                        old = _position_from_payload(json.loads(existing[0]))
                        if (old.symbol, old.opened_ns, old.entry_debit, old.planned_loss) != (
                            position.symbol,
                            position.opened_ns,
                            position.entry_debit,
                            position.planned_loss,
                        ):
                            raise ValueError(
                                "Primary trade identity changed; use a new research DB"
                            )
                        continue
                    if not position.entry_observation:
                        raise ValueError(
                            "Legacy entry lacks recorded observation; use a fresh validation ledger"
                        )
                    state = initial_state(position, profile, gst=self.gst, penalty=self.penalty)
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO cohorts VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            position.trade_id,
                            profile,
                            position.symbol,
                            _json(_position_to_payload(position)),
                            _json(asdict(state)),
                            0,
                        ),
                    ).rowcount
                    if inserted:
                        event = {
                            "kind": "entry",
                            "journaled_ns": time.time_ns(),
                            "mfe": str(state.mfe) if state.mfe is not None else None,
                            "mae": str(state.mae) if state.mae is not None else None,
                            "profile": profile,
                            "trade_id": position.trade_id,
                            "position": _position_to_payload(position),
                            "entry_observation": json.loads(position.entry_observation)
                            if position.entry_observation
                            else None,
                            "missing_entry_observation": not bool(position.entry_observation),
                            "stop": str(state.stop),
                            "target": str(state.target) if state.target is not None else None,
                            "initial_risk": str(state.risk),
                            "penalty": str(self.penalty),
                        }
                        self._append(db, position, profile, position.opened_ns, event)

    @staticmethod
    def _append(
        db: sqlite3.Connection,
        position: PaperPosition,
        profile: PayoffProfile,
        observed_ns: int,
        event: dict[str, object],
    ) -> None:
        db.execute(
            "INSERT INTO events (trade_id, profile, observed_ns, payload) VALUES (?, ?, ?, ?)",
            (position.trade_id, profile, observed_ns, _json(event)),
        )

    def symbols(self) -> tuple[str, ...]:
        with closing(self._connect()) as db:
            return tuple(
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT symbol FROM cohorts WHERE closed=0 ORDER BY symbol",
                )
            )

    def on_quote(self, ticker: DeltaOptionTicker, observed_ns: int) -> None:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT position, state FROM cohorts WHERE symbol=? AND closed=0",
                (ticker.symbol,),
            ).fetchall()
            for position_json, state_json in rows:
                position = _position_from_payload(json.loads(position_json))
                state, event = observe(
                    position,
                    _state(state_json),
                    ticker,
                    observed_ns=observed_ns,
                    gst=self.gst,
                    penalty=self.penalty,
                )
                self._append(db, position, state.profile, observed_ns, event)
                db.execute(
                    "UPDATE cohorts SET state=?, closed=? WHERE trade_id=? AND profile=?",
                    (_json(asdict(state)), int(state.closed), position.trade_id, state.profile),
                )

    def summary(self) -> dict[str, object]:
        with closing(self._connect()) as db:
            counts = list(
                db.execute(
                    "SELECT profile, COUNT(*), SUM(closed) FROM cohorts GROUP BY profile",
                )
            )
            total = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {
            "selected_profile": self.profile.value,
            "primary_paper_profile": PayoffProfile.BASELINE.value,
            "cohort_type": "paired admitted-entry exit research; no independent portfolio",
            "penalty_per_unit": str(self.penalty),
            "profiles": [
                {"profile": row[0], "entries": row[1], "closed": row[2]} for row in counts
            ],
            "observations": total,
        }

    def events(self) -> tuple[dict[str, object], ...]:
        with closing(self._connect()) as db:
            return tuple(
                cast(dict[str, object], json.loads(row[0]))
                for row in db.execute(
                    "SELECT payload FROM events ORDER BY sequence",
                )
            )
