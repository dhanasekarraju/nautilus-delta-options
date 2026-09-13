from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.paper.ledger import PaperClosedTrade
from nautilus_delta_options.paper.session import PaperLedgerSession


class DeltaFastExitMarketData(Protocol):
    def fetch_option_tickers(
        self,
        symbols: Sequence[str],
    ) -> tuple[DeltaOptionTicker, ...]: ...


@dataclass(frozen=True, slots=True)
class PaperFastExitCycle:
    checked_positions: int
    closed_trades: tuple[PaperClosedTrade, ...]
    warnings: tuple[str, ...]


def run_fast_exit_cycle(
    *,
    client: DeltaFastExitMarketData,
    session: PaperLedgerSession,
    clock_ns: Callable[[], int] = time.time_ns,
    apply_time_policy: bool = False,
    extra_symbols: tuple[str, ...] = (),
    on_quote: Callable[[DeltaOptionTicker, int], None] | None = None,
) -> PaperFastExitCycle:
    ledger = session.snapshot()
    positions = ledger.open_positions

    if not positions and not extra_symbols:
        return PaperFastExitCycle(
            checked_positions=0,
            closed_trades=(),
            warnings=(),
        )

    symbols = tuple(
        position.symbol
        for position in positions
    )

    tickers: list[DeltaOptionTicker] = []

    for start in range(0, len(symbols), 10):
        tickers.extend(
            client.fetch_option_tickers(
                symbols[start : start + 10],
            )
        )

    tickers_by_symbol: dict[str, DeltaOptionTicker] = {}

    for ticker in tickers:
        if ticker.symbol in tickers_by_symbol:
            raise ValueError(
                "Fast exit ticker response contains "
                f"duplicate symbol: {ticker.symbol}"
            )

        tickers_by_symbol[ticker.symbol] = ticker

    closed_trades: list[PaperClosedTrade] = []
    warnings: list[str] = []

    observed_by_symbol: dict[str, int] = {}

    for position in positions:
        position_ticker = tickers_by_symbol.get(position.symbol)

        if position_ticker is None:
            session.note_unresolved(position.trade_id, "fast exit ticker missing")
            warnings.append(
                f"{position.symbol}: fast exit ticker missing"
            )
            continue

        try:
            observed_ns = clock_ns()
            observed_by_symbol[position.symbol] = observed_ns
            closed = session.process_exit_ticker(
                position.trade_id,
                position_ticker,
                observed_ns=observed_ns,
                apply_time_policy=apply_time_policy,
            )
        except ValueError as error:
            if any(p.trade_id == position.trade_id for p in session.snapshot().open_positions):
                session.note_unresolved(position.trade_id, str(error))
            warnings.append(
                f"{position.symbol}: fast exit deferred: {error}"
            )
            continue

        if closed is not None:
            closed_trades.append(closed)

    # Extra research contracts and journal latency must not block primary exits.
    if on_quote is not None:
        for ticker in tickers:
            try:
                stamp = observed_by_symbol.get(ticker.symbol)
                on_quote(ticker, clock_ns() if stamp is None else stamp)
            except Exception as error:
                warnings.append(f"Research observation failed: {error}")
        for symbol in dict.fromkeys(extra_symbols):
            if symbol in symbols:
                continue
            try:
                extra = client.fetch_option_tickers((symbol,))
                if len(extra) != 1 or extra[0].symbol != symbol:
                    raise ValueError("Missing or mismatched research quote")
                on_quote(extra[0], clock_ns())
            except Exception as error:
                warnings.append(f"{symbol}: research observation deferred: {error}")

    return PaperFastExitCycle(
        checked_positions=len(positions),
        closed_trades=tuple(closed_trades),
        warnings=tuple(warnings),
    )
