"""Bounded public GETs: isolate DNS/read stalls so shutdown can kill the request."""

import json
import math
import re
import subprocess
import sys
import time
import urllib.parse
from threading import Event
from typing import cast

_WORKER = """
import sys, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Public data redirects are not permitted")
request = urllib.request.Request(
    sys.argv[1], headers={"Accept": "application/json",
                         "User-Agent": "nautilus-delta-options/0.1"}, method="GET")
with urllib.request.build_opener(NoRedirect).open(request, timeout=float(sys.argv[2])) as response:
    payload = response.read(8 * 1024 * 1024 + 1)
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("Public response exceeds size limit")
    sys.stdout.buffer.write(payload)
"""


def public_json(url: str, *, timeout: float, stop: Event | None = None) -> object:
    parsed = urllib.parse.urlsplit(url)
    allowed = {
        "api.india.delta.exchange": ("/v2/products", "/v2/tickers", "/v2/history/candles"),
        "fapi.binance.com": ("/fapi/v1/klines",),
    }
    paths = allowed.get(parsed.hostname or "", ())
    valid_path = parsed.path in paths or (
        parsed.hostname == "api.india.delta.exchange"
        and re.fullmatch(r"/v2/tickers/[A-Za-z0-9,_-]+", parsed.path) is not None
    )
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or not valid_path):
        raise ValueError("Only approved public market-data GET endpoints are permitted")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Public request timeout must be finite and positive")
    if stop is not None and stop.is_set():
        raise RuntimeError("Public market-data request cancelled during shutdown")
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        [sys.executable, "-I", "-c", _WORKER, url, str(timeout)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as process:
        try:
            while True:
                if stop is not None and stop.is_set():
                    raise RuntimeError("Public market-data request cancelled during shutdown")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Public market-data deadline exceeded")
                try:
                    output, error = process.communicate(timeout=min(remaining, 0.1))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise RuntimeError("Public market-data request failed: " + error.decode()[-500:])
            return cast(object, json.loads(output))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
