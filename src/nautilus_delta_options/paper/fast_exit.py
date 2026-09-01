from __future__ import annotations

from collections.abc import Sequence
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
) -> PaperFastExitCycle:
    ledger = session.snapshot()
    positions = ledger.open_positions

    if not positions:
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

    for position in positions:
        position_ticker = tickers_by_symbol.get(position.symbol)

        if position_ticker is None:
            warnings.append(
                f"{position.symbol}: fast exit ticker missing"
            )
            continue

        try:
            closed = session.process_exit_ticker(
                position.trade_id,
                position_ticker,
            )
        except ValueError as error:
            warnings.append(
                f"{position.symbol}: fast exit deferred: {error}"
            )
            continue

        if closed is not None:
            closed_trades.append(closed)

    return PaperFastExitCycle(
        checked_positions=len(positions),
        closed_trades=tuple(closed_trades),
        warnings=tuple(warnings),
    )
