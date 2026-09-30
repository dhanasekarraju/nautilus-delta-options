from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from nautilus_delta_options.delta.history import (
    DeltaCandle,
    DeltaCandleSnapshot,
)
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
)
from nautilus_delta_options.research.experiment1.capture import (
    CapturedHistorySnapshot,
    CapturedOptionChainSnapshot,
    RawPublicResponse,
)
from nautilus_delta_options.research.experiment1.market_data import (
    Experiment1CapturedMarketData,
)


def _raw(
    url: str,
    body: bytes,
) -> RawPublicResponse:
    return RawPublicResponse(
        url=url,
        request_started_utc_ns=100,
        response_received_utc_ns=101,
        request_elapsed_ns=1,
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
    )


def _captured_history(
    underlying: str,
) -> CapturedHistorySnapshot:
    body = json.dumps(
        {
            "success": True,
            "result": [],
        },
        separators=(",", ":"),
    ).encode()

    snapshot = DeltaCandleSnapshot(
        underlying=underlying,  # type: ignore[arg-type]
        symbol=f"{underlying}USD",
        resolution="5m",
        candles=(
            DeltaCandle(
                time_s=1_800_000_000,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("10"),
            ),
        ),
        captured_ns=100,
    )

    return CapturedHistorySnapshot(
        response=_raw(
            "https://api.india.delta.exchange"
            "/v2/history/candles",
            body,
        ),
        snapshot=snapshot,
    )


def _captured_chain(
    underlying: str,
) -> CapturedOptionChainSnapshot:
    body = b'{"success":true,"result":[]}'

    return CapturedOptionChainSnapshot(
        response=_raw(
            "https://api.india.delta.exchange/v2/tickers",
            body,
        ),
        snapshot=DeltaOptionChainSnapshot(
            underlying=underlying,  # type: ignore[arg-type]
            tickers=(),
            rejected_records=(),
        ),
    )


def test_history_returns_exact_retained_snapshot() -> None:
    calls: list[
        tuple[str, int, int | None, float]
    ] = []

    def history_fetcher(
        underlying: str,
        *,
        count: int,
        now_s: int | None,
        timeout: float,
        stop,
    ) -> CapturedHistorySnapshot:
        del stop

        calls.append(
            (
                underlying,
                count,
                now_s,
                timeout,
            )
        )

        return _captured_history(underlying)

    market = Experiment1CapturedMarketData(
        timeout_seconds=7.5,
        history_fetcher=history_fetcher,
    )

    snapshot = market.fetch_5m_candles(
        "BTC",
        count=240,
        now_s=123,
    )

    captured = market.captured_history("BTC")

    assert snapshot is captured.snapshot
    assert calls == [
        ("BTC", 240, 123, 7.5),
    ]


def test_option_chain_returns_exact_retained_snapshot() -> None:
    calls: list[tuple[str, float]] = []

    def option_fetcher(
        underlying: str,
        *,
        timeout: float,
        stop,
    ) -> CapturedOptionChainSnapshot:
        del stop

        calls.append(
            (
                underlying,
                timeout,
            )
        )

        return _captured_chain(underlying)

    market = Experiment1CapturedMarketData(
        timeout_seconds=6.0,
        option_chain_fetcher=option_fetcher,
    )

    snapshot = market.fetch_option_chain("ETH")

    captured = market.captured_option_chain("ETH")

    assert snapshot is captured.snapshot
    assert calls == [
        ("ETH", 6.0),
    ]


def test_capture_state_is_isolated_per_underlying() -> None:
    def history_fetcher(
        underlying: str,
        **kwargs,
    ) -> CapturedHistorySnapshot:
        del kwargs
        return _captured_history(underlying)

    def option_fetcher(
        underlying: str,
        **kwargs,
    ) -> CapturedOptionChainSnapshot:
        del kwargs
        return _captured_chain(underlying)

    market = Experiment1CapturedMarketData(
        history_fetcher=history_fetcher,
        option_chain_fetcher=option_fetcher,
    )

    market.fetch_5m_candles("BTC")
    market.fetch_5m_candles("ETH")
    market.fetch_option_chain("BTC")
    market.fetch_option_chain("ETH")

    assert (
        market.captured_history("BTC").snapshot.underlying
        == "BTC"
    )
    assert (
        market.captured_history("ETH").snapshot.underlying
        == "ETH"
    )
    assert (
        market.captured_option_chain(
            "BTC"
        ).snapshot.underlying
        == "BTC"
    )
    assert (
        market.captured_option_chain(
            "ETH"
        ).snapshot.underlying
        == "ETH"
    )


def test_missing_capture_is_explicit() -> None:
    market = Experiment1CapturedMarketData()

    with pytest.raises(
        RuntimeError,
        match="no captured history",
    ):
        market.captured_history("BTC")

    with pytest.raises(
        RuntimeError,
        match="no captured option chain",
    ):
        market.captured_option_chain("BTC")


def test_clear_underlying_removes_only_that_asset() -> None:
    def history_fetcher(
        underlying: str,
        **kwargs,
    ) -> CapturedHistorySnapshot:
        del kwargs
        return _captured_history(underlying)

    def option_fetcher(
        underlying: str,
        **kwargs,
    ) -> CapturedOptionChainSnapshot:
        del kwargs
        return _captured_chain(underlying)

    market = Experiment1CapturedMarketData(
        history_fetcher=history_fetcher,
        option_chain_fetcher=option_fetcher,
    )

    for underlying in ("BTC", "ETH"):
        market.fetch_5m_candles(underlying)  # type: ignore[arg-type]
        market.fetch_option_chain(underlying)  # type: ignore[arg-type]

    market.clear_underlying("BTC")

    with pytest.raises(RuntimeError):
        market.captured_history("BTC")

    with pytest.raises(RuntimeError):
        market.captured_option_chain("BTC")

    assert (
        market.captured_history("ETH").snapshot.underlying
        == "ETH"
    )
    assert (
        market.captured_option_chain(
            "ETH"
        ).snapshot.underlying
        == "ETH"
    )
