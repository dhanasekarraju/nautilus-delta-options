import json
import subprocess
import threading
import time

import pytest

from nautilus_delta_options import public_http
from nautilus_delta_options.delta.history import DeltaHistoryClient
from nautilus_delta_options.delta.public_client import DeltaPublicClient
from nautilus_delta_options.signals.binance import BinanceFuturesPublicClient

URL = "https://api.india.delta.exchange/v2/products"


def test_hung_public_request_is_killed_and_next_request_recovers(monkeypatch):
    children = []
    popen = subprocess.Popen

    def capture(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(public_http.subprocess, "Popen", capture)
    monkeypatch.setattr(public_http, "_WORKER", "import time; time.sleep(60)")
    before = time.monotonic()
    for _ in range(2):
        with pytest.raises(TimeoutError):
            public_http.public_json(URL, timeout=0.15)
        assert children[-1].poll() is not None
    assert time.monotonic() - before < 3
    monkeypatch.setattr(public_http, "_WORKER", 'print(\'{"success": true}\')')
    assert public_http.public_json(URL, timeout=3) == {"success": True}
    assert len(children) == 3  # Exactly one child per request, no retries.


def test_shutdown_kills_hung_request_and_prevents_followup(monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(public_http, "_WORKER", "import time; time.sleep(60)")
    timer = threading.Timer(0.15, stop.set)
    timer.start()
    before = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="cancelled"):
            public_http.public_json(URL, timeout=20, stop=stop)
        assert time.monotonic() - before < 3
        with pytest.raises(RuntimeError, match="cancelled"):
            public_http.public_json(URL, timeout=20, stop=stop)
    finally:
        timer.join()


@pytest.mark.parametrize("url", [
    "https://api.india.delta.exchange/v2/orders",
    "https://api.india.delta.exchange/v2/balances",
    "http://api.india.delta.exchange/v2/products",
    "https://user:secret@api.india.delta.exchange/v2/products",
    "https://example.org/v2/products",
    "https://api.india.delta.exchange:444/v2/products",
])
def test_non_public_endpoints_fail_before_spawn(url, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("spawned forbidden request")
    monkeypatch.setattr(public_http.subprocess, "Popen", forbidden)
    with pytest.raises(ValueError, match="public market-data"):
        public_http.public_json(url, timeout=1)


@pytest.mark.parametrize(
    "client", [DeltaPublicClient, DeltaHistoryClient, BinanceFuturesPublicClient],
)
@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
def test_client_timeout_validation(client, timeout):
    with pytest.raises(ValueError):
        client(timeout_seconds=timeout)


def test_invalid_json_is_reported_and_subsequent_request_recovers(monkeypatch):
    monkeypatch.setattr(public_http, "_WORKER", "print('not JSON')")
    with pytest.raises(json.JSONDecodeError):
        public_http.public_json(URL, timeout=3)
    monkeypatch.setattr(public_http, "_WORKER", "print('[]')")
    assert public_http.public_json(URL, timeout=3) == []


def test_binance_public_fetch_uses_bounded_transport(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from nautilus_delta_options.signals import binance
    calls = []
    stop = threading.Event()

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return []  # Valid transport payload, deliberately insufficient candle history.

    monkeypatch.setattr(binance, "public_json", fetch)
    with pytest.raises(ValueError, match="only 0"):
        BinanceFuturesPublicClient(stop_event=stop).fetch_v31_candles("BTC")
    url, kwargs = calls[0]
    assert urlsplit(url).path == "/fapi/v1/klines"
    assert parse_qs(urlsplit(url).query)["symbol"] == ["BTCUSDT"]
    assert kwargs == {"timeout": 20.0, "stop": stop}


@pytest.mark.parametrize("suffix", ["../orders", "%2e%2e%2forders", "C-BTC/../../orders"])
def test_ticker_path_cannot_escape_public_endpoint(suffix):
    with pytest.raises(ValueError):
        public_http.public_json(
            "https://api.india.delta.exchange/v2/tickers/" + suffix, timeout=1,
        )
