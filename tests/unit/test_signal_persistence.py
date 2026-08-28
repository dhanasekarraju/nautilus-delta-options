from decimal import Decimal
from pathlib import Path

import pytest

from nautilus_delta_options.paper.ledger import (
    PaperLedger,
    PaperPosition,
)
from nautilus_delta_options.paper.persistence import (
    PaperLedgerPersistenceError,
    SQLitePaperLedgerStore,
)

SIGNAL_KEY = "BTC:1787887499999:call"
CANDLE_CLOSED_NS = 1_787_887_499_999_000_000


def _empty_ledger() -> PaperLedger:
    return PaperLedger(
        initial_cash=Decimal("250"),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
    )


def _position(
    *,
    trade_id: int = 1,
    underlying: str = "BTC",
) -> PaperPosition:
    return PaperPosition(
        trade_id=trade_id,
        product_id=1000 + trade_id,
        symbol=f"C-{underlying}-80000-300826",
        underlying=underlying,
        contract_type="call_options",
        contracts=Decimal("10"),
        contract_value=Decimal("0.001"),
        entry_price=Decimal("1000"),
        entry_spot=Decimal("80000"),
        entry_premium=Decimal("10"),
        entry_fee=Decimal("0.1"),
        entry_debit=Decimal("40.1"),
        stop_price=Decimal("900"),
        target_price=Decimal("1200"),
        planned_reward_risk=Decimal("1.6"),
        opened_ns=1_787_887_500_000_000_000,
        stop_spot=Decimal("79200"),
        target_spot=Decimal("80800"),
        planned_loss=Decimal("5"),
        planned_reward=Decimal("8"),
    )


def _ledger_with_position(
    *,
    cash: str = "209.9",
    trade_id: int = 1,
    underlying: str = "BTC",
) -> PaperLedger:
    position = _position(
        trade_id=trade_id,
        underlying=underlying,
    )

    return PaperLedger.from_state(
        initial_cash=Decimal("250"),
        cash=Decimal(cash),
        minimum_reward_risk=Decimal("1.5"),
        max_positions=3,
        gst_rate=Decimal("0.18"),
        next_trade_id=trade_id + 1,
        open_positions=(position,),
        closed_trades=(),
    )


def test_atomically_saves_ledger_and_signal_receipt(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")
    store.save(_empty_ledger())

    candidate = _ledger_with_position()

    receipt = store.save_with_signal(
        candidate,
        signal_key=SIGNAL_KEY,
        underlying="BTC",
        candle_closed_ns=CANDLE_CLOSED_NS,
        trade_id=1,
    )

    assert receipt is not None
    assert receipt.signal_key == SIGNAL_KEY
    assert receipt.underlying == "BTC"
    assert receipt.candle_closed_ns == CANDLE_CLOSED_NS
    assert receipt.trade_id == 1
    assert receipt.consumed_ns > 0
    assert store.has_consumed_signal(SIGNAL_KEY)

    loaded = store.load()

    assert loaded is not None
    assert loaded.cash == candidate.cash
    assert loaded.open_positions == candidate.open_positions


def test_duplicate_signal_cannot_overwrite_ledger(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")
    store.save(_empty_ledger())

    first = _ledger_with_position(cash="209.9")
    second = _ledger_with_position(cash="150")

    first_receipt = store.save_with_signal(
        first,
        signal_key=SIGNAL_KEY,
        underlying="BTC",
        candle_closed_ns=CANDLE_CLOSED_NS,
        trade_id=1,
    )
    duplicate_receipt = store.save_with_signal(
        second,
        signal_key=SIGNAL_KEY,
        underlying="BTC",
        candle_closed_ns=CANDLE_CLOSED_NS,
        trade_id=1,
    )

    assert first_receipt is not None
    assert duplicate_receipt is None

    loaded = store.load()

    assert loaded is not None
    assert loaded.cash == first.cash
    assert loaded.open_positions == first.open_positions


def test_one_trade_cannot_consume_two_signal_keys(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")
    candidate = _ledger_with_position()

    receipt = store.save_with_signal(
        candidate,
        signal_key=SIGNAL_KEY,
        underlying="BTC",
        candle_closed_ns=CANDLE_CLOSED_NS,
        trade_id=1,
    )

    assert receipt is not None

    second_key = "BTC:1787887799999:call"

    with pytest.raises(
        PaperLedgerPersistenceError,
        match="Signal receipt conflicts",
    ):
        store.save_with_signal(
            candidate,
            signal_key=second_key,
            underlying="BTC",
            candle_closed_ns=CANDLE_CLOSED_NS + 300_000_000_000,
            trade_id=1,
        )

    assert not store.has_consumed_signal(second_key)

    loaded = store.load()

    assert loaded is not None
    assert loaded.cash == candidate.cash


def test_signal_underlying_must_match_open_position(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")
    candidate = _ledger_with_position(underlying="BTC")

    with pytest.raises(
        ValueError,
        match="underlying does not match",
    ):
        store.save_with_signal(
            candidate,
            signal_key=SIGNAL_KEY,
            underlying="ETH",
            candle_closed_ns=CANDLE_CLOSED_NS,
            trade_id=1,
        )

    assert not store.has_consumed_signal(SIGNAL_KEY)


def test_signal_trade_must_identify_open_position(
    tmp_path: Path,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")

    with pytest.raises(
        ValueError,
        match="identify an open position",
    ):
        store.save_with_signal(
            _empty_ledger(),
            signal_key=SIGNAL_KEY,
            underlying="BTC",
            candle_closed_ns=CANDLE_CLOSED_NS,
            trade_id=1,
        )

    assert not store.has_consumed_signal(SIGNAL_KEY)


@pytest.mark.parametrize(
    "signal_key",
    [
        "",
        " ",
        " BTC:1:call",
        "BTC:1:call ",
        "x" * 257,
    ],
)
def test_invalid_signal_key_is_rejected(
    tmp_path: Path,
    signal_key: str,
) -> None:
    store = SQLitePaperLedgerStore(tmp_path / "paper.sqlite")

    with pytest.raises(ValueError, match="signal_key"):
        store.has_consumed_signal(signal_key)
