from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from threading import Event
from typing import cast

from nautilus_delta_options.delta.history import (
    DeltaCandleSnapshot,
    parse_history_candles_payload,
)
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaUnderlying,
    parse_option_chain_payload,
)

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_RAW_WORKER = """
import sys, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Public data redirects are not permitted")
request = urllib.request.Request(
    sys.argv[1],
    headers={
        "Accept": "application/json",
        "User-Agent": "nautilus-delta-options/0.1",
    },
    method="GET",
)
with urllib.request.build_opener(NoRedirect).open(
    request,
    timeout=float(sys.argv[2]),
) as response:
    payload = response.read(8 * 1024 * 1024 + 1)
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("Public response exceeds size limit")
    sys.stdout.buffer.write(payload)
"""


@dataclass(frozen=True, slots=True)
class RawPublicResponse:
    url: str
    request_started_utc_ns: int
    response_received_utc_ns: int
    request_elapsed_ns: int
    body: bytes
    sha256: str

    def json_payload(self) -> object:
        return cast(object, json.loads(self.body))


def raw_delta_public_get(
    url: str,
    *,
    timeout: float,
    stop: Event | None = None,
) -> RawPublicResponse:
    """Fetch approved Delta public data while retaining exact response bytes."""

    _validate_delta_research_url(url)

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(
            "Public request timeout must be finite and positive"
        )

    if stop is not None and stop.is_set():
        raise RuntimeError(
            "Public market-data request cancelled during shutdown"
        )

    started_utc_ns = time.time_ns()
    started_monotonic_ns = time.monotonic_ns()
    deadline = time.monotonic() + timeout

    with subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            _RAW_WORKER,
            url,
            str(timeout),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            while True:
                if stop is not None and stop.is_set():
                    raise RuntimeError(
                        "Public market-data request cancelled during shutdown"
                    )

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Public market-data deadline exceeded"
                    )

                try:
                    output, error = process.communicate(
                        timeout=min(remaining, 0.1)
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue

            if process.returncode:
                raise RuntimeError(
                    "Public market-data request failed: "
                    + error.decode(errors="replace")[-500:]
                )

            if len(output) > _MAX_RESPONSE_BYTES:
                raise ValueError(
                    "Public response exceeds size limit"
                )

            received_utc_ns = time.time_ns()
            elapsed_ns = (
                time.monotonic_ns()
                - started_monotonic_ns
            )

            return RawPublicResponse(
                url=url,
                request_started_utc_ns=started_utc_ns,
                response_received_utc_ns=received_utc_ns,
                request_elapsed_ns=elapsed_ns,
                body=output,
                sha256=hashlib.sha256(output).hexdigest(),
            )

        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def _validate_delta_research_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)

    allowed_paths = {
        "/v2/history/candles",
        "/v2/tickers",
    }

    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.india.delta.exchange"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.path not in allowed_paths
    ):
        raise ValueError(
            "Only approved Delta public research endpoints are permitted"
        )


_DELTA_BASE_URL = "https://api.india.delta.exchange"
_FIVE_MINUTE_SECONDS = 300


@dataclass(frozen=True, slots=True)
class CapturedHistorySnapshot:
    response: RawPublicResponse
    snapshot: DeltaCandleSnapshot


@dataclass(frozen=True, slots=True)
class CapturedOptionChainSnapshot:
    response: RawPublicResponse
    snapshot: DeltaOptionChainSnapshot


def fetch_captured_5m_candles(
    underlying: DeltaUnderlying,
    *,
    count: int = 240,
    now_s: int | None = None,
    timeout: float = 20.0,
    stop: Event | None = None,
) -> CapturedHistorySnapshot:
    """Fetch raw Delta history and parse it with the frozen production parser."""

    if count < 60 or count > 1000:
        raise ValueError("count must be between 60 and 1000")

    resolved_now_s = int(time.time()) if now_s is None else now_s
    if resolved_now_s <= 0:
        raise ValueError("now_s must be positive")

    symbol = f"{underlying}USD"
    start_s = (
        resolved_now_s
        - (count + 8) * _FIVE_MINUTE_SECONDS
    )

    params = urllib.parse.urlencode(
        {
            "resolution": "5m",
            "symbol": symbol,
            "start": start_s,
            "end": resolved_now_s,
        }
    )
    url = f"{_DELTA_BASE_URL}/v2/history/candles?{params}"

    response = raw_delta_public_get(
        url,
        timeout=timeout,
        stop=stop,
    )

    candles = parse_history_candles_payload(
        response.json_payload(),
        resolution="5m",
        now_s=resolved_now_s,
    )

    if len(candles) < count:
        raise ValueError(
            f"Delta returned only {len(candles)} completed 5m candles; "
            f"{count} required"
        )

    snapshot = DeltaCandleSnapshot(
        underlying=underlying,
        symbol=symbol,
        resolution="5m",
        candles=candles[-count:],
        captured_ns=response.request_started_utc_ns,
    )

    return CapturedHistorySnapshot(
        response=response,
        snapshot=snapshot,
    )


def fetch_captured_option_chain(
    underlying: DeltaUnderlying,
    *,
    timeout: float = 20.0,
    stop: Event | None = None,
) -> CapturedOptionChainSnapshot:
    """Fetch raw Delta option-chain data and use the frozen production parser."""

    params = urllib.parse.urlencode(
        {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": underlying,
        }
    )
    url = f"{_DELTA_BASE_URL}/v2/tickers?{params}"

    response = raw_delta_public_get(
        url,
        timeout=timeout,
        stop=stop,
    )

    snapshot = parse_option_chain_payload(
        response.json_payload(),
        underlying=underlying,
    )

    return CapturedOptionChainSnapshot(
        response=response,
        snapshot=snapshot,
    )
