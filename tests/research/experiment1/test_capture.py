from __future__ import annotations

import hashlib
import subprocess
import threading
import time

import pytest

from nautilus_delta_options.research.experiment1 import capture

HISTORY_URL = (
    "https://api.india.delta.exchange/v2/history/candles"
    "?resolution=5m&symbol=BTCUSD&start=100&end=200"
)

CHAIN_URL = (
    "https://api.india.delta.exchange/v2/tickers"
    "?contract_types=call_options%2Cput_options"
    "&underlying_asset_symbols=BTC"
)


def test_raw_response_bytes_are_retained_and_hashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b'{ "success" : true, "result" : [] }\n'

    worker = (
        "import sys; "
        f"sys.stdout.buffer.write({raw!r})"
    )
    monkeypatch.setattr(capture, "_RAW_WORKER", worker)

    response = capture.raw_delta_public_get(
        HISTORY_URL,
        timeout=3,
    )

    assert response.body == raw
    assert response.sha256 == hashlib.sha256(raw).hexdigest()
    assert response.json_payload() == {
        "success": True,
        "result": [],
    }
    assert response.request_elapsed_ns >= 0
    assert response.clock_anomaly == (
        response.response_received_utc_ns
        < response.request_started_utc_ns
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://api.india.delta.exchange/v2/orders",
        "https://api.india.delta.exchange/v2/products",
        "http://api.india.delta.exchange/v2/tickers",
        "https://user:secret@api.india.delta.exchange/v2/tickers",
        "https://example.org/v2/tickers",
        "https://api.india.delta.exchange:444/v2/tickers",
        "https://api.india.delta.exchange/v2/tickers/ABC",
    ],
)
def test_forbidden_endpoint_fails_before_spawn(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        pytest.fail("spawned forbidden request")

    monkeypatch.setattr(
        capture.subprocess,
        "Popen",
        forbidden,
    )

    with pytest.raises(
        ValueError,
        match="approved Delta public research endpoints",
    ):
        capture.raw_delta_public_get(url, timeout=1)


def test_hung_raw_request_is_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def record_child(*args: object, **kwargs: object):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(
        capture.subprocess,
        "Popen",
        record_child,
    )
    monkeypatch.setattr(
        capture,
        "_RAW_WORKER",
        "import time; time.sleep(60)",
    )

    before = time.monotonic()

    with pytest.raises(TimeoutError):
        capture.raw_delta_public_get(
            HISTORY_URL,
            timeout=0.15,
        )

    assert time.monotonic() - before < 3
    assert len(children) == 1
    assert children[0].poll() is not None


def test_shutdown_cancels_raw_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()

    monkeypatch.setattr(
        capture,
        "_RAW_WORKER",
        "import time; time.sleep(60)",
    )

    timer = threading.Timer(0.15, stop.set)
    timer.start()

    try:
        with pytest.raises(RuntimeError, match="cancelled"):
            capture.raw_delta_public_get(
                CHAIN_URL,
                timeout=20,
                stop=stop,
            )

        with pytest.raises(RuntimeError, match="cancelled"):
            capture.raw_delta_public_get(
                CHAIN_URL,
                timeout=20,
                stop=stop,
            )
    finally:
        timer.join()


def test_invalid_json_preserves_raw_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"not JSON\n"

    monkeypatch.setattr(
        capture,
        "_RAW_WORKER",
        (
            "import sys; "
            f"sys.stdout.buffer.write({raw!r})"
        ),
    )

    response = capture.raw_delta_public_get(
        CHAIN_URL,
        timeout=3,
    )

    assert response.body == raw
    assert response.sha256 == hashlib.sha256(raw).hexdigest()

    with pytest.raises(ValueError):
        response.json_payload()


def _raw_response(
    url: str,
    payload: object,
) -> capture.RawPublicResponse:
    import json

    body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")

    return capture.RawPublicResponse(
        url=url,
        request_started_utc_ns=1_900_000_000_000_000_000,
        response_received_utc_ns=1_900_000_000_100_000_000,
        request_elapsed_ns=100_000_000,
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
    )


def test_captured_history_uses_frozen_parser_and_production_url_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_s = 1_800_000_000
    count = 60

    result = [
        {
            "time": now_s - ((count - index) * 300),
            "open": "100",
            "high": "102",
            "low": "99",
            "close": "101",
            "volume": "10",
        }
        for index in range(count)
    ]

    calls: list[tuple[str, float, object]] = []

    def fake_get(
        url: str,
        *,
        timeout: float,
        stop: object = None,
    ) -> capture.RawPublicResponse:
        calls.append((url, timeout, stop))
        return _raw_response(
            url,
            {
                "success": True,
                "result": result,
            },
        )

    monkeypatch.setattr(
        capture,
        "raw_delta_public_get",
        fake_get,
    )

    captured = capture.fetch_captured_5m_candles(
        "BTC",
        count=count,
        now_s=now_s,
        timeout=7.5,
    )

    assert len(captured.snapshot.candles) == count
    assert captured.snapshot.underlying == "BTC"
    assert captured.snapshot.symbol == "BTCUSD"
    assert captured.snapshot.resolution == "5m"
    assert captured.snapshot.candles[-1].time_s == now_s - 300
    assert (
        captured.snapshot.captured_ns
        == captured.response.request_started_utc_ns
    )

    assert len(calls) == 1

    from urllib.parse import parse_qs, urlsplit

    url, timeout, stop = calls[0]
    parsed = urlsplit(url)

    assert parsed.scheme == "https"
    assert parsed.hostname == "api.india.delta.exchange"
    assert parsed.path == "/v2/history/candles"

    query = parse_qs(parsed.query)
    assert query == {
        "resolution": ["5m"],
        "symbol": ["BTCUSD"],
        "start": [str(now_s - (count + 8) * 300)],
        "end": [str(now_s)],
    }

    assert timeout == 7.5
    assert stop is None


def test_captured_history_rejects_insufficient_completed_population(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_s = 1_800_000_000

    result = [
        {
            "time": now_s - ((59 - index) * 300),
            "open": "100",
            "high": "102",
            "low": "99",
            "close": "101",
            "volume": "10",
        }
        for index in range(59)
    ]

    def fake_get(
        url: str,
        *,
        timeout: float,
        stop: object = None,
    ) -> capture.RawPublicResponse:
        return _raw_response(
            url,
            {
                "success": True,
                "result": result,
            },
        )

    monkeypatch.setattr(
        capture,
        "raw_delta_public_get",
        fake_get,
    )

    with pytest.raises(
        ValueError,
        match="only 59 completed 5m candles",
    ):
        capture.fetch_captured_5m_candles(
            "ETH",
            count=60,
            now_s=now_s,
        )


def test_captured_option_chain_uses_frozen_parser_and_production_url_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticker = {
        "product_id": 149236,
        "symbol": "P-BTC-94000-301026",
        "underlying_asset_symbol": "BTC",
        "contract_type": "put_options",
        "strike_price": "94000",
        "mark_price": "14315.31",
        "spot_price": "80521.7",
        "contract_value": "0.001",
        "tick_size": "0.1",
        "quotes": {
            "best_ask": "14419",
            "best_bid": "14209",
            "mark_iv": "0.3371",
            "bid_iv": "0.3241",
            "ask_iv": "0.3508",
            "ask_size": "4080",
            "bid_size": "6085",
        },
        "greeks": {
            "delta": "-0.8479",
            "theta": "-21.0263",
            "gamma": "0.00002074",
            "rho": "-143.6114",
            "vega": "78.9998",
        },
        "oi_contracts": "261",
        "volume": 0.031,
        "timestamp": 1787878544804095,
        "product_trading_status": "operational",
    }

    calls: list[tuple[str, float, object]] = []

    def fake_get(
        url: str,
        *,
        timeout: float,
        stop: object = None,
    ) -> capture.RawPublicResponse:
        calls.append((url, timeout, stop))
        return _raw_response(
            url,
            {
                "success": True,
                "result": [ticker],
            },
        )

    monkeypatch.setattr(
        capture,
        "raw_delta_public_get",
        fake_get,
    )

    captured = capture.fetch_captured_option_chain(
        "BTC",
        timeout=6.0,
    )

    assert captured.snapshot.underlying == "BTC"
    assert len(captured.snapshot.tickers) == 1
    assert captured.snapshot.tickers[0].symbol == ticker["symbol"]

    assert len(calls) == 1

    from urllib.parse import parse_qs, urlsplit

    url, timeout, stop = calls[0]
    parsed = urlsplit(url)

    assert parsed.path == "/v2/tickers"

    query = parse_qs(parsed.query)
    assert query == {
        "contract_types": ["call_options,put_options"],
        "underlying_asset_symbols": ["BTC"],
    }

    assert timeout == 6.0
    assert stop is None



def test_raw_response_flags_backward_wall_clock_without_rewrite() -> None:
    response = capture.RawPublicResponse(
        url=(
            "https://api.india.delta.exchange/"
            "v2/history/candles"
        ),
        request_started_utc_ns=2_000,
        response_received_utc_ns=1_500,
        request_elapsed_ns=25,
        body=b"{}",
        sha256=hashlib.sha256(b"{}").hexdigest(),
    )

    assert response.clock_anomaly
    assert response.request_started_utc_ns == 2_000
    assert response.response_received_utc_ns == 1_500
    assert response.request_elapsed_ns == 25
