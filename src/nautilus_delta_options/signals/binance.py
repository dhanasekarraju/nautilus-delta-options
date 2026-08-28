from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

type SignalUnderlying = Literal["BTC", "ETH"]


@dataclass(frozen=True, slots=True)
class MarketCandle:
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class BinanceCandleSnapshot:
    underlying: SignalUnderlying
    symbol: str
    interval: str
    captured_ms: int
    candles: tuple[MarketCandle, ...]


class BinanceFuturesPublicClient:
    """Read-only Binance Futures client for completed 5m candles."""

    def __init__(
        self,
        *,
        base_url: str = "https://fapi.binance.com",
        timeout_seconds: float = 20.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def fetch_v31_candles(
        self,
        underlying: SignalUnderlying,
    ) -> BinanceCandleSnapshot:
        symbol = f"{underlying}USDT"
        parameters = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": "5m",
                "limit": 201,
            }
        )
        request = urllib.request.Request(
            (f"{self._base_url}/fapi/v1/klines?{parameters}"),
            headers={
                "Accept": "application/json",
                "User-Agent": ("nautilus-delta-options/0.1"),
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=self._timeout_seconds,
        ) as response:
            payload: object = json.load(response)

        captured_ms = time.time_ns() // 1_000_000
        completed = parse_binance_klines(
            payload,
            now_ms=captured_ms,
        )

        if len(completed) < 200:
            raise ValueError(f"{symbol} returned only {len(completed)} completed candles")

        return BinanceCandleSnapshot(
            underlying=underlying,
            symbol=symbol,
            interval="5m",
            captured_ms=captured_ms,
            candles=completed[-200:],
        )


def parse_binance_klines(
    payload: object,
    *,
    now_ms: int,
) -> tuple[MarketCandle, ...]:
    if now_ms <= 0:
        raise ValueError("now_ms must be positive")
    if not isinstance(payload, list):
        raise ValueError("Binance kline payload must be a list")

    rows = cast(list[object], payload)
    completed: list[MarketCandle] = []
    previous_open_time = -1

    for index, row in enumerate(rows):
        candle = _parse_candle(row, index=index)

        if candle.open_time_ms <= previous_open_time:
            raise ValueError("Binance candles must be chronological")
        previous_open_time = candle.open_time_ms

        if candle.close_time_ms <= now_ms:
            completed.append(candle)

    return tuple(completed)


def _parse_candle(
    value: object,
    *,
    index: int,
) -> MarketCandle:
    if not isinstance(value, list) or len(value) < 7:
        raise ValueError(f"Binance candle {index} is malformed")

    row = cast(list[object], value)
    candle = MarketCandle(
        open_time_ms=_integer(
            row[0],
            label=f"candle {index} open_time",
        ),
        open=_decimal(
            row[1],
            label=f"candle {index} open",
        ),
        high=_decimal(
            row[2],
            label=f"candle {index} high",
        ),
        low=_decimal(
            row[3],
            label=f"candle {index} low",
        ),
        close=_decimal(
            row[4],
            label=f"candle {index} close",
        ),
        volume=_decimal(
            row[5],
            label=f"candle {index} volume",
        ),
        close_time_ms=_integer(
            row[6],
            label=f"candle {index} close_time",
        ),
    )

    if candle.close_time_ms < candle.open_time_ms:
        raise ValueError(f"Binance candle {index} closes before it opens")
    if (
        min(
            candle.open,
            candle.high,
            candle.low,
            candle.close,
        )
        <= 0
    ):
        raise ValueError(f"Binance candle {index} prices must be positive")
    if candle.volume < 0:
        raise ValueError(f"Binance candle {index} volume cannot be negative")
    if candle.high < max(candle.open, candle.close):
        raise ValueError(f"Binance candle {index} high is invalid")
    if candle.low > min(candle.open, candle.close):
        raise ValueError(f"Binance candle {index} low is invalid")

    return candle


def _integer(
    value: object,
    *,
    label: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _decimal(
    value: object,
    *,
    label: str,
) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label} must be numeric") from error

    if not result.is_finite():
        raise ValueError(f"{label} must be finite")

    return result
