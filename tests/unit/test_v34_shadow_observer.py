from __future__ import annotations

from datetime import date
from decimal import Decimal

from nautilus_delta_options.delta.history import DeltaCandle, DeltaCandleSnapshot
from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.public_client import DeltaOptionChainSnapshot
from nautilus_delta_options.paper.v34_shadow import (
    V34ShadowObserver,
    v34_shadow_cycle_payload,
)


def _candles(underlying: str, last_time_s: int) -> DeltaCandleSnapshot:
    rows = tuple(
        DeltaCandle(
            time_s=last_time_s - (199 - i) * 300,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for i in range(200)
    )
    return DeltaCandleSnapshot(
        underlying=underlying,  # type: ignore[arg-type]
        symbol=f"{underlying}USD",
        resolution="5m",
        candles=rows,
        captured_ns=1,
    )


def _ticker(underlying: str, contract_type: str, symbol: str, delta: str) -> DeltaOptionTicker:
    return DeltaOptionTicker(
        product_id=abs(hash(symbol)) % 1_000_000 + 1,
        symbol=symbol,
        underlying=underlying,
        contract_type=contract_type,  # type: ignore[arg-type]
        strike_price=Decimal("100"),
        expiry=date(2026, 9, 11),
        mark_price=Decimal("10"),
        spot_price=Decimal("100"),
        contract_value=Decimal("1"),
        tick_size=Decimal("0.1"),
        best_bid=Decimal("9.9"),
        best_ask=Decimal("10"),
        bid_size=Decimal("100"),
        ask_size=Decimal("100"),
        mark_iv=Decimal("0.4"),
        bid_iv=Decimal("0.39"),
        ask_iv=Decimal("0.41"),
        delta=Decimal(delta),
        gamma=Decimal("0.01"),
        theta=Decimal("-1"),
        rho=Decimal("1"),
        vega=Decimal("1"),
        open_interest_contracts=Decimal("100"),
        volume=Decimal("100"),
        exchange_timestamp=1_800_000_000_000_000,
        trading_status="operational",
    )


def _chain(underlying: str) -> DeltaOptionChainSnapshot:
    return DeltaOptionChainSnapshot(
        underlying=underlying,  # type: ignore[arg-type]
        tickers=(
            _ticker(underlying, "call_options", f"C-{underlying}-100-110926", "0.50"),
            _ticker(underlying, "call_options", f"C-{underlying}-105-110926", "0.40"),
            _ticker(underlying, "put_options", f"P-{underlying}-100-110926", "-0.50"),
            _ticker(underlying, "put_options", f"P-{underlying}-95-110926", "-0.40"),
        ),
        rejected_records=(),
    )


class _History:
    def __init__(self, btc: DeltaCandleSnapshot, eth: DeltaCandleSnapshot) -> None:
        self.rows = {"BTC": btc, "ETH": eth}
        self.calls = 0

    def fetch_5m_candles(self, underlying: str, *, count: int = 240, now_s: int | None = None):
        self.calls += 1
        return self.rows[underlying]


class _Delta:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_option_chain(self, underlying: str):
        self.calls += 1
        return _chain(underlying)


def test_observer_has_no_entry_authority() -> None:
    observer = V34ShadowObserver(
        history_client=_History(_candles("BTC", 1_800_000_000), _candles("ETH", 1_800_000_000)),
        delta_client=_Delta(),
    )
    assert observer.entry_authority is False


def test_misaligned_boundaries_do_not_fetch_option_chain() -> None:
    history = _History(
        _candles("BTC", 1_800_000_000),
        _candles("ETH", 1_800_000_300),
    )
    delta = _Delta()
    observer = V34ShadowObserver(history_client=history, delta_client=delta)

    cycle = observer.run_cycle()

    assert cycle.evaluated is False
    assert cycle.candle_close_ms is None
    assert "boundary not aligned" in cycle.warnings[0]
    assert delta.calls == 0


def test_duplicate_boundary_does_not_refetch_chain(monkeypatch) -> None:
    history = _History(
        _candles("BTC", 1_800_000_000),
        _candles("ETH", 1_800_000_000),
    )
    delta = _Delta()

    # Avoid indicator-shape concerns here; exercise observer ownership only.
    from nautilus_delta_options.paper import v34_shadow as module
    from nautilus_delta_options.signals.v34 import (
        V34ChainState,
        V34Decision,
        V34ShadowSignal,
        V34UnderlyingState,
    )

    def fake_evaluate(candles, chain, *, as_of, captured_ns, previous_chain, config):
        state = V34ChainState(
            underlying=candles.underlying,
            captured_ns=captured_ns,
            contracts=(),
        )
        underlying = V34UnderlyingState(
            close=100,
            rsi=50,
            ema20=100,
            ema50=100,
            adx=10,
            atr_pct=0.01,
            ema20_slope_atr=0,
            return_5m=0,
            return_15m=0,
            return_30m=0,
            extension_atr=0,
            call_score=0,
            put_score=0,
        )
        return V34ShadowSignal(
            underlying=candles.underlying,
            candle_close_ms=candles.candle_close_ms,
            decision=V34Decision.WAIT,
            call_score=0,
            put_score=0,
            confidence=0,
            underlying_state=underlying,
            flow_state=None,
            chain_state=state,
            reasons=("test",),
        )

    monkeypatch.setattr(module, "evaluate_v34_shadow_signal", fake_evaluate)

    observer = V34ShadowObserver(
        history_client=history,
        delta_client=delta,
        clock_ns=lambda: 1_800_000_000_000_000_001,
        utc_date=lambda: date(2026, 9, 10),
    )

    first = observer.run_cycle()
    second = observer.run_cycle()

    assert first.evaluated is True
    assert second.evaluated is False
    assert delta.calls == 2


def test_payload_explicitly_reports_no_entry_authority() -> None:
    from nautilus_delta_options.paper.v34_shadow import V34ShadowCycle

    payload = v34_shadow_cycle_payload(
        V34ShadowCycle(
            candle_close_ms=123,
            evaluated=False,
            signals=(),
            warnings=(),
        )
    )

    assert payload["entry_authority"] is False


def test_observer_attaches_best_quality_call_and_put(monkeypatch) -> None:
    history = _History(
        _candles("BTC", 1_800_000_000),
        _candles("ETH", 1_800_000_000),
    )
    delta = _Delta()

    from nautilus_delta_options.paper import v34_shadow as module
    from nautilus_delta_options.signals.v34 import (
        V34ChainState,
        V34Decision,
        V34ShadowSignal,
        V34UnderlyingState,
    )

    def fake_evaluate(candles, chain, *, as_of, captured_ns, previous_chain, config):
        return V34ShadowSignal(
            underlying=candles.underlying,
            candle_close_ms=candles.candle_close_ms,
            decision=V34Decision.WAIT,
            call_score=0,
            put_score=0,
            confidence=0,
            underlying_state=V34UnderlyingState(
                close=100,
                rsi=50,
                ema20=100,
                ema50=100,
                adx=10,
                atr_pct=0.01,
                ema20_slope_atr=0,
                return_5m=0,
                return_15m=0,
                return_30m=0,
                extension_atr=0,
                call_score=0,
                put_score=0,
            ),
            flow_state=None,
            chain_state=V34ChainState(
                underlying=candles.underlying,
                captured_ns=captured_ns,
                contracts=(),
            ),
            reasons=("test",),
        )

    monkeypatch.setattr(module, "evaluate_v34_shadow_signal", fake_evaluate)

    observer = V34ShadowObserver(
        history_client=history,
        delta_client=delta,
        clock_ns=lambda: 1_800_000_000_000_000_001,
        utc_date=lambda: date(2026, 9, 10),
    )

    cycle = observer.run_cycle()
    payload = v34_shadow_cycle_payload(cycle)

    assert cycle.evaluated is True
    assert len(cycle.quality) == 2
    assert payload["entry_authority"] is False

    for signal in payload["signals"]:
        quality = signal["contract_quality"]
        assert quality["call_candidate_count"] == 2
        assert quality["put_candidate_count"] == 2
        assert quality["best_call"]["abs_delta"] == 0.5
        assert quality["best_put"]["abs_delta"] == 0.5
        assert quality["best_call"]["components"]
        assert quality["best_put"]["components"]


def test_quality_payload_uses_score_edge_name_without_removing_compatibility(monkeypatch) -> None:
    history = _History(
        _candles("BTC", 1_800_000_000),
        _candles("ETH", 1_800_000_000),
    )
    delta = _Delta()

    from nautilus_delta_options.paper import v34_shadow as module
    from nautilus_delta_options.signals.v34 import (
        V34ChainState,
        V34Decision,
        V34ShadowSignal,
        V34UnderlyingState,
    )

    def fake_evaluate(candles, chain, *, as_of, captured_ns, previous_chain, config):
        return V34ShadowSignal(
            underlying=candles.underlying,
            candle_close_ms=candles.candle_close_ms,
            decision=V34Decision.WAIT,
            call_score=70,
            put_score=10,
            confidence=60,
            underlying_state=V34UnderlyingState(
                close=100, rsi=50, ema20=100, ema50=100, adx=10,
                atr_pct=0.01, ema20_slope_atr=0, return_5m=0,
                return_15m=0, return_30m=0, extension_atr=0,
                call_score=70, put_score=10,
            ),
            flow_state=None,
            chain_state=V34ChainState(
                underlying=candles.underlying,
                captured_ns=captured_ns,
                contracts=(),
            ),
            reasons=("test",),
        )

    monkeypatch.setattr(module, "evaluate_v34_shadow_signal", fake_evaluate)
    cycle = V34ShadowObserver(
        history_client=history,
        delta_client=delta,
        clock_ns=lambda: 1_800_000_000_000_000_001,
        utc_date=lambda: date(2026, 9, 10),
    ).run_cycle()

    signal = v34_shadow_cycle_payload(cycle)["signals"][0]
    assert signal["score_edge"] == 60
    assert signal["confidence"] == 60
