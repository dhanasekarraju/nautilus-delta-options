from dataclasses import replace
from decimal import Decimal

from test_audit_remediation import NOW, session_at
from test_paper_ledger import AS_OF, _product, _ticker

from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaOptionProductsSnapshot,
)
from nautilus_delta_options.paper.v34_paper_live import run_v34_paper_entry_cycle
from nautilus_delta_options.paper.v34_shadow import V34ShadowCycle, V34ShadowQualitySelection
from nautilus_delta_options.selection.v34_quality import rank_v34_contract_quality
from nautilus_delta_options.signals.v34 import (
    V34ChainState,
    V34Decision,
    V34FlowState,
    V34ShadowSignal,
    V34UnderlyingState,
)


def signal(underlying="BTC", score=80):
    return V34ShadowSignal(
        underlying=underlying,
        candle_close_ms=NOW // 1_000_000 - 1,
        decision=V34Decision.CALL,
        call_score=score,
        put_score=10,
        confidence=score,
        underlying_state=V34UnderlyingState(
            close=80000,
            rsi=60,
            ema20=80000,
            ema50=79000,
            adx=30,
            atr_pct=0.01,
            ema20_slope_atr=0.2,
            return_5m=0.001,
            return_15m=0.002,
            return_30m=0.003,
            extension_atr=0.1,
            call_score=75,
            put_score=10,
        ),
        flow_state=V34FlowState(80, 10, 3, 0, 0.1, 0.1, 0.1, 0.1, 0.1),
        chain_state=V34ChainState(underlying, NOW, ()),
        reasons=(),
    )


def cycle(signals):
    quality = rank_v34_contract_quality(
        DeltaOptionChainSnapshot("BTC", (_ticker(),), ()),
        contract_type="call_options",
        as_of=AS_OF,
    )[0]
    return V34ShadowCycle(
        candle_close_ms=signals[0].candle_close_ms,
        evaluated=True,
        signals=tuple(signals),
        warnings=(),
        quality=(V34ShadowQualitySelection("BTC", 1, 0, quality, None),),
    )


class Market:
    def __init__(self, final=None):
        self.calls = 0
        self.final = final

    def fetch_option_products(self, underlying):
        assert underlying == "BTC"
        return DeltaOptionProductsSnapshot("BTC", (_product(),), ())

    def fetch_option_tickers(self, symbols):
        assert symbols == (_ticker().symbol,)
        self.calls += 1
        return (self.final if self.calls >= 2 and self.final is not None else _ticker(),)


def test_real_v34_entry_pipeline_persists_and_deduplicates_on_restart(tmp_path):
    path = tmp_path / "paper.sqlite"
    market = Market()
    observed_cycle = cycle([signal()])
    session = session_at(path)
    result = run_v34_paper_entry_cycle(
        cycle=observed_cycle,
        delta_client=market,
        session=session,
        clock_ns=lambda: NOW,
        as_of=AS_OF,
    )
    assert result.results[0].status.value == "opened"
    assert market.calls == 2
    before = session.snapshot()
    restored = session_at(path)
    replay = run_v34_paper_entry_cycle(
        cycle=observed_cycle,
        delta_client=market,
        session=restored,
        clock_ns=lambda: NOW,
        as_of=AS_OF,
    )
    assert replay.results[0].status.value == "already_consumed"
    assert restored.snapshot().cash == before.cash
    assert restored.snapshot().open_positions == before.open_positions
    assert market.calls == 2


def test_final_exact_quote_deterioration_does_not_debit_cash(tmp_path):
    session = session_at(tmp_path / "paper.sqlite")
    market = Market(final=replace(_ticker(), best_ask=Decimal("1100")))
    result = run_v34_paper_entry_cycle(
        cycle=cycle([signal()]),
        delta_client=market,
        session=session,
        clock_ns=lambda: NOW,
        as_of=AS_OF,
    )
    assert result.results[0].status.value == "quality_revalidation_rejected"
    assert market.calls == 2
    assert session.snapshot().cash == Decimal("100")
    assert session.snapshot().open_positions == ()


def test_signal_expired_before_refresh_never_fetches_or_opens(tmp_path):
    market = Market()
    session = session_at(tmp_path / "paper.sqlite")
    result = run_v34_paper_entry_cycle(
        cycle=cycle([signal()]),
        delta_client=market,
        session=session,
        clock_ns=lambda: NOW + 16_000_000_000,
        as_of=AS_OF,
    )
    assert result.results[0].status.value == "stale_signal"
    assert market.calls == 0
    assert session.snapshot().open_positions == ()


def test_btc_eth_same_direction_compete_for_one_persisted_entry(tmp_path):
    session = session_at(tmp_path / "paper.sqlite")
    market = Market()
    shared = cycle([signal("ETH", 75), signal("BTC", 80)])
    btc_quality = shared.quality[0].best_call
    eth_quality = replace(btc_quality, symbol="C-ETH-2500-300826", score=100)
    shared = replace(
        shared,
        quality=(*shared.quality, V34ShadowQualitySelection("ETH", 1, 0, eth_quality, None)),
    )
    result = run_v34_paper_entry_cycle(
        cycle=shared,
        delta_client=market,
        session=session,
        clock_ns=lambda: NOW,
        as_of=AS_OF,
    )
    assert result.results[0].status.value == "correlated_signal_skipped"
    assert result.results[1].status.value == "opened"
    assert len(session.snapshot().open_positions) == 1
    assert session.snapshot().open_positions[0].underlying == "BTC"
    assert market.calls == 2
