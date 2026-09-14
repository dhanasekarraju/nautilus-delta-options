from __future__ import annotations

import math
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import Event
from typing import Literal, cast

from nautilus_delta_options.delta.public_client import DeltaUnderlying
from nautilus_delta_options.public_http import public_json

type DeltaCandleResolution = Literal["5m"]

_RESOLUTION_SECONDS: dict[DeltaCandleResolution, int] = {"5m": 300}


@dataclass(frozen=True, slots=True)
class DeltaCandle:
    time_s: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def close_ms(self, resolution: DeltaCandleResolution) -> int:
        return (self.time_s + _RESOLUTION_SECONDS[resolution]) * 1_000 - 1


@dataclass(frozen=True, slots=True)
class DeltaCandleSnapshot:
    underlying: DeltaUnderlying
    symbol: str
    resolution: DeltaCandleResolution
    candles: tuple[DeltaCandle, ...]
    captured_ns: int

    @property
    def candle_close_ms(self) -> int:
        if not self.candles:
            raise ValueError("Candle snapshot is empty")
        return self.candles[-1].close_ms(self.resolution)


class DeltaHistoryClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.india.delta.exchange",
        timeout_seconds: float = 20.0,
        stop_event: Event | None = None,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("base_url must use HTTPS")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._stop_event = stop_event

    def fetch_5m_candles(
        self,
        underlying: DeltaUnderlying,
        *,
        count: int = 240,
        now_s: int | None = None,
    ) -> DeltaCandleSnapshot:
        if count < 60 or count > 1000:
            raise ValueError("count must be between 60 and 1000")

        captured_ns = time.time_ns()
        resolved_now_s = int(time.time()) if now_s is None else now_s
        if resolved_now_s <= 0:
            raise ValueError("now_s must be positive")

        resolution: DeltaCandleResolution = "5m"
        seconds = _RESOLUTION_SECONDS[resolution]
        symbol = f"{underlying}USD"
        start_s = resolved_now_s - (count + 8) * seconds

        params = urllib.parse.urlencode(
            {
                "resolution": resolution,
                "symbol": symbol,
                "start": start_s,
                "end": resolved_now_s,
            }
        )
        url = f"{self._base_url}/v2/history/candles?{params}"
        payload = public_json(url, timeout=self._timeout_seconds, stop=self._stop_event)

        candles = parse_history_candles_payload(
            payload,
            resolution=resolution,
            now_s=resolved_now_s,
        )

        if len(candles) < count:
            raise ValueError(
                f"Delta returned only {len(candles)} completed 5m candles; "
                f"{count} required"
            )

        return DeltaCandleSnapshot(
            underlying=underlying,
            symbol=symbol,
            resolution=resolution,
            candles=candles[-count:],
            captured_ns=captured_ns,
        )


def parse_history_candles_payload(
    payload: object,
    *,
    resolution: DeltaCandleResolution = "5m",
    now_s: int,
) -> tuple[DeltaCandle, ...]:
    if not isinstance(payload, Mapping):
        raise ValueError("Delta candle response must be an object")

    response = cast(Mapping[str, object], payload)
    if response.get("success") is not True:
        raise ValueError("Delta candle response indicated failure")

    raw_result = response.get("result")
    if not isinstance(raw_result, list):
        raise ValueError("Delta candle response result must be a list")

    seconds = _RESOLUTION_SECONDS[resolution]
    by_time: dict[int, DeltaCandle] = {}

    for index, raw in enumerate(cast(list[object], raw_result)):
        if not isinstance(raw, Mapping):
            raise ValueError(f"candle record {index} must be an object")

        record = cast(Mapping[str, object], raw)
        candle = DeltaCandle(
            time_s=_integer(record.get("time"), "time"),
            open=_decimal(record.get("open"), "open"),
            high=_decimal(record.get("high"), "high"),
            low=_decimal(record.get("low"), "low"),
            close=_decimal(record.get("close"), "close"),
            volume=_decimal(record.get("volume", 0), "volume"),
        )

        if candle.time_s + seconds > now_s:
            continue
        if candle.low > candle.high:
            raise ValueError(f"candle record {index} has low above high")
        if not candle.low <= candle.open <= candle.high:
            raise ValueError(f"candle record {index} open outside high/low")
        if not candle.low <= candle.close <= candle.high:
            raise ValueError(f"candle record {index} close outside high/low")
        if candle.volume < 0:
            raise ValueError(f"candle record {index} has negative volume")

        by_time[candle.time_s] = candle

    ordered = tuple(by_time[key] for key in sorted(by_time))
    for i, candle in enumerate(ordered):
        if candle.time_s < 0 or candle.time_s % seconds:
            raise ValueError("Candle timestamps must align to resolution")
        if min(candle.open, candle.high, candle.low, candle.close) <= 0:
            raise ValueError("Candle prices must be positive")
        if i and candle.time_s - ordered[i - 1].time_s != seconds:
            raise ValueError("Candle history contains a gap")
    return ordered


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field} must be an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{field} must be numeric")
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError(f"{field} must be finite")
        return result
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be numeric") from exc
