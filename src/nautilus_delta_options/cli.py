from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from nautilus_delta_options.delta.public_client import (
    DeltaPublicClient,
    DeltaUnderlying,
)
from nautilus_delta_options.paper.ledger import PaperLedger
from nautilus_delta_options.paper.observer import (
    PaperDryRunCycle,
    PaperDryRunObserver,
)
from nautilus_delta_options.paper.persistence import (
    SQLitePaperLedgerStore,
)
from nautilus_delta_options.paper.session import PaperLedgerSession


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)

    database = cast(Path, arguments.database)
    initial_cash = cast(Decimal, arguments.initial_cash)
    minimum_reward_risk = cast(
        Decimal,
        arguments.minimum_reward_risk,
    )
    max_positions = cast(int, arguments.max_positions)
    underlying_values = cast(
        list[str],
        arguments.underlyings,
    )
    underlyings = cast(
        tuple[DeltaUnderlying, ...],
        tuple(underlying_values),
    )

    interval_seconds = cast(
        int | None,
        arguments.interval_seconds,
    )

    store = SQLitePaperLedgerStore(database)
    session = PaperLedgerSession.load_or_create(
        store,
        initial_cash=initial_cash,
        minimum_reward_risk=minimum_reward_risk,
        max_positions=max_positions,
    )
    observer = PaperDryRunObserver(
        client=DeltaPublicClient(),
        session=session,
        underlyings=underlyings,
    )
    if interval_seconds is None:
        cycle = observer.run_cycle()
        print(render_cycle_report(cycle, session.ledger))
        return 0

    try:
        while True:
            cycle = observer.run_cycle()
            print(
                render_cycle_report(cycle, session.ledger),
                flush=True,
            )
            print("\n" + "=" * 80, flush=True)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        return 0


def render_cycle_report(
    cycle: PaperDryRunCycle,
    ledger: PaperLedger,
) -> str:
    lines = [
        "NAUTILUS DELTA OPTIONS",
        "MODE: PAPER OBSERVER — ENTRIES DISABLED",
        f"Cycle date: {cycle.as_of.isoformat()}",
        "",
        "PAPER WALLET",
        f"Cash: {ledger.cash:.8f}",
        f"Open positions: {len(ledger.open_positions)}",
        f"Closed trades: {len(ledger.closed_trades)}",
        f"Cycle exits: {len(cycle.closed_trades)}",
    ]

    for snapshot in cycle.snapshots:
        eligible = sum(record.eligibility.eligible for record in snapshot.records)
        lines.extend(
            (
                "",
                snapshot.underlying,
                f"Products: {snapshot.product_count}",
                f"Tickers: {snapshot.ticker_count}",
                f"Joined: {len(snapshot.records)}",
                f"Eligible: {eligible}",
                f"Snapshot errors: {len(snapshot.errors)}",
            )
        )

    lines.extend(
        (
            "",
            "PROPOSALS ONLY — NO ENTRIES",
        )
    )

    if not cycle.proposals:
        lines.append("No payoff-approved proposal this cycle.")

    for number, proposal in enumerate(
        cycle.proposals,
        start=1,
    ):
        ticker = proposal.record.ticker
        sizing = proposal.sizing
        levels = proposal.levels
        spread_fraction = ticker.spread_fraction
        spread_percent = (
            spread_fraction * Decimal("100") if spread_fraction is not None else Decimal("0")
        )

        lines.extend(
            (
                "",
                f"#{number} {ticker.symbol}",
                f"Side: {ticker.contract_type}",
                f"DTE: {proposal.record.eligibility.dte}",
                f"Bid / Ask: {ticker.best_bid} / {ticker.best_ask}",
                f"Spread: {spread_percent:.4f}%",
                f"Contracts: {sizing.contracts}",
                f"Entry debit: {sizing.total_entry_debit:.8f}",
                f"Planned loss: {sizing.total_planned_loss:.8f}",
                f"Planned reward: {sizing.total_planned_reward:.8f}",
                f"Net reward/risk: {sizing.reward_risk_ratio:.4f}",
                f"Stop bid: {levels.stop_exit_bid}",
                f"Target bid: {levels.target_exit_bid}",
                f"Sizing limit: {sizing.limiting_factor.value}",
            )
        )

    if cycle.warnings:
        lines.extend(("", "WARNINGS"))
        lines.extend(f"- {warning}" for warning in cycle.warnings)

    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nautilus-delta-options",
        description=(
            "Observe Delta India BTC/ETH options and produce "
            "payoff-gated paper proposals without entering trades."
        ),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/paper-ledger.sqlite"),
        help="SQLite paper-ledger path.",
    )
    parser.add_argument(
        "--initial-cash",
        type=_decimal_argument,
        default=Decimal("250"),
        help="Initial virtual wallet balance.",
    )
    parser.add_argument(
        "--minimum-reward-risk",
        type=_decimal_argument,
        default=Decimal("1.5"),
        help="Minimum net reward/risk after costs.",
    )
    parser.add_argument(
        "--max-positions",
        type=int,
        default=3,
        help="Maximum simultaneous paper positions.",
    )
    parser.add_argument(
        "--underlyings",
        nargs="+",
        choices=("BTC", "ETH"),
        default=["BTC", "ETH"],
        help="Option underlyings to observe.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=_positive_int_argument,
        default=None,
        help=("Repeat observation using this interval; omit for one cycle."),
    )
    return parser


def _decimal_argument(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"Invalid decimal value: {value}") from error

    if not result.is_finite():
        raise argparse.ArgumentTypeError("Decimal value must be finite")

    return result


def _positive_int_argument(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Invalid integer value: {value}") from error

    if result <= 0:
        raise argparse.ArgumentTypeError("Value must be positive")

    return result
