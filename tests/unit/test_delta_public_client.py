from nautilus_delta_options.delta.public_client import parse_option_chain_payload


def _valid_ticker() -> dict[str, object]:
    return {
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


def test_parses_valid_option_chain() -> None:
    snapshot = parse_option_chain_payload(
        {
            "success": True,
            "result": [_valid_ticker()],
        },
        underlying="BTC",
    )

    assert len(snapshot.tickers) == 1
    assert len(snapshot.tradeable_tickers) == 1
    assert snapshot.rejected_records == ()


def test_retains_valid_tickers_and_reports_invalid_records() -> None:
    snapshot = parse_option_chain_payload(
        {
            "success": True,
            "result": [_valid_ticker(), "invalid record"],
        },
        underlying="BTC",
    )

    assert len(snapshot.tickers) == 1
    assert snapshot.rejected_records == ("record 1: must be an object",)


def test_rejects_mismatched_underlying() -> None:
    ticker = _valid_ticker()
    ticker["underlying_asset_symbol"] = "ETH"

    snapshot = parse_option_chain_payload(
        {
            "success": True,
            "result": [ticker],
        },
        underlying="BTC",
    )

    assert snapshot.tickers == ()
    assert len(snapshot.rejected_records) == 1
    assert "expected underlying BTC, got ETH" in snapshot.rejected_records[0]
