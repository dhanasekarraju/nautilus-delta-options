from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
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
import sys, json, urllib.request, urllib.error
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Public data redirects are not permitted")
meta = {"complete": False, "status": None, "error": None}
try:
    request = urllib.request.Request(sys.argv[1], headers={
        "Accept": "application/json", "User-Agent": "nautilus-delta-options/0.1"
    }, method="GET")
    try:
        response = urllib.request.build_opener(NoRedirect).open(
            request, timeout=float(sys.argv[2]))
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        meta["status"] = response.status
        total = 0
        while True:
            chunk = response.read1(min(65536, 8 * 1024 * 1024 + 1 - total))
            if not chunk:
                meta["complete"] = True
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            total += len(chunk)
            if total > 8 * 1024 * 1024:
                raise ValueError("Public response exceeds size limit")
        if response.status != 200:
            meta["error"] = "HTTP status " + str(response.status)
except Exception as exc:
    meta["error"] = type(exc).__name__ + ": " + str(exc)
finally:
    sys.stderr.write(json.dumps(meta))
"""


@dataclass(frozen=True, slots=True)
class RawPublicResponse:
    url: str
    request_started_utc_ns: int
    response_received_utc_ns: int
    request_elapsed_ns: int
    body: bytes
    sha256: str
    http_status: int | None = None
    body_complete: bool = True
    transport_error: str | None = None
    capture_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def clock_anomaly(self) -> bool:
        return self.response_received_utc_ns < self.request_started_utc_ns

    def json_payload(self) -> object:
        return cast(object, json.loads(self.body))


def raw_delta_public_get(
    url: str,
    *,
    timeout: float,
    stop: Event | None = None,
    on_response: Callable[[RawPublicResponse], None] | None = None,
) -> RawPublicResponse:
    """Fetch approved Delta public data while retaining exact response bytes."""

    _validate_delta_research_url(url)

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Public request timeout must be finite and positive")

    if stop is not None and stop.is_set():
        raise RuntimeError("Public market-data request cancelled during shutdown")

    started_utc_ns = time.time_ns()
    started_monotonic_ns = time.monotonic_ns()
    deadline = time.monotonic() + timeout

    with subprocess.Popen(
        [sys.executable, "-I", "-c", _RAW_WORKER, url, str(timeout)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        failure: Exception | None = None
        try:
            while True:
                if stop is not None and stop.is_set():
                    raise RuntimeError("Public request cancelled during shutdown")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Public market-data deadline exceeded")
                try:
                    output, error = process.communicate(timeout=min(remaining, 0.1))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except Exception as exc:
            failure = exc
            process.kill()
            output, error = process.communicate()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        meta: dict[str, object] = {}
        if error:
            try:
                decoded = json.loads(error)
                if isinstance(decoded, dict):
                    meta = decoded
            except (ValueError, UnicodeError):
                failure = failure or RuntimeError(error.decode(errors="replace")[-500:])
        if process.returncode or meta.get("error") or len(output) > _MAX_RESPONSE_BYTES:
            failure = failure or RuntimeError(
                str(meta.get("error") or "Public response failed or exceeds size limit")
            )
        status = meta.get("status")
        response = RawPublicResponse(
            url=url,
            request_started_utc_ns=started_utc_ns,
            response_received_utc_ns=time.time_ns(),
            request_elapsed_ns=time.monotonic_ns() - started_monotonic_ns,
            body=output,
            sha256=hashlib.sha256(output).hexdigest(),
            http_status=status if isinstance(status, int) else None,
            body_complete=bool(meta.get("complete", failure is None)),
            transport_error=str(failure) if failure else None,
        )
        if on_response is not None:
            on_response(response)
        if failure is not None:
            raise failure
        return response


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
        raise ValueError("Only approved Delta public research endpoints are permitted")


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
    on_response: Callable[[RawPublicResponse], None] | None = None,
) -> CapturedHistorySnapshot:
    """Fetch raw Delta history and parse it with the frozen production parser."""

    if count < 60 or count > 1000:
        raise ValueError("count must be between 60 and 1000")

    resolved_now_s = int(time.time()) if now_s is None else now_s
    if resolved_now_s <= 0:
        raise ValueError("now_s must be positive")

    symbol = f"{underlying}USD"
    start_s = resolved_now_s - (count + 8) * _FIVE_MINUTE_SECONDS

    params = urllib.parse.urlencode(
        {
            "resolution": "5m",
            "symbol": symbol,
            "start": start_s,
            "end": resolved_now_s,
        }
    )
    url = f"{_DELTA_BASE_URL}/v2/history/candles?{params}"

    if on_response is None:
        response = raw_delta_public_get(url, timeout=timeout, stop=stop)
    else:
        response = raw_delta_public_get(
            url,
            timeout=timeout,
            stop=stop,
            on_response=on_response,
        )

    candles = parse_history_candles_payload(
        response.json_payload(),
        resolution="5m",
        now_s=resolved_now_s,
    )

    if len(candles) < count:
        raise ValueError(
            f"Delta returned only {len(candles)} completed 5m candles; {count} required"
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
    on_response: Callable[[RawPublicResponse], None] | None = None,
) -> CapturedOptionChainSnapshot:
    """Fetch raw Delta option-chain data and use the frozen production parser."""

    params = urllib.parse.urlencode(
        {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": underlying,
        }
    )
    url = f"{_DELTA_BASE_URL}/v2/tickers?{params}"

    if on_response is None:
        response = raw_delta_public_get(url, timeout=timeout, stop=stop)
    else:
        response = raw_delta_public_get(
            url,
            timeout=timeout,
            stop=stop,
            on_response=on_response,
        )

    snapshot = parse_option_chain_payload(
        response.json_payload(),
        underlying=underlying,
    )

    return CapturedOptionChainSnapshot(
        response=response,
        snapshot=snapshot,
    )
