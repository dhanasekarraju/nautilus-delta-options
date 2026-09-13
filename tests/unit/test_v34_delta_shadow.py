from datetime import date
from decimal import Decimal

import pytest

import nautilus_delta_options.signals.v34 as v34
from nautilus_delta_options.delta.history import (
    DeltaCandle,
    DeltaCandleSnapshot,
    parse_history_candles_payload,
)
from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.public_client import DeltaOptionChainSnapshot
from nautilus_delta_options.signals.v34 import (
    V34ChainState,
    V34Config,
    V34ContractState,
    V34Decision,
    V34FlowState,
    V34UnderlyingState,
    build_v34_chain_state,
    evaluate_v34_chain_flow,
    evaluate_v34_shadow_signal,
)


def _ticker(
    *,
    symbol: str,
    contract_type: str,
    expiry: date,
    delta: str,
    mark: str = "100",
    bid: str = "99",
    ask: str = "100",
    bid_iv: str = "0.40",
    ask_iv: str = "0.42",
    oi: str = "100",
    volume: str = "100",
    bid_size: str = "100",
    ask_size: str = "100",
) -> DeltaOptionTicker:
    return DeltaOptionTicker(
        product_id=abs(hash(symbol)) % 1_000_000 + 1,
        symbol=symbol,
        underlying="BTC",
        contract_type=contract_type,  # type: ignore[arg-type]
        strike_price=Decimal("78000"),
        expiry=expiry,
        mark_price=Decimal(mark),
        spot_price=Decimal("78000"),
        contract_value=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        bid_size=Decimal(bid_size),
        ask_size=Decimal(ask_size),
        mark_iv=None,
        bid_iv=Decimal(bid_iv),
        ask_iv=Decimal(ask_iv),
        delta=Decimal(delta),
        gamma=Decimal("0.0002"),
        theta=Decimal("-100"),
        rho=Decimal("1"),
        vega=Decimal("10"),
        open_interest_contracts=Decimal(oi),
        volume=Decimal(volume),
        exchange_timestamp=1_800_000_000_000_000,
        trading_status="operational",
    )


def _chain(*tickers: DeltaOptionTicker) -> DeltaOptionChainSnapshot:
    return DeltaOptionChainSnapshot(
        underlying="BTC",
        tickers=tickers,
        rejected_records=(),
    )


def test_history_parser_drops_incomplete_candle() -> None:
    payload = {
        "success": True,
        "result": [
            {
                "time": 900,
                "open": 10,
                "high": 12,
                "low": 9,
                "close": 11,
                "volume": 5,
            },
            {
                "time": 1200,
                "open": 11,
                "high": 13,
                "low": 10,
                "close": 12,
                "volume": 6,
            },
        ],
    }

    candles = parse_history_candles_payload(
        payload,
        now_s=1400,
    )

    assert len(candles) == 1
    assert candles[0].time_s == 900


def test_v34_blocks_zero_dte() -> None:
    as_of = date(2026, 9, 10)
    chain = _chain(
        _ticker(
            symbol="C-BTC-78000-100926",
            contract_type="call_options",
            expiry=as_of,
            delta="0.50",
        ),
    )

    with pytest.raises(ValueError, match="No V3.4 core option contracts"):
        build_v34_chain_state(
            chain,
            as_of=as_of,
            captured_ns=1,
        )


def test_chain_state_uses_bid_ask_iv_midpoint_when_mark_iv_missing() -> None:
    as_of = date(2026, 9, 10)
    chain = _chain(
        _ticker(
            symbol="C-BTC-78000-110926",
            contract_type="call_options",
            expiry=date(2026, 9, 11),
            delta="0.50",
            bid_iv="0.40",
            ask_iv="0.44",
        ),
    )

    state = build_v34_chain_state(
        chain,
        as_of=as_of,
        captured_ns=1,
    )

    assert state.contracts[0].iv == pytest.approx(0.42)


def test_chain_flow_requires_multiple_bullish_call_inputs() -> None:
    previous = V34ChainState(
        underlying="BTC",
        captured_ns=1,
        contracts=(
            V34ContractState(
                symbol="C",
                contract_type="call_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=100,
                bid_size=100,
                ask_size=100,
            ),
            V34ContractState(
                symbol="P",
                contract_type="put_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=100,
                bid_size=100,
                ask_size=100,
            ),
        ),
    )
    current = V34ChainState(
        underlying="BTC",
        captured_ns=301_000_000_001,
        contracts=(
            V34ContractState(
                symbol="C",
                contract_type="call_options",
                iv=0.42,
                mark_price=102,
                open_interest=120,
                volume=150,
                bid_size=140,
                ask_size=80,
            ),
            V34ContractState(
                symbol="P",
                contract_type="put_options",
                iv=0.399,
                mark_price=99,
                open_interest=101,
                volume=110,
                bid_size=80,
                ask_size=120,
            ),
        ),
    )

    flow = evaluate_v34_chain_flow(previous, current)

    assert flow.call_confirmations == 3
    assert flow.call_score > flow.put_score
    assert flow.iv_edge > 0
    assert flow.oi_edge > 0
    assert flow.volume_edge > 0
    assert flow.depth_edge > 0
    assert flow.premium_edge > 0


def test_shadow_waits_until_chain_flow_is_warmed_up(monkeypatch) -> None:
    monkeypatch.setattr(
        v34,
        "evaluate_v34_underlying",
        lambda _: V34UnderlyingState(
            close=78000,
            rsi=60,
            ema20=77900,
            ema50=77500,
            adx=25,
            atr_pct=0.003,
            ema20_slope_atr=0.2,
            return_5m=0.001,
            return_15m=0.003,
            return_30m=0.005,
            extension_atr=0.5,
            call_score=65,
            put_score=10,
        ),
    )

    candles = DeltaCandleSnapshot(
        underlying="BTC",
        symbol="BTCUSD",
        resolution="5m",
        candles=(
            DeltaCandle(
                time_s=1_800_000_000,
                open=Decimal("77900"),
                high=Decimal("78100"),
                low=Decimal("77800"),
                close=Decimal("78000"),
                volume=Decimal("100"),
            ),
        ),
        captured_ns=1,
    )

    chain = _chain(
        _ticker(
            symbol="C-BTC-78000-110926",
            contract_type="call_options",
            expiry=date(2026, 9, 11),
            delta="0.50",
        ),
        _ticker(
            symbol="P-BTC-78000-110926",
            contract_type="put_options",
            expiry=date(2026, 9, 11),
            delta="-0.50",
        ),
    )

    signal = evaluate_v34_shadow_signal(
        candles,
        chain,
        as_of=date(2026, 9, 10),
        captured_ns=2,
        previous_chain=None,
    )

    assert signal.decision is V34Decision.WAIT
    assert "warming_up_chain_flow" in signal.reasons


def test_shadow_can_authorize_call_only_after_flow_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(
        v34,
        "evaluate_v34_underlying",
        lambda _: V34UnderlyingState(
            close=78000,
            rsi=60,
            ema20=77900,
            ema50=77500,
            adx=25,
            atr_pct=0.003,
            ema20_slope_atr=0.2,
            return_5m=0.001,
            return_15m=0.003,
            return_30m=0.005,
            extension_atr=0.5,
            call_score=65,
            put_score=10,
        ),
    )
    monkeypatch.setattr(
        v34,
        "evaluate_v34_chain_flow",
        lambda *_: V34FlowState(
            call_score=20,
            put_score=0,
            call_confirmations=4,
            put_confirmations=0,
            iv_edge=0.01,
            oi_edge=0.10,
            volume_edge=0.40,
            depth_edge=0.20,
            premium_edge=0.02,
        ),
    )

    candles = DeltaCandleSnapshot(
        underlying="BTC",
        symbol="BTCUSD",
        resolution="5m",
        candles=(
            DeltaCandle(
                time_s=1_800_000_000,
                open=Decimal("77900"),
                high=Decimal("78100"),
                low=Decimal("77800"),
                close=Decimal("78000"),
                volume=Decimal("100"),
            ),
        ),
        captured_ns=1,
    )
    chain = _chain(
        _ticker(
            symbol="C-BTC-78000-110926",
            contract_type="call_options",
            expiry=date(2026, 9, 11),
            delta="0.50",
        ),
        _ticker(
            symbol="P-BTC-78000-110926",
            contract_type="put_options",
            expiry=date(2026, 9, 11),
            delta="-0.50",
        ),
        _ticker(
            symbol="C-BTC-78200-110926",
            contract_type="call_options",
            expiry=date(2026, 9, 11),
            delta="0.45",
        ),
        _ticker(
            symbol="P-BTC-77800-110926",
            contract_type="put_options",
            expiry=date(2026, 9, 11),
            delta="-0.45",
        ),
    )
    previous = build_v34_chain_state(
        chain,
        as_of=date(2026, 9, 10),
        captured_ns=1,
    )

    signal = evaluate_v34_shadow_signal(
        candles,
        chain,
        as_of=date(2026, 9, 10),
        captured_ns=301_000_000_001,
        previous_chain=previous,
        config=V34Config(),
    )

    assert signal.decision is V34Decision.CALL
    assert signal.call_score > signal.put_score


def test_ticker_parser_prefers_top_level_mark_vol() -> None:
    raw = {
        "product_id": 123,
        "symbol": "C-BTC-78000-110926",
        "underlying_asset_symbol": "BTC",
        "contract_type": "call_options",
        "strike_price": "78000",
        "mark_price": "100",
        "spot_price": "78000",
        "contract_value": "0.001",
        "tick_size": "0.1",
        "quotes": {
            "best_ask": "100",
            "best_bid": "99",
            "mark_iv": "0.99",
            "bid_iv": "0.40",
            "ask_iv": "0.44",
            "ask_size": "100",
            "bid_size": "100",
        },
        "greeks": {
            "delta": "0.50",
            "theta": "-100",
            "gamma": "0.0002",
            "rho": "1",
            "vega": "10",
        },
        "mark_vol": "0.42",
        "oi_contracts": "100",
        "volume": "100",
        "timestamp": 1800000000000000,
        "product_trading_status": "operational",
    }

    ticker = DeltaOptionTicker.from_api(raw)

    assert ticker.mark_iv == Decimal("0.42")


def test_shadow_blocks_low_adx_even_with_strong_put_scores(monkeypatch) -> None:
    monkeypatch.setattr(
        v34,
        "evaluate_v34_underlying",
        lambda _: V34UnderlyingState(
            close=78000,
            rsi=40,
            ema20=78100,
            ema50=78200,
            adx=14.8,
            atr_pct=0.002,
            ema20_slope_atr=-0.4,
            return_5m=-0.001,
            return_15m=-0.002,
            return_30m=-0.004,
            extension_atr=0.5,
            call_score=0,
            put_score=70,
        ),
    )
    monkeypatch.setattr(
        v34,
        "evaluate_v34_chain_flow",
        lambda *_: V34FlowState(
            call_score=0,
            put_score=25,
            call_confirmations=0,
            put_confirmations=5,
            iv_edge=-0.01,
            oi_edge=-0.10,
            volume_edge=-0.40,
            depth_edge=-0.20,
            premium_edge=-0.02,
        ),
    )

    candles = DeltaCandleSnapshot(
        underlying="BTC",
        symbol="BTCUSD",
        resolution="5m",
        candles=(
            DeltaCandle(
                time_s=1_800_000_000,
                open=Decimal("78100"),
                high=Decimal("78200"),
                low=Decimal("77900"),
                close=Decimal("78000"),
                volume=Decimal("100"),
            ),
        ),
        captured_ns=1,
    )
    chain = _chain(
        _ticker(
            symbol="C-BTC-78000-110926",
            contract_type="call_options",
            expiry=date(2026, 9, 11),
            delta="0.50",
        ),
        _ticker(
            symbol="C-BTC-78200-110926",
            contract_type="call_options",
            expiry=date(2026, 9, 11),
            delta="0.45",
        ),
        _ticker(
            symbol="P-BTC-78000-110926",
            contract_type="put_options",
            expiry=date(2026, 9, 11),
            delta="-0.50",
        ),
        _ticker(
            symbol="P-BTC-78200-110926",
            contract_type="put_options",
            expiry=date(2026, 9, 11),
            delta="-0.45",
        ),
    )
    previous = build_v34_chain_state(
        chain,
        as_of=date(2026, 9, 10),
        captured_ns=1,
    )

    signal = evaluate_v34_shadow_signal(
        candles,
        chain,
        as_of=date(2026, 9, 10),
        captured_ns=301_000_000_001,
        previous_chain=previous,
    )

    assert signal.decision is V34Decision.WAIT
    assert "weak_trend_regime" in signal.reasons



def test_flow_does_not_treat_oi_and_volume_alone_as_directional_confirmation() -> None:
    previous = V34ChainState(
        underlying="BTC",
        captured_ns=1,
        contracts=(
            V34ContractState(
                symbol="C1",
                contract_type="call_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=100,
                bid_size=100,
                ask_size=100,
            ),
            V34ContractState(
                symbol="P1",
                contract_type="put_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=100,
                bid_size=100,
                ask_size=100,
            ),
        ),
    )
    current = V34ChainState(
        underlying="BTC",
        captured_ns=301_000_000_001,
        contracts=(
            V34ContractState(
                symbol="C1",
                contract_type="call_options",
                iv=0.40,
                mark_price=100,
                open_interest=150,
                volume=180,
                bid_size=100,
                ask_size=100,
            ),
            V34ContractState(
                symbol="P1",
                contract_type="put_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=110,
                bid_size=100,
                ask_size=100,
            ),
        ),
    )

    flow = evaluate_v34_chain_flow(previous, current)

    assert flow.oi_edge > 0
    assert flow.volume_edge > 0
    assert flow.premium_edge == pytest.approx(0)
    assert flow.iv_edge == pytest.approx(0)
    assert flow.depth_edge == pytest.approx(0)
    assert flow.call_score == pytest.approx(0)
    assert flow.call_confirmations == 0


def test_flow_volume_drop_is_not_converted_into_false_surge() -> None:
    previous = V34ChainState(
        underlying="BTC",
        captured_ns=1,
        contracts=(
            V34ContractState(
                symbol="C1",
                contract_type="call_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=1000,
                bid_size=100,
                ask_size=100,
            ),
            V34ContractState(
                symbol="P1",
                contract_type="put_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=100,
                bid_size=100,
                ask_size=100,
            ),
        ),
    )
    current = V34ChainState(
        underlying="BTC",
        captured_ns=301_000_000_001,
        contracts=(
            V34ContractState(
                symbol="C1",
                contract_type="call_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=10,
                bid_size=100,
                ask_size=100,
            ),
            V34ContractState(
                symbol="P1",
                contract_type="put_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=120,
                bid_size=100,
                ask_size=100,
            ),
        ),
    )

    flow = evaluate_v34_chain_flow(previous, current)

    assert flow.volume_edge < 0


def test_flow_depth_uses_change_not_static_imbalance() -> None:
    previous = V34ChainState(
        underlying="BTC",
        captured_ns=1,
        contracts=(
            V34ContractState(
                symbol="C1",
                contract_type="call_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=100,
                bid_size=200,
                ask_size=100,
            ),
            V34ContractState(
                symbol="P1",
                contract_type="put_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=100,
                bid_size=100,
                ask_size=100,
            ),
        ),
    )
    current = V34ChainState(
        underlying="BTC",
        captured_ns=301_000_000_001,
        contracts=(
            V34ContractState(
                symbol="C1",
                contract_type="call_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=100,
                bid_size=200,
                ask_size=100,
            ),
            V34ContractState(
                symbol="P1",
                contract_type="put_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=100,
                bid_size=100,
                ask_size=100,
            ),
        ),
    )

    flow = evaluate_v34_chain_flow(previous, current)

    assert flow.depth_edge == pytest.approx(0)
    assert flow.call_confirmations == 0


def test_supporting_oi_and_volume_do_not_count_as_primary_confirmations() -> None:
    previous = V34ChainState(
        underlying="BTC",
        captured_ns=1,
        contracts=(
            V34ContractState(
                symbol="C1",
                contract_type="call_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=100,
                bid_size=100,
                ask_size=100,
            ),
            V34ContractState(
                symbol="P1",
                contract_type="put_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=100,
                bid_size=100,
                ask_size=100,
            ),
        ),
    )
    current = V34ChainState(
        underlying="BTC",
        captured_ns=301_000_000_001,
        contracts=(
            V34ContractState(
                symbol="C1",
                contract_type="call_options",
                iv=0.40,
                mark_price=106,
                open_interest=140,
                volume=180,
                bid_size=100,
                ask_size=100,
            ),
            V34ContractState(
                symbol="P1",
                contract_type="put_options",
                iv=0.40,
                mark_price=100,
                open_interest=100,
                volume=110,
                bid_size=100,
                ask_size=100,
            ),
        ),
    )

    flow = evaluate_v34_chain_flow(previous, current)

    # Premium is the only primary directional confirmation here.
    # OI and volume can add score, but may not promote this to two
    # independent confirmations.
    assert flow.premium_edge > 0
    assert flow.oi_edge > 0
    assert flow.volume_edge > 0
    assert flow.call_score > 10
    assert flow.call_confirmations == 1
