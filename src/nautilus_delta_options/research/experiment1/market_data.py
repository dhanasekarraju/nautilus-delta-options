from __future__ import annotations

from collections.abc import Callable
from threading import Event

from nautilus_delta_options.delta.history import (
    DeltaCandleSnapshot,
)
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaUnderlying,
)
from nautilus_delta_options.research.experiment1.capture import (
    CapturedHistorySnapshot,
    CapturedOptionChainSnapshot,
    fetch_captured_5m_candles,
    fetch_captured_option_chain,
)

HistoryFetcher = Callable[..., CapturedHistorySnapshot]
OptionChainFetcher = Callable[..., CapturedOptionChainSnapshot]


class Experiment1CapturedMarketData:
    """Frozen-parser market data with exact raw capture retention.

    The observer receives the same parsed snapshot objects produced
    by the capture functions. The corresponding raw-response wrapper
    remains available for Experiment 1 evidence persistence.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        stop: Event | None = None,
        history_fetcher: HistoryFetcher = (
            fetch_captured_5m_candles
        ),
        option_chain_fetcher: OptionChainFetcher = (
            fetch_captured_option_chain
        ),
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(
                "timeout_seconds must be positive"
            )

        self._timeout_seconds = timeout_seconds
        self._stop = stop
        self._history_fetcher = history_fetcher
        self._option_chain_fetcher = option_chain_fetcher

        self._history: dict[
            DeltaUnderlying,
            CapturedHistorySnapshot,
        ] = {}

        self._option_chains: dict[
            DeltaUnderlying,
            CapturedOptionChainSnapshot,
        ] = {}

    def fetch_5m_candles(
        self,
        underlying: DeltaUnderlying,
        *,
        count: int = 240,
        now_s: int | None = None,
    ) -> DeltaCandleSnapshot:
        captured = self._history_fetcher(
            underlying,
            count=count,
            now_s=now_s,
            timeout=self._timeout_seconds,
            stop=self._stop,
        )

        if captured.snapshot.underlying != underlying:
            raise RuntimeError(
                "captured history underlying changed"
            )

        self._history[underlying] = captured

        return captured.snapshot

    def fetch_option_chain(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionChainSnapshot:
        captured = self._option_chain_fetcher(
            underlying,
            timeout=self._timeout_seconds,
            stop=self._stop,
        )

        if captured.snapshot.underlying != underlying:
            raise RuntimeError(
                "captured option-chain underlying changed"
            )

        self._option_chains[underlying] = captured

        return captured.snapshot

    def captured_history(
        self,
        underlying: DeltaUnderlying,
    ) -> CapturedHistorySnapshot:
        try:
            return self._history[underlying]
        except KeyError as error:
            raise RuntimeError(
                "no captured history exists for underlying"
            ) from error

    def captured_option_chain(
        self,
        underlying: DeltaUnderlying,
    ) -> CapturedOptionChainSnapshot:
        try:
            return self._option_chains[underlying]
        except KeyError as error:
            raise RuntimeError(
                "no captured option chain exists for underlying"
            ) from error

    def clear_underlying(
        self,
        underlying: DeltaUnderlying,
    ) -> None:
        self._history.pop(underlying, None)
        self._option_chains.pop(underlying, None)
