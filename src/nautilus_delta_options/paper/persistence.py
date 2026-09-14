from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, closing, nullcontext
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from nautilus_delta_options.paper.ledger import (
    ExitReason,
    PaperClosedTrade,
    PaperLedger,
    PaperPosition,
)

_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PaperSignalReceipt:
    signal_key: str
    underlying: str
    candle_closed_ns: int
    trade_id: int
    consumed_ns: int


class PaperLedgerPersistenceError(RuntimeError):
    """Raised when persisted paper-ledger state is invalid or unsupported."""


class SQLitePaperLedgerStore:
    """Atomically persists one complete paper-ledger snapshot in SQLite."""

    def __init__(self, path: str | Path) -> None:
        self._revision: int | None = None
        self.run_provenance: dict[str, object] | None = None
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def revision(self) -> int | None:
        return self._revision

    def _check_revision(
        self,
        connection: sqlite3.Connection,
        expected: int | None,
    ) -> None:
        row = connection.execute(
            "SELECT updated_ns FROM paper_ledger_state WHERE id = 1"
        ).fetchone()
        actual = row[0] if row else None
        if actual != expected:
            raise PaperLedgerPersistenceError(
                "Ledger revision changed: another writer is active; reload before retrying"
            )

    def save(
        self,
        ledger: PaperLedger,
        *,
        expected_revision: int | None = None,
    ) -> None:
        payload = json.dumps(
            _ledger_to_payload(ledger),
            separators=(",", ":"),
            sort_keys=True,
        )

        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._check_revision(
                connection,
                self._revision if expected_revision is None else expected_revision,
            )
            revision = max(time.time_ns(), (self._revision or 0) + 1)
            connection.execute(
                """
                INSERT INTO paper_ledger_state (
                    id,
                    schema_version,
                    payload,
                    updated_ns
                )
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    payload = excluded.payload,
                    updated_ns = excluded.updated_ns
                """,
                (_SCHEMA_VERSION, payload, revision),
            )

        self._revision = revision

    def has_consumed_signal(self, signal_key: str) -> bool:
        _validate_signal_metadata(
            signal_key=signal_key,
            underlying="CHECK",
            candle_closed_ns=1,
            trade_id=1,
        )

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT 1
                FROM paper_signal_receipts
                WHERE signal_key = ?
                """,
                (signal_key,),
            ).fetchone()

        return row is not None

    def save_with_signal(
        self,
        ledger: PaperLedger,
        *,
        signal_key: str,
        underlying: str,
        candle_closed_ns: int,
        trade_id: int,
        expected_revision: int | None = None,
        before_commit: Callable[[], None] | None = None,
        readiness_guard: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> PaperSignalReceipt | None:
        _validate_signal_metadata(
            signal_key=signal_key,
            underlying=underlying,
            candle_closed_ns=candle_closed_ns,
            trade_id=trade_id,
        )

        position = next(
            (candidate for candidate in ledger.open_positions if candidate.trade_id == trade_id),
            None,
        )
        if position is None:
            raise ValueError("Signal trade_id must identify an open position")
        if position.underlying != underlying:
            raise ValueError("Signal underlying does not match the position")

        payload = json.dumps(
            _ledger_to_payload(ledger),
            separators=(",", ":"),
            sort_keys=True,
        )
        consumed_ns = max(time.time_ns(), (self._revision or 0) + 1)
        receipt = PaperSignalReceipt(
            signal_key=signal_key,
            underlying=underlying,
            candle_closed_ns=candle_closed_ns,
            trade_id=trade_id,
            consumed_ns=consumed_ns,
        )

        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT 1
                FROM paper_signal_receipts
                WHERE signal_key = ?
                """,
                (signal_key,),
            ).fetchone()

            if existing is not None:
                return None

            self._check_revision(
                connection,
                self._revision if expected_revision is None else expected_revision,
            )
            with readiness_guard() if readiness_guard is not None else nullcontext():
                if before_commit is not None:
                    before_commit()
                try:
                    connection.execute(
                        """
                        INSERT INTO paper_signal_receipts (
                            signal_key,
                            underlying,
                            candle_closed_ns,
                            trade_id,
                            consumed_ns
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            signal_key,
                            underlying,
                            candle_closed_ns,
                            trade_id,
                            consumed_ns,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO paper_ledger_state (
                            id,
                            schema_version,
                            payload,
                            updated_ns
                        )
                        VALUES (1, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            schema_version = excluded.schema_version,
                            payload = excluded.payload,
                            updated_ns = excluded.updated_ns
                        """,
                        (_SCHEMA_VERSION, payload, consumed_ns),
                    )
                    connection.commit()
                except sqlite3.IntegrityError as error:
                    raise PaperLedgerPersistenceError(
                        "Signal receipt conflicts with persisted state"
                    ) from error

        self._revision = consumed_ns
        return receipt

    def load(self) -> PaperLedger | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT schema_version, payload, updated_ns
                FROM paper_ledger_state
                WHERE id = 1
                """
            ).fetchone()

        if row is None:
            return None

        schema_version, payload_text, revision = row
        self._revision = revision

        if schema_version != _SCHEMA_VERSION:
            raise PaperLedgerPersistenceError(f"Unsupported schema version: {schema_version}")
        if not isinstance(payload_text, str):
            raise PaperLedgerPersistenceError("Persisted ledger payload must be text")

        try:
            decoded: object = json.loads(payload_text)
        except (json.JSONDecodeError, TypeError) as error:
            raise PaperLedgerPersistenceError("Persisted ledger payload is invalid JSON") from error

        ledger = _ledger_from_payload(decoded)
        expected_cash = ledger.initial_cash + ledger.realized_pnl - sum(
            (p.entry_debit for p in ledger.open_positions), Decimal("0"),
        )
        if ledger.cash != expected_cash:
            raise PaperLedgerPersistenceError("Persisted cash does not reconcile with paper trades")
        return ledger

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_ledger_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    schema_version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    updated_ns INTEGER NOT NULL
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_signal_receipts (
                    signal_key TEXT PRIMARY KEY,
                    underlying TEXT NOT NULL,
                    candle_closed_ns INTEGER NOT NULL
                        CHECK (candle_closed_ns > 0),
                    trade_id INTEGER NOT NULL UNIQUE
                        CHECK (trade_id > 0),
                    consumed_ns INTEGER NOT NULL
                        CHECK (consumed_ns > 0)
                )
                """
            )


def _validate_signal_metadata(
    *,
    signal_key: str,
    underlying: str,
    candle_closed_ns: int,
    trade_id: int,
) -> None:
    if not signal_key or signal_key.strip() != signal_key or len(signal_key) > 256:
        raise ValueError("signal_key must be non-empty normalized text")

    if not underlying or underlying.strip() != underlying or underlying.upper() != underlying:
        raise ValueError("underlying must be non-empty uppercase text")

    integer_fields = (
        ("candle_closed_ns", candle_closed_ns),
        ("trade_id", trade_id),
    )

    for name, value in integer_fields:
        if isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")


def _ledger_to_payload(ledger: PaperLedger) -> dict[str, object]:
    return {
        "initial_cash": str(ledger.initial_cash),
        "cash": str(ledger.cash),
        "minimum_reward_risk": str(ledger.minimum_reward_risk),
        "max_positions": ledger.max_positions,
        "gst_rate": str(ledger.gst_rate),
        "next_trade_id": ledger.next_trade_id,
        "open_positions": [_position_to_payload(position) for position in ledger.open_positions],
        "closed_trades": [_closed_trade_to_payload(trade) for trade in ledger.closed_trades],
    }


def _position_to_payload(position: PaperPosition) -> dict[str, object]:
    return {
        "trade_id": position.trade_id,
        "product_id": position.product_id,
        "symbol": position.symbol,
        "underlying": position.underlying,
        "contract_type": position.contract_type,
        "contracts": str(position.contracts),
        "contract_value": str(position.contract_value),
        "entry_price": str(position.entry_price),
        "entry_spot": str(position.entry_spot),
        "entry_premium": str(position.entry_premium),
        "entry_fee": str(position.entry_fee),
        "entry_debit": str(position.entry_debit),
        "stop_price": str(position.stop_price),
        "target_price": str(position.target_price),
        "planned_reward_risk": str(position.planned_reward_risk),
        "opened_ns": position.opened_ns,
        "stop_spot": str(position.stop_spot),
        "target_spot": str(position.target_spot),
        "planned_loss": str(position.planned_loss),
        "planned_reward": str(position.planned_reward),
        "taker_fee": str(position.taker_fee),
        "premium_cap_rate": str(position.premium_cap_rate),
        "entry_observation": position.entry_observation,
        "last_quote_ns": position.last_quote_ns,
        "last_bid": str(position.last_bid) if position.last_bid is not None else None,
        "last_exit_fee": str(position.last_exit_fee)
        if position.last_exit_fee is not None
        else None,
        "settlement_ns": position.settlement_ns,
        "strike_price": str(position.strike_price),
        "unresolved_reason": position.unresolved_reason,
    }


def _closed_trade_to_payload(
    trade: PaperClosedTrade,
) -> dict[str, object]:
    return {
        "position": _position_to_payload(trade.position),
        "exit_price": str(trade.exit_price),
        "exit_spot": str(trade.exit_spot),
        "exit_fee": str(trade.exit_fee),
        "gross_pnl": str(trade.gross_pnl),
        "net_pnl": str(trade.net_pnl),
        "reason": trade.reason.value,
        "closed_ns": trade.closed_ns,
    }


def _ledger_from_payload(value: object) -> PaperLedger:
    payload = _require_mapping(value, "ledger payload")

    open_positions = tuple(
        _position_from_payload(item) for item in _require_list(payload, "open_positions")
    )
    closed_trades = tuple(
        _closed_trade_from_payload(item) for item in _require_list(payload, "closed_trades")
    )

    try:
        return PaperLedger.from_state(
            initial_cash=_require_decimal(payload, "initial_cash"),
            cash=_require_decimal(payload, "cash"),
            minimum_reward_risk=_require_decimal(
                payload,
                "minimum_reward_risk",
            ),
            max_positions=_require_int(payload, "max_positions"),
            gst_rate=_require_decimal(payload, "gst_rate"),
            next_trade_id=_require_int(payload, "next_trade_id"),
            open_positions=open_positions,
            closed_trades=closed_trades,
        )
    except ValueError as error:
        raise PaperLedgerPersistenceError(f"Persisted ledger state is invalid: {error}") from error


def _position_from_payload(value: object) -> PaperPosition:
    payload = _require_mapping(value, "position")

    return PaperPosition(
        trade_id=_require_int(payload, "trade_id"),
        product_id=_require_int(payload, "product_id"),
        symbol=_require_string(payload, "symbol"),
        underlying=_require_string(payload, "underlying"),
        contract_type=_require_string(payload, "contract_type"),
        contracts=_require_decimal(payload, "contracts"),
        contract_value=_require_decimal(payload, "contract_value"),
        entry_price=_require_decimal(payload, "entry_price"),
        entry_spot=_require_decimal(payload, "entry_spot"),
        entry_premium=_require_decimal(payload, "entry_premium"),
        entry_fee=_require_decimal(payload, "entry_fee"),
        entry_debit=_require_decimal(payload, "entry_debit"),
        stop_price=_require_decimal(payload, "stop_price"),
        target_price=_require_decimal(payload, "target_price"),
        planned_reward_risk=_require_decimal(
            payload,
            "planned_reward_risk",
        ),
        opened_ns=_require_int(payload, "opened_ns"),
        stop_spot=_require_optional_decimal(
            payload,
            "stop_spot",
            default=_require_decimal(payload, "entry_spot"),
        ),
        target_spot=_require_optional_decimal(
            payload,
            "target_spot",
            default=_require_decimal(payload, "entry_spot"),
        ),
        planned_loss=_require_optional_decimal(
            payload,
            "planned_loss",
            default=_require_decimal(payload, "entry_debit"),
        ),
        planned_reward=_require_optional_decimal(
            payload,
            "planned_reward",
            default=Decimal("0"),
        ),
        taker_fee=_require_optional_decimal(
            payload,
            "taker_fee",
            default=Decimal("0.0001"),
        ),
        entry_observation=(
            _require_string(payload, "entry_observation") if "entry_observation" in payload else ""
        ),
        last_quote_ns=_require_int(payload, "last_quote_ns") if "last_quote_ns" in payload else 0,
        last_bid=_require_decimal(payload, "last_bid") if payload.get("last_bid") else None,
        last_exit_fee=(
            _require_decimal(payload, "last_exit_fee") if payload.get("last_exit_fee") else None
        ),
        settlement_ns=_require_int(payload, "settlement_ns") if "settlement_ns" in payload else 0,
        strike_price=_require_optional_decimal(payload, "strike_price", default=Decimal("0")),
        unresolved_reason=cast(str | None, payload.get("unresolved_reason")),
        premium_cap_rate=_require_optional_decimal(
            payload,
            "premium_cap_rate",
            default=Decimal("0.035"),
        ),
    )


def _closed_trade_from_payload(value: object) -> PaperClosedTrade:
    payload = _require_mapping(value, "closed trade")

    try:
        reason = ExitReason(_require_string(payload, "reason"))
    except ValueError as error:
        raise PaperLedgerPersistenceError("Closed trade contains an invalid exit reason") from error

    return PaperClosedTrade(
        position=_position_from_payload(_require_value(payload, "position")),
        exit_price=_require_decimal(payload, "exit_price"),
        exit_spot=_require_decimal(payload, "exit_spot"),
        exit_fee=_require_decimal(payload, "exit_fee"),
        gross_pnl=_require_decimal(payload, "gross_pnl"),
        net_pnl=_require_decimal(payload, "net_pnl"),
        reason=reason,
        closed_ns=_require_int(payload, "closed_ns"),
    )


def _require_mapping(
    value: object,
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise PaperLedgerPersistenceError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _require_value(
    payload: Mapping[str, object],
    key: str,
) -> object:
    if key not in payload:
        raise PaperLedgerPersistenceError(f"Persisted ledger is missing {key}")
    return payload[key]


def _require_string(
    payload: Mapping[str, object],
    key: str,
) -> str:
    value = _require_value(payload, key)
    if not isinstance(value, str):
        raise PaperLedgerPersistenceError(f"Persisted {key} must be text")
    return value


def _require_int(
    payload: Mapping[str, object],
    key: str,
) -> int:
    value = _require_value(payload, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaperLedgerPersistenceError(f"Persisted {key} must be an integer")
    return value


def _require_decimal(
    payload: Mapping[str, object],
    key: str,
) -> Decimal:
    value = _require_string(payload, key)

    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise PaperLedgerPersistenceError(f"Persisted {key} must be decimal text") from error

    if not result.is_finite():
        raise PaperLedgerPersistenceError(f"Persisted {key} must be finite")

    return result


def _require_optional_decimal(
    payload: Mapping[str, object],
    key: str,
    *,
    default: Decimal,
) -> Decimal:
    if key not in payload:
        return default
    return _require_decimal(payload, key)


def _require_list(
    payload: Mapping[str, object],
    key: str,
) -> list[object]:
    value = _require_value(payload, key)
    if not isinstance(value, list):
        raise PaperLedgerPersistenceError(f"Persisted {key} must be a list")
    return cast(list[object], value)
