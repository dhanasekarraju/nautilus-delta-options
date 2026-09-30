from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from nautilus_delta_options.delta.history import (
    DeltaCandle,
    DeltaCandleSnapshot,
)
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
)
from nautilus_delta_options.paper import (
    quote_safety,
)
from nautilus_delta_options.paper import (
    v34_shadow as frozen_module,
)
from nautilus_delta_options.paper.v34_shadow import (
    V34ShadowObserver,
)
from nautilus_delta_options.research.experiment1 import (
    state_machine as research_module,
)
from nautilus_delta_options.research.experiment1.state_machine import (
    EvaluationReturn,
    Experiment1ObserverStateMachine,
)
from nautilus_delta_options.signals.v34 import (
    V34ChainState,
    V34Decision,
    V34ShadowSignal,
    V34UnderlyingState,
)

_C1 = 1_800_000_000
_C2 = _C1 + 300
_C3 = _C2 + 300


def _candles(
    underlying: str,
    close_s: int,
) -> DeltaCandleSnapshot:
    rows = tuple(
        DeltaCandle(
            time_s=close_s - (199 - index) * 300,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for index in range(200)
    )

    return DeltaCandleSnapshot(
        underlying=underlying,  # type: ignore[arg-type]
        symbol=f"{underlying}USD",
        resolution="5m",
        candles=rows,
        captured_ns=1,
    )


def _chain(
    underlying: str,
) -> DeltaOptionChainSnapshot:
    return DeltaOptionChainSnapshot(
        underlying=underlying,  # type: ignore[arg-type]
        tickers=(),
        rejected_records=(),
    )


@dataclass
class _Control:
    stage: str | None = None


class _History:
    def __init__(
        self,
        control: _Control,
        close_s: int,
        trace: list[str],
    ) -> None:
        self.control = control
        self.close_s = close_s
        self.trace = trace

    def fetch_5m_candles(
        self,
        underlying: str,
        *,
        count: int = 240,
        now_s: int | None = None,
    ) -> DeltaCandleSnapshot:
        del count, now_s

        self.trace.append(f"history:{underlying}")

        if self.control.stage == (
            f"{underlying.lower()}_history"
        ):
            raise RuntimeError(self.control.stage)

        return _candles(
            underlying,
            self.close_s,
        )


class _Delta:
    def __init__(
        self,
        control: _Control,
        trace: list[str],
    ) -> None:
        self.control = control
        self.trace = trace

    def fetch_option_chain(
        self,
        underlying: str,
    ) -> DeltaOptionChainSnapshot:
        self.trace.append(f"chain:{underlying}")

        if self.control.stage == (
            f"{underlying.lower()}_chain"
        ):
            raise RuntimeError(self.control.stage)

        return _chain(underlying)


def _signal(
    underlying: str,
    *,
    captured_ns: int,
    candle_close_ms: int,
    decision: V34Decision = V34Decision.WAIT,
) -> V34ShadowSignal:
    return V34ShadowSignal(
        underlying=underlying,
        candle_close_ms=candle_close_ms,
        decision=decision,
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
            underlying=underlying,
            captured_ns=captured_ns,
            contracts=(),
        ),
        reasons=("test",),
    )


def _install_functions(
    monkeypatch: pytest.MonkeyPatch,
    *,
    control: _Control,
    trace: list[str],
    previous_seen: list[tuple[str, int | None]],
) -> None:
    def fake_fresh_chain(
        chain: DeltaOptionChainSnapshot,
        observed_ns: int,
    ) -> DeltaOptionChainSnapshot:
        del observed_ns

        trace.append(f"fresh:{chain.underlying}")

        if control.stage == (
            f"{chain.underlying.lower()}_fresh_chain"
        ):
            raise RuntimeError(control.stage)

        return chain

    def fake_evaluate(
        candles: DeltaCandleSnapshot,
        chain: DeltaOptionChainSnapshot,
        *,
        as_of: date,
        captured_ns: int,
        previous_chain: V34ChainState | None,
        config: object,
    ) -> V34ShadowSignal:
        del as_of, config

        underlying = candles.underlying

        assert chain.underlying == underlying

        trace.append(f"evaluate:{underlying}")

        previous_seen.append(
            (
                underlying,
                (
                    previous_chain.captured_ns
                    if previous_chain is not None
                    else None
                ),
            )
        )

        if control.stage == (
            f"{underlying.lower()}_evaluation"
        ):
            raise RuntimeError(control.stage)

        return _signal(
            underlying,
            captured_ns=captured_ns,
            candle_close_ms=candles.candle_close_ms,
        )

    def fake_rank(
        chain: DeltaOptionChainSnapshot,
        *,
        contract_type: str,
        as_of: date,
        config: object,
    ) -> tuple[()]:
        del as_of, config

        side = (
            "call"
            if contract_type == "call_options"
            else "put"
        )

        trace.append(
            f"rank:{chain.underlying}:{side}"
        )

        if control.stage == (
            f"{chain.underlying.lower()}_{side}_ranking"
        ):
            raise RuntimeError(control.stage)

        return ()

    monkeypatch.setattr(
        quote_safety,
        "fresh_chain",
        fake_fresh_chain,
    )

    monkeypatch.setattr(
        frozen_module,
        "evaluate_v34_shadow_signal",
        fake_evaluate,
    )
    monkeypatch.setattr(
        research_module,
        "evaluate_v34_shadow_signal",
        fake_evaluate,
    )

    monkeypatch.setattr(
        frozen_module,
        "rank_v34_contract_quality",
        fake_rank,
    )
    monkeypatch.setattr(
        research_module,
        "rank_v34_contract_quality",
        fake_rank,
    )


def _clock() -> int:
    _clock.value += 1
    return _clock.value


_clock.value = 10_000_000_000_000_000  # type: ignore[attr-defined]


def _reset_clock() -> None:
    _clock.value = 10_000_000_000_000_000  # type: ignore[attr-defined]


def _new_frozen(
    control: _Control,
    history: _History,
    delta: _Delta,
) -> V34ShadowObserver:
    return V34ShadowObserver(
        history_client=history,  # type: ignore[arg-type]
        delta_client=delta,  # type: ignore[arg-type]
        clock_ns=_clock,
        utc_date=lambda: date(2026, 9, 10),
    )


def _new_research(
    control: _Control,
    history: _History,
    delta: _Delta,
    *,
    hook=None,
) -> Experiment1ObserverStateMachine:
    del control

    return Experiment1ObserverStateMachine(
        history_client=history,  # type: ignore[arg-type]
        delta_client=delta,  # type: ignore[arg-type]
        clock_ns=_clock,
        utc_date=lambda: date(2026, 9, 10),
        on_evaluation_return=hook,
    )


_FAILURE_CASES = (
    ("btc_history", 0),
    ("eth_history", 0),
    ("btc_chain", 0),
    ("eth_chain", 0),
    ("btc_fresh_chain", 0),
    ("btc_evaluation", 0),
    ("btc_call_ranking", 1),
    ("btc_put_ranking", 1),
    ("eth_fresh_chain", 1),
    ("eth_evaluation", 1),
    ("eth_call_ranking", 2),
    ("eth_put_ranking", 2),
)


_EXPECTED_FAILURE_TRACE: dict[str, list[str]] = {
    "btc_history": [
        "history:BTC",
    ],
    "eth_history": [
        "history:BTC",
        "history:ETH",
    ],
    "btc_chain": [
        "history:BTC",
        "history:ETH",
        "chain:BTC",
    ],
    "eth_chain": [
        "history:BTC",
        "history:ETH",
        "chain:BTC",
        "chain:ETH",
    ],
    "btc_fresh_chain": [
        "history:BTC",
        "history:ETH",
        "chain:BTC",
        "chain:ETH",
        "fresh:BTC",
    ],
    "btc_evaluation": [
        "history:BTC",
        "history:ETH",
        "chain:BTC",
        "chain:ETH",
        "fresh:BTC",
        "evaluate:BTC",
    ],
    "btc_call_ranking": [
        "history:BTC",
        "history:ETH",
        "chain:BTC",
        "chain:ETH",
        "fresh:BTC",
        "evaluate:BTC",
        "rank:BTC:call",
    ],
    "btc_put_ranking": [
        "history:BTC",
        "history:ETH",
        "chain:BTC",
        "chain:ETH",
        "fresh:BTC",
        "evaluate:BTC",
        "rank:BTC:call",
        "rank:BTC:put",
    ],
    "eth_fresh_chain": [
        "history:BTC",
        "history:ETH",
        "chain:BTC",
        "chain:ETH",
        "fresh:BTC",
        "evaluate:BTC",
        "rank:BTC:call",
        "rank:BTC:put",
        "fresh:ETH",
    ],
    "eth_evaluation": [
        "history:BTC",
        "history:ETH",
        "chain:BTC",
        "chain:ETH",
        "fresh:BTC",
        "evaluate:BTC",
        "rank:BTC:call",
        "rank:BTC:put",
        "fresh:ETH",
        "evaluate:ETH",
    ],
    "eth_call_ranking": [
        "history:BTC",
        "history:ETH",
        "chain:BTC",
        "chain:ETH",
        "fresh:BTC",
        "evaluate:BTC",
        "rank:BTC:call",
        "rank:BTC:put",
        "fresh:ETH",
        "evaluate:ETH",
        "rank:ETH:call",
    ],
    "eth_put_ranking": [
        "history:BTC",
        "history:ETH",
        "chain:BTC",
        "chain:ETH",
        "fresh:BTC",
        "evaluate:BTC",
        "rank:BTC:call",
        "rank:BTC:put",
        "fresh:ETH",
        "evaluate:ETH",
        "rank:ETH:call",
        "rank:ETH:put",
    ],
}


@pytest.mark.parametrize(
    ("failure_stage", "expected_returns"),
    _FAILURE_CASES,
)
def test_failure_matrix_matches_frozen_state_advancement(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_returns: int,
) -> None:
    # ---------------- Frozen reference ----------------

    frozen_control = _Control()
    frozen_trace: list[str] = []
    frozen_previous: list[tuple[str, int | None]] = []

    _reset_clock()

    _install_functions(
        monkeypatch,
        control=frozen_control,
        trace=frozen_trace,
        previous_seen=frozen_previous,
    )

    frozen_history = _History(
        frozen_control,
        _C1,
        frozen_trace,
    )
    frozen_delta = _Delta(
        frozen_control,
        frozen_trace,
    )

    frozen = _new_frozen(
        frozen_control,
        frozen_history,
        frozen_delta,
    )

    first_frozen = frozen.run_cycle()

    assert first_frozen.evaluated is True

    frozen_committed_boundary = (
        frozen.last_candle_close_ms
    )

    frozen_history.close_s = _C2
    frozen_control.stage = failure_stage
    frozen_previous.clear()
    frozen_trace.clear()

    failed_frozen = frozen.run_cycle()

    assert failed_frozen.evaluated is False
    assert failed_frozen.signals == ()
    assert (
        frozen.last_candle_close_ms
        == frozen_committed_boundary
    )
    assert (
        frozen_trace
        == _EXPECTED_FAILURE_TRACE[failure_stage]
    )

    frozen_history.close_s = _C3
    frozen_control.stage = None
    frozen_previous.clear()

    recovered_frozen = frozen.run_cycle()

    assert recovered_frozen.evaluated is True

    frozen_recovery_previous = tuple(
        frozen_previous
    )

    # ---------------- Research candidate ----------------

    research_control = _Control()
    research_trace: list[str] = []
    research_previous: list[tuple[str, int | None]] = []

    _reset_clock()

    _install_functions(
        monkeypatch,
        control=research_control,
        trace=research_trace,
        previous_seen=research_previous,
    )

    research_history = _History(
        research_control,
        _C1,
        research_trace,
    )
    research_delta = _Delta(
        research_control,
        research_trace,
    )

    research = _new_research(
        research_control,
        research_history,
        research_delta,
    )

    first_research = research.run_cycle()

    assert first_research.evaluated is True

    research_committed_boundary = (
        research.last_candle_close_ms
    )

    research_history.close_s = _C2
    research_control.stage = failure_stage
    research_previous.clear()
    research_trace.clear()

    failed_research = research.run_cycle()

    assert failed_research.evaluated is False
    assert failed_research.signals == ()
    assert failed_research.failure_stage == failure_stage
    assert (
        len(failed_research.evaluation_returns)
        == expected_returns
    )
    assert tuple(
        result.underlying
        for result in failed_research.evaluation_returns
    ) == ("BTC", "ETH")[:expected_returns]
    assert (
        research_trace
        == _EXPECTED_FAILURE_TRACE[failure_stage]
    )
    assert (
        research.last_candle_close_ms
        == research_committed_boundary
    )

    research_history.close_s = _C3
    research_control.stage = None
    research_previous.clear()

    recovered_research = research.run_cycle()

    assert recovered_research.evaluated is True

    research_recovery_previous = tuple(
        research_previous
    )

    # Most important differential proof:
    # the failed C2 cycle did not become the previous-chain
    # baseline for either implementation.
    assert (
        research_recovery_previous
        == frozen_recovery_previous
    )

    assert (
        research.last_candle_close_ms
        == frozen.last_candle_close_ms
    )


def test_success_order_matches_frozen_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = [
        "history:BTC",
        "history:ETH",
        "chain:BTC",
        "chain:ETH",
        "fresh:BTC",
        "evaluate:BTC",
        "rank:BTC:call",
        "rank:BTC:put",
        "fresh:ETH",
        "evaluate:ETH",
        "rank:ETH:call",
        "rank:ETH:put",
    ]

    control = _Control()
    trace: list[str] = []
    previous: list[tuple[str, int | None]] = []

    _reset_clock()

    _install_functions(
        monkeypatch,
        control=control,
        trace=trace,
        previous_seen=previous,
    )

    history = _History(control, _C1, trace)
    delta = _Delta(control, trace)

    frozen = _new_frozen(
        control,
        history,
        delta,
    )

    assert frozen.run_cycle().evaluated is True
    assert trace == expected

    trace.clear()
    previous.clear()
    _reset_clock()

    history = _History(control, _C1, trace)
    delta = _Delta(control, trace)

    research = _new_research(
        control,
        history,
        delta,
    )

    cycle = research.run_cycle()

    assert cycle.evaluated is True
    assert trace == expected
    assert len(cycle.evaluation_returns) == 2


def test_first_actual_return_remains_canonical_across_conflicting_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WAIT -> aborted ranking -> CALL retry must not replace WAIT."""

    control = _Control()
    trace: list[str] = []
    previous: list[tuple[str, int | None]] = []

    _reset_clock()

    _install_functions(
        monkeypatch,
        control=control,
        trace=trace,
        previous_seen=previous,
    )

    c2_close_ms = _candles(
        "BTC",
        _C2,
    ).candle_close_ms

    btc_c2_return_count = 0

    def varying_evaluate(
        candles: DeltaCandleSnapshot,
        chain: DeltaOptionChainSnapshot,
        *,
        as_of: date,
        captured_ns: int,
        previous_chain: V34ChainState | None,
        config: object,
    ) -> V34ShadowSignal:
        del as_of, config

        nonlocal btc_c2_return_count

        assert chain.underlying == candles.underlying

        previous.append(
            (
                candles.underlying,
                (
                    previous_chain.captured_ns
                    if previous_chain is not None
                    else None
                ),
            )
        )

        decision = V34Decision.WAIT

        if (
            candles.underlying == "BTC"
            and candles.candle_close_ms == c2_close_ms
        ):
            btc_c2_return_count += 1

            if btc_c2_return_count >= 2:
                decision = V34Decision.CALL

        return _signal(
            candles.underlying,
            captured_ns=captured_ns,
            candle_close_ms=candles.candle_close_ms,
            decision=decision,
        )

    monkeypatch.setattr(
        research_module,
        "evaluate_v34_shadow_signal",
        varying_evaluate,
    )

    observed: list[
        tuple[
            str,
            int,
            str,
            V34Decision,
            V34Decision,
            int,
            int,
        ]
    ] = []

    def hook(
        result: EvaluationReturn,
    ) -> bool:
        observed.append(
            (
                result.underlying,
                result.signal.candle_close_ms,
                result.population_status,
                result.signal.decision,
                result.canonical_decision,
                result.retry_ordinal,
                result.canonical_captured_ns,
            )
        )

        # Deliberately reject the FIRST actual BTC C2 result.
        # Canonical ownership must nevertheless remain sealed.
        return not (
            result.underlying == "BTC"
            and result.signal.candle_close_ms == c2_close_ms
            and result.population_status == "canonical"
        )

    history = _History(
        control,
        _C1,
        trace,
    )
    delta = _Delta(
        control,
        trace,
    )

    observer = _new_research(
        control,
        history,
        delta,
        hook=hook,
    )

    # Establish committed previous-chain state at C1.
    first = observer.run_cycle()

    assert first.evaluated is True

    committed_boundary = observer.last_candle_close_ms
    committed_btc = observer.previous_chain("BTC")
    committed_eth = observer.previous_chain("ETH")

    assert committed_btc is not None
    assert committed_eth is not None

    # First C2 attempt:
    # BTC evaluator returns WAIT, evidence hook rejects it,
    # then BTC CALL ranking aborts the observer cycle.
    history.close_s = _C2
    control.stage = "btc_call_ranking"

    failed = observer.run_cycle()

    assert failed.evaluated is False
    assert failed.failure_stage == "btc_call_ranking"
    assert len(failed.evaluation_returns) == 1
    assert len(failed.evidence_faults) == 1

    first_btc_c2 = failed.evaluation_returns[0]

    assert first_btc_c2.underlying == "BTC"
    assert (
        first_btc_c2.signal.decision
        is V34Decision.WAIT
    )
    assert (
        first_btc_c2.population_status
        == "canonical"
    )
    assert first_btc_c2.retry_ordinal == 0
    assert (
        first_btc_c2.canonical_decision
        is V34Decision.WAIT
    )
    assert (
        first_btc_c2.canonical_captured_ns
        == first_btc_c2.captured_ns
    )

    # Frozen observer rolling state still belongs to C1.
    assert observer.last_candle_close_ms == committed_boundary
    assert observer.previous_chain("BTC") == committed_btc
    assert observer.previous_chain("ETH") == committed_eth

    # Retry SAME C2 boundary.
    #
    # The fake evaluator now returns CALL for BTC. The cycle is
    # allowed to succeed, but the population owner must remain
    # the earlier WAIT even though that first evidence hook was
    # rejected.
    control.stage = None

    retry = observer.run_cycle()

    assert retry.evaluated is True
    assert len(retry.evaluation_returns) == 2

    retry_btc = retry.evaluation_returns[0]
    retry_eth = retry.evaluation_returns[1]

    assert retry_btc.underlying == "BTC"
    assert (
        retry_btc.signal.decision
        is V34Decision.CALL
    )
    assert (
        retry_btc.population_status
        == "conflicting_retry"
    )
    assert retry_btc.retry_ordinal == 1
    assert (
        retry_btc.canonical_decision
        is V34Decision.WAIT
    )
    assert (
        retry_btc.canonical_captured_ns
        == first_btc_c2.captured_ns
    )

    # ETH never evaluated during the aborted first attempt, so
    # its first actual C2 evaluator return is still canonical.
    assert retry_eth.underlying == "ETH"
    assert retry_eth.population_status == "canonical"
    assert retry_eth.retry_ordinal == 0

    # The retry may legitimately become committed observer state.
    # Population canonicality and rolling-state commitment are
    # intentionally separate concepts.
    assert observer.last_candle_close_ms == (
        _candles("BTC", _C2).candle_close_ms
    )
    assert observer.previous_chain("BTC") == (
        retry_btc.signal.chain_state
    )
    assert observer.previous_chain("ETH") == (
        retry_eth.signal.chain_state
    )

    btc_c2_observed = [
        row
        for row in observed
        if row[0] == "BTC"
        and row[1] == c2_close_ms
    ]

    assert len(btc_c2_observed) == 2

    assert btc_c2_observed[0][2] == "canonical"
    assert btc_c2_observed[0][3] is V34Decision.WAIT
    assert btc_c2_observed[0][4] is V34Decision.WAIT
    assert btc_c2_observed[0][5] == 0

    assert (
        btc_c2_observed[1][2]
        == "conflicting_retry"
    )
    assert btc_c2_observed[1][3] is V34Decision.CALL
    assert btc_c2_observed[1][4] is V34Decision.WAIT
    assert btc_c2_observed[1][5] == 1
    assert (
        btc_c2_observed[1][6]
        == btc_c2_observed[0][6]
    )


def test_evidence_rejection_has_zero_state_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _Control()
    trace: list[str] = []
    previous: list[tuple[str, int | None]] = []

    _reset_clock()

    _install_functions(
        monkeypatch,
        control=control,
        trace=trace,
        previous_seen=previous,
    )

    seen: list[str] = []

    def hook(
        result: EvaluationReturn,
    ) -> bool:
        seen.append(result.underlying)
        return False

    history = _History(control, _C1, trace)
    delta = _Delta(control, trace)

    observer = _new_research(
        control,
        history,
        delta,
        hook=hook,
    )

    cycle = observer.run_cycle()

    assert cycle.evaluated is True
    assert len(cycle.signals) == 2
    assert seen == ["BTC", "ETH"]
    assert len(cycle.evidence_faults) == 2
    assert (
        observer.last_candle_close_ms
        == _candles("BTC", _C1).candle_close_ms
    )
    assert observer.previous_chain("BTC") is not None
    assert observer.previous_chain("ETH") is not None


def test_evidence_exception_has_zero_state_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _Control()
    trace: list[str] = []
    previous: list[tuple[str, int | None]] = []

    _reset_clock()

    _install_functions(
        monkeypatch,
        control=control,
        trace=trace,
        previous_seen=previous,
    )

    def hook(
        result: EvaluationReturn,
    ) -> bool:
        if result.underlying == "BTC":
            raise RuntimeError("journal unavailable")
        return False

    history = _History(control, _C1, trace)
    delta = _Delta(control, trace)

    observer = _new_research(
        control,
        history,
        delta,
        hook=hook,
    )

    cycle = observer.run_cycle()

    assert cycle.evaluated is True
    assert len(cycle.evidence_faults) == 2
    assert "journal unavailable" in cycle.evidence_faults[0]
    assert observer.previous_chain("BTC") is not None
    assert observer.previous_chain("ETH") is not None


def test_misalignment_matches_frozen_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _Control()
    trace: list[str] = []
    previous: list[tuple[str, int | None]] = []

    _reset_clock()

    _install_functions(
        monkeypatch,
        control=control,
        trace=trace,
        previous_seen=previous,
    )

    history = _History(control, _C1, trace)

    def misaligned_fetch(
        underlying: str,
        *,
        count: int = 240,
        now_s: int | None = None,
    ) -> DeltaCandleSnapshot:
        del count, now_s

        return _candles(
            underlying,
            (
                _C1
                if underlying == "BTC"
                else _C1 + 300
            ),
        )

    history.fetch_5m_candles = misaligned_fetch  # type: ignore[method-assign]

    delta = _Delta(control, trace)

    frozen = _new_frozen(
        control,
        history,
        delta,
    )

    frozen_cycle = frozen.run_cycle()

    assert frozen_cycle.evaluated is False
    assert delta.trace == trace

    research_history = _History(
        control,
        _C1,
        [],
    )
    research_history.fetch_5m_candles = (  # type: ignore[method-assign]
        misaligned_fetch
    )
    research_delta = _Delta(control, [])

    research = _new_research(
        control,
        research_history,
        research_delta,
    )

    research_cycle = research.run_cycle()

    assert research_cycle.evaluated is False
    assert research_cycle.failure_stage == (
        "candle_alignment"
    )


def test_duplicate_boundary_matches_frozen_no_chain_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _Control()
    trace: list[str] = []
    previous: list[tuple[str, int | None]] = []

    _reset_clock()

    _install_functions(
        monkeypatch,
        control=control,
        trace=trace,
        previous_seen=previous,
    )

    history = _History(control, _C1, trace)
    delta = _Delta(control, trace)

    observer = _new_research(
        control,
        history,
        delta,
    )

    first = observer.run_cycle()

    assert first.evaluated is True

    trace.clear()

    second = observer.run_cycle()

    assert second.evaluated is False
    assert second.failure_stage == "duplicate_boundary"
    assert trace == [
        "history:BTC",
        "history:ETH",
    ]
