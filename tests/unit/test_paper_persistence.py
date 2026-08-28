import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from nautilus_delta_options.paper.ledger import (
    ExitReason,
    PaperClosedTrade,
    PaperLedger,
    PaperPosition,
)
from nautilus_delta_options.paper.persistence import (
    PaperLedgerPersistenceError,
    SQLitePaperLedgerStore,
)


def _position(
    trade_id: int,
    symbol: str,
) -> PaperPosition:
    return PaperPosition(
        trade_id=trade_id,
        product_id=1000 + trade_id,
        symbol=symbol,
        underlying="BTC",
        contract_type="call_options",
        contracts=Decimal("10"),
        contract_value=Decimal("0.001"),
        entry_price=Decimal("1000"),
        entry_spot=Decimal("80000"),
        entry_premium=Decimal("10.000"),
        entry_fee=Decimal("0.100"),
        entry_debit=Decimal("10.100"),
        stop_price=Decimal("900"),
        target_price=Decimal("1200"),
        planned_reward_risk=Decimal("1.5"),
        opened_ns=1_000_000_000 + trade_id,
    )


def _closed_trade(position: PaperPosition) -> PaperClosedTrade:
    return PaperClosedTrade(
        position=position,
        exit_price=Decimal("1200"),
        exit_spot=Decimal("81000"),
        exit_fee=Decimal("0.100"),
        gross_pnl=Decimal("2.000"),
        net_pnl=Decimal("1.800"),
        reason=ExitReason.TARGET,
        closed_ns=2_000_000_000,
    )


def test_load_returns_none_before_first_save(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")

    assert store.load() is None


def test_round_trips_complete_ledger_state(
    tmp_path: Path,
) -> None:
    closed_position = _position(
        1,
        "C-BTC-80000-290826",
    )
    open_position = _position(
        2,
        "C-BTC-81000-290826",
    )
    closed_trade = _closed_trade(closed_position)

    ledger = PaperLedger.from_state(
        initial_cash=Decimal("250"),
        cash=Decimal("241.700"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
        gst_rate=Decimal("0.18"),
        next_trade_id=3,
        open_positions=(open_position,),
        closed_trades=(closed_trade,),
    )

    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")
    store.save(ledger)

    restored = store.load()

    assert restored is not None
    assert restored.initial_cash == Decimal("250")
    assert restored.cash == Decimal("241.700")
    assert restored.minimum_reward_risk == Decimal("1.5")
    assert restored.max_positions == 3
    assert restored.gst_rate == Decimal("0.18")
    assert restored.next_trade_id == 3
    assert restored.open_positions == (open_position,)
    assert restored.closed_trades == (closed_trade,)
    assert restored.realized_pnl == Decimal("1.800")


def test_save_atomically_replaces_previous_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "paper.sqlite"
    store = SQLitePaperLedgerStore(database)

    first = PaperLedger.from_state(
        initial_cash=Decimal("250"),
        cash=Decimal("239.900"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
        gst_rate=Decimal("0.18"),
        next_trade_id=2,
        open_positions=(_position(1, "C-BTC-80000-290826"),),
        closed_trades=(),
    )
    store.save(first)

    replacement = PaperLedger(
        initial_cash=Decimal("100"),
        minimum_reward_risk=Decimal("1.25"),
        max_positions=2,
        gst_rate=Decimal("0.20"),
    )
    store.save(replacement)

    restored = store.load()

    assert restored is not None
    assert restored.initial_cash == Decimal("100")
    assert restored.cash == Decimal("100")
    assert restored.minimum_reward_risk == Decimal("1.25")
    assert restored.max_positions == 2
    assert restored.gst_rate == Decimal("0.20")
    assert restored.next_trade_id == 1
    assert restored.open_positions == ()
    assert restored.closed_trades == ()

    with sqlite3.connect(str(database)) as connection:
        count = connection.execute("SELECT COUNT(*) FROM paper_ledger_state").fetchone()

    assert count == (1,)


def test_rejects_unsupported_schema_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "paper.sqlite"
    store = SQLitePaperLedgerStore(database)
    store.save(
        PaperLedger(
            initial_cash=Decimal("250"),
            minimum_reward_risk=Decimal("1.5"),
            max_positions=3,
        )
    )

    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            """
            UPDATE paper_ledger_state
            SET schema_version = 999
            WHERE id = 1
            """
        )

    with pytest.raises(
        PaperLedgerPersistenceError,
        match="Unsupported schema version",
    ):
        store.load()
