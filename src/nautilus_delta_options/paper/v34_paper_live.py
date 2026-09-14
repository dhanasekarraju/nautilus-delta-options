from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, cast

from nautilus_delta_options.delta.models import (
    DeltaOptionContractType,
    DeltaOptionTicker,
)
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaOptionProductsSnapshot,
    DeltaUnderlying,
)
from nautilus_delta_options.delta.snapshot import (
    DeltaMarketSnapshot,
    DeltaOptionMarketRecord,
    build_market_snapshot,
)
from nautilus_delta_options.paper.ledger import (
    PaperLedger,
    PaperPosition,
)
from nautilus_delta_options.paper.portfolio_risk import (
    PortfolioRiskConfig,
    PortfolioRiskDecision,
    evaluate_portfolio_entry,
)
from nautilus_delta_options.paper.proposals import (
    PaperEntryProposal,
    PaperProposalConfig,
    build_ranked_paper_entry_proposals,
    revalidate_paper_entry_proposal,
)
from nautilus_delta_options.paper.session import (
    PaperLedgerSession,
    PaperSignalAlreadyConsumedError,
)
from nautilus_delta_options.paper.v34_shadow import (
    V34ShadowCycle,
    V34ShadowQualitySelection,
)
from nautilus_delta_options.selection.eligibility import (
    EligibilityConfig,
)
from nautilus_delta_options.selection.entry_safety import (
    EntrySafetyConfig,
    signal_is_fresh,
)
from nautilus_delta_options.selection.v34_quality import (
    V34ContractQuality,
    V34QualityConfig,
    rank_v34_contract_quality,
)
from nautilus_delta_options.signals.v34 import (
    V34Decision,
    V34ShadowSignal,
)


class V34PaperMarketData(Protocol):
    def fetch_option_products(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionProductsSnapshot: ...

    def fetch_option_tickers(
        self,
        symbols: tuple[str, ...],
    ) -> tuple[DeltaOptionTicker, ...]: ...


class V34PaperEntryStatus(StrEnum):
    WAIT = "wait"
    ALREADY_CONSUMED = "already_consumed"
    DIRECTION_BLOCKED = "direction_blocked"
    CORRELATED_SIGNAL_SKIPPED = "correlated_signal_skipped"

    NO_QUALITY_CONTRACT = "no_quality_contract"
    STALE_SIGNAL = "stale_signal"

    MARKET_REFRESH_FAILED = "market_refresh_failed"
    QUALITY_REVALIDATION_REJECTED = (
        "quality_revalidation_rejected"
    )
    NO_PROPOSAL = "no_proposal"

    RISK_REJECTED = "risk_rejected"
    PAYOFF_REVALIDATION_REJECTED = (
        "payoff_revalidation_rejected"
    )

    OPENED = "opened"


@dataclass(frozen=True, slots=True)
class V34PaperEntryResult:
    status: V34PaperEntryStatus
    signal: V34ShadowSignal
    quality: V34ContractQuality | None
    proposal: PaperEntryProposal | None
    position: PaperPosition | None
    risk_decisions: tuple[PortfolioRiskDecision, ...]
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class V34PaperEntryCycle:
    candle_close_ms: int | None
    evaluated: bool
    results: tuple[V34PaperEntryResult, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _RefreshResult:
    record: DeltaOptionMarketRecord | None
    detail: str | None


def v34_paper_episode_key(
    signal: V34ShadowSignal,
) -> str:
    """Shared BTC/ETH same-candle + same-direction receipt key."""

    if signal.decision is V34Decision.WAIT:
        raise ValueError("WAIT signal has no paper episode key")

    return (
        f"v34:{signal.candle_close_ms}:"
        f"{signal.decision.value}"
    )


def default_v34_paper_proposal_config(
) -> PaperProposalConfig:
    """Keep V3.3 execution safety without narrowing V3.4 strategy.

    V3.4 itself owns DTE, delta, spread and contract-quality
    eligibility. This layer keeps quote freshness, future-clock
    protection, signal freshness, sizing and payoff validation.

    The very-wide moneyness ceiling and one-hour settlement floor
    avoid silently replacing V3.4's explicit 1-3 DTE / delta rules
    with the older V3.3 strategy boundaries.
    """

    return PaperProposalConfig(
        entry_safety=EntrySafetyConfig(
            min_abs_delta=Decimal("0.35"),
            max_abs_delta=Decimal("0.65"),
            max_moneyness_fraction=Decimal("0.99"),
            min_hours_to_settlement=Decimal("1"),
            max_quote_age_seconds=Decimal("15"),
            max_signal_age_seconds=Decimal("15"),
            max_future_skew_seconds=Decimal("5"),
        ),
    )


def default_v34_paper_eligibility_config(
) -> EligibilityConfig:
    """Broad market mapper limits matching the V3.4 hard universe.

    V34QualityConfig remains the stricter authoritative quality
    filter and is rechecked on each exact-contract refresh.
    """

    return EligibilityConfig(
        min_dte=1,
        max_dte=3,
        max_spread_fraction=Decimal("0.015"),
    )


def run_v34_paper_entry_cycle(
    *,
    cycle: V34ShadowCycle,
    delta_client: V34PaperMarketData,
    session: PaperLedgerSession,
    proposal_config: PaperProposalConfig | None = None,
    portfolio_config: PortfolioRiskConfig | None = None,
    eligibility_config: EligibilityConfig | None = None,
    quality_config: V34QualityConfig | None = None,
    clock_ns: Callable[[], int] = time.time_ns,
    as_of: date | None = None,
    readiness_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> V34PaperEntryCycle:
    resolved_proposal = (
        proposal_config
        or default_v34_paper_proposal_config()
    )
    resolved_eligibility = (
        eligibility_config
        or default_v34_paper_eligibility_config()
    )
    resolved_quality = quality_config or V34QualityConfig()
    cycle_date = as_of or datetime.now(UTC).date()

    if not cycle.evaluated:
        return V34PaperEntryCycle(
            candle_close_ms=cycle.candle_close_ms,
            evaluated=False,
            results=(),
            warnings=cycle.warnings,
        )

    quality_by_underlying = {
        row.underlying: row
        for row in cycle.quality
    }

    results: list[V34PaperEntryResult | None] = [
        None
        for _ in cycle.signals
    ]

    for index, signal in enumerate(cycle.signals):
        if signal.decision is V34Decision.WAIT:
            results[index] = _result(
                V34PaperEntryStatus.WAIT,
                signal,
            )

    for decision in (
        V34Decision.CALL,
        V34Decision.PUT,
    ):
        group = [
            index
            for index, signal in enumerate(cycle.signals)
            if signal.decision is decision
        ]

        if not group:
            continue

        # Preserve the V3.2/V3.3 correlation principle:
        # BTC and ETH with the same direction on the same
        # completed candle compete for one entry.
        #
        # V3.4 directional conviction is authoritative first.
        # Its own contract-quality score breaks ties next.
        group.sort(
            key=lambda index: _candidate_order_key(
                cycle.signals[index],
                quality_by_underlying.get(
                    cast(
                        DeltaUnderlying,
                        cycle.signals[index].underlying,
                    ),
                ),
            ),
        )

        unresolved: list[int] = []

        for index in group:
            signal = cycle.signals[index]
            episode_key = v34_paper_episode_key(signal)

            if session.has_consumed_signal(episode_key):
                results[index] = _result(
                    V34PaperEntryStatus.ALREADY_CONSUMED,
                    signal,
                    quality=_quality_for_signal(
                        signal,
                        quality_by_underlying,
                    ),
                )
            else:
                unresolved.append(index)

        if not unresolved:
            continue

        contract_type = _contract_type_for(
            cycle.signals[unresolved[0]],
        )

        if _has_directional_position(
            session.snapshot().open_positions,
            contract_type,
        ):
            for index in unresolved:
                signal = cycle.signals[index]

                results[index] = _result(
                    V34PaperEntryStatus.DIRECTION_BLOCKED,
                    signal,
                    quality=_quality_for_signal(
                        signal,
                        quality_by_underlying,
                    ),
                )

            continue

        winner_index: int | None = None

        for index in unresolved:
            signal = cycle.signals[index]
            quality = _quality_for_signal(
                signal,
                quality_by_underlying,
            )

            if quality is None:
                results[index] = _result(
                    V34PaperEntryStatus.NO_QUALITY_CONTRACT,
                    signal,
                    detail=(
                        "V3.4 produced a direction but no "
                        "quality-approved contract exists"
                    ),
                )
                continue

            result = _try_open(
                signal=signal,
                selected_quality=quality,
                delta_client=delta_client,
                session=session,
                proposal_config=resolved_proposal,
                portfolio_config=portfolio_config,
                eligibility_config=resolved_eligibility,
                quality_config=resolved_quality,
                clock_ns=clock_ns,
                as_of=cycle_date,
                readiness_guard=readiness_guard,
            )

            results[index] = result

            if result.status is V34PaperEntryStatus.OPENED:
                winner_index = index
                break

        if winner_index is not None:
            for index in unresolved:
                if index == winner_index:
                    continue

                if results[index] is not None:
                    continue

                signal = cycle.signals[index]

                results[index] = _result(
                    V34PaperEntryStatus.CORRELATED_SIGNAL_SKIPPED,
                    signal,
                    quality=_quality_for_signal(
                        signal,
                        quality_by_underlying,
                    ),
                    detail=(
                        "Another BTC/ETH V3.4 signal won "
                        "same-candle same-direction arbitration"
                    ),
                )

    if any(result is None for result in results):
        raise RuntimeError(
            "V3.4 paper cycle left an unresolved signal"
        )

    return V34PaperEntryCycle(
        candle_close_ms=cycle.candle_close_ms,
        evaluated=True,
        results=tuple(
            cast(V34PaperEntryResult, result)
            for result in results
        ),
        warnings=cycle.warnings,
    )


def _try_open(
    *,
    signal: V34ShadowSignal,
    selected_quality: V34ContractQuality,
    delta_client: V34PaperMarketData,
    session: PaperLedgerSession,
    proposal_config: PaperProposalConfig,
    portfolio_config: PortfolioRiskConfig | None,
    eligibility_config: EligibilityConfig,
    quality_config: V34QualityConfig,
    clock_ns: Callable[[], int],
    as_of: date,
    readiness_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> V34PaperEntryResult:
    contract_type = _contract_type_for(signal)
    episode_key = v34_paper_episode_key(signal)

    if selected_quality.contract_type != contract_type:
        return _result(
            V34PaperEntryStatus.NO_QUALITY_CONTRACT,
            signal,
            quality=selected_quality,
            detail=(
                "Selected V3.4 quality contract direction "
                "does not match signal direction"
            ),
        )

    observed_ns = clock_ns()

    if not signal_is_fresh(
        candle_close_ms=signal.candle_close_ms,
        observed_ns=observed_ns,
        config=proposal_config.entry_safety,
    ):
        return _result(
            V34PaperEntryStatus.STALE_SIGNAL,
            signal,
            quality=selected_quality,
        )

    ledger_snapshot = session.snapshot()

    if _has_directional_position(
        ledger_snapshot.open_positions,
        contract_type,
    ):
        return _result(
            V34PaperEntryStatus.DIRECTION_BLOCKED,
            signal,
            quality=selected_quality,
        )

    try:
        catalog = delta_client.fetch_option_products(
            cast(DeltaUnderlying, signal.underlying),
        )
    except Exception as error:
        return _result(
            V34PaperEntryStatus.MARKET_REFRESH_FAILED,
            signal,
            quality=selected_quality,
            detail=f"product refresh failed: {error}",
        )

    initial = _refresh_selected_record(
        signal=signal,
        selected_quality=selected_quality,
        catalog=catalog,
        delta_client=delta_client,
        proposal_config=proposal_config,
        eligibility_config=eligibility_config,
        quality_config=quality_config,
        clock_ns=clock_ns,
        as_of=as_of,
    )

    if initial.record is None:
        return _result(
            (
                V34PaperEntryStatus.QUALITY_REVALIDATION_REJECTED
                if initial.detail == "quality"
                else V34PaperEntryStatus.MARKET_REFRESH_FAILED
            ),
            signal,
            quality=selected_quality,
            detail=initial.detail,
        )

    ledger_snapshot = session.snapshot()

    proposals = build_ranked_paper_entry_proposals(
        _single_record_snapshot(initial.record),
        ledger=ledger_snapshot,
        config=proposal_config,
        contract_type=contract_type,
    )

    if not proposals:
        return _result(
            V34PaperEntryStatus.NO_PROPOSAL,
            signal,
            quality=selected_quality,
            detail=(
                "V3.4-selected contract failed existing "
                "payoff/sizing/entry-safety proposal gate"
            ),
        )

    proposal = proposals[0]

    # Because the market snapshot contains exactly the
    # V3.4-selected symbol, the generic proposal ranker
    # cannot substitute another contract.
    if proposal.record.product.symbol != selected_quality.symbol:
        raise RuntimeError(
            "V3.4 exact-contract proposal changed symbol"
        )

    first_risk = evaluate_portfolio_entry(
        ledger_snapshot,
        proposal,
        config=portfolio_config,
    )

    risk_decisions: tuple[PortfolioRiskDecision, ...] = (first_risk,)

    if not first_risk.approved:
        return _result(
            V34PaperEntryStatus.RISK_REJECTED,
            signal,
            quality=selected_quality,
            proposal=proposal,
            risk_decisions=risk_decisions,
        )

    # Exact-contract second refresh immediately before
    # mutation. This is the V3.3 no-chasing principle.
    fresh = _refresh_selected_record(
        signal=signal,
        selected_quality=selected_quality,
        catalog=catalog,
        delta_client=delta_client,
        proposal_config=proposal_config,
        eligibility_config=eligibility_config,
        quality_config=quality_config,
        clock_ns=clock_ns,
        as_of=as_of,
    )

    if fresh.record is None:
        return _result(
            (
                V34PaperEntryStatus.QUALITY_REVALIDATION_REJECTED
                if fresh.detail == "quality"
                else V34PaperEntryStatus.MARKET_REFRESH_FAILED
            ),
            signal,
            quality=selected_quality,
            proposal=proposal,
            risk_decisions=risk_decisions,
            detail=fresh.detail,
        )

    observed_ns = clock_ns()

    if not signal_is_fresh(
        candle_close_ms=signal.candle_close_ms,
        observed_ns=observed_ns,
        config=proposal_config.entry_safety,
    ):
        return _result(
            V34PaperEntryStatus.STALE_SIGNAL,
            signal,
            quality=selected_quality,
            proposal=proposal,
            risk_decisions=risk_decisions,
            detail=(
                "signal became stale during final "
                "exact-contract refresh"
            ),
        )

    ledger_snapshot = session.snapshot()

    if _has_directional_position(
        ledger_snapshot.open_positions,
        contract_type,
    ):
        return _result(
            V34PaperEntryStatus.DIRECTION_BLOCKED,
            signal,
            quality=selected_quality,
            proposal=proposal,
            risk_decisions=risk_decisions,
        )

    refreshed = revalidate_paper_entry_proposal(
        proposal,
        fresh.record,
        ledger=ledger_snapshot,
        config=proposal_config,
    )

    if refreshed is None:
        return _result(
            V34PaperEntryStatus.PAYOFF_REVALIDATION_REJECTED,
            signal,
            quality=selected_quality,
            proposal=proposal,
            risk_decisions=risk_decisions,
            detail=(
                "fresh exact quote no longer satisfies "
                "original stop/target economics"
            ),
        )

    final_risk = evaluate_portfolio_entry(
        session.snapshot(),
        refreshed,
        config=portfolio_config,
    )

    risk_decisions = (
        *risk_decisions,
        final_risk,
    )

    if not final_risk.approved:
        return _result(
            V34PaperEntryStatus.RISK_REJECTED,
            signal,
            quality=selected_quality,
            proposal=refreshed,
            risk_decisions=risk_decisions,
        )

    if session.has_consumed_signal(episode_key):
        return _result(
            V34PaperEntryStatus.ALREADY_CONSUMED,
            signal,
            quality=selected_quality,
            proposal=refreshed,
            risk_decisions=risk_decisions,
        )

    def admission(current: PaperLedger) -> None:
        from nautilus_delta_options.paper.quote_safety import validate_quote_time

        now = clock_ns()
        validate_quote_time(refreshed.record.ticker.exchange_timestamp * 1000, now)
        if not signal_is_fresh(
            candle_close_ms=signal.candle_close_ms, observed_ns=now,
            config=proposal_config.entry_safety,
        ):
            raise ValueError("Signal expired before ledger admission")
        if _has_directional_position(current.open_positions, contract_type):
            raise ValueError("Directional exposure changed before ledger admission")
        if not evaluate_portfolio_entry(
            current, refreshed, config=portfolio_config, observed_ns=now,
        ).approved:
            raise ValueError("Portfolio exposure changed before ledger admission")
        _, complete = current.liquidation_equity(observed_ns=now)
        if not complete:
            raise ValueError("Unresolved or stale open-position valuation")

    try:
        position = session.open_long_for_signal(
            refreshed.record,
            signal_key=episode_key,
            signal_underlying=signal.underlying,
            candle_closed_ns=(
                signal.candle_close_ms
                * 1_000_000
            ),
            contracts=refreshed.sizing.contracts,
            stop_exit_bid=(
                refreshed.levels.stop_exit_bid
            ),
            target_exit_bid=(
                refreshed.levels.target_exit_bid
            ),
            stop_spot=refreshed.levels.stop_spot,
            target_spot=refreshed.levels.target_spot,
            admission=admission,
            readiness_guard=readiness_guard,
        )
    except PaperSignalAlreadyConsumedError:
        return _result(
            V34PaperEntryStatus.ALREADY_CONSUMED,
            signal,
            quality=selected_quality,
            proposal=refreshed,
            risk_decisions=risk_decisions,
        )

    return _result(
        V34PaperEntryStatus.OPENED,
        signal,
        quality=selected_quality,
        proposal=refreshed,
        position=position,
        risk_decisions=risk_decisions,
    )


def _refresh_selected_record(
    *,
    signal: V34ShadowSignal,
    selected_quality: V34ContractQuality,
    catalog: DeltaOptionProductsSnapshot,
    delta_client: V34PaperMarketData,
    proposal_config: PaperProposalConfig,
    eligibility_config: EligibilityConfig,
    quality_config: V34QualityConfig,
    clock_ns: Callable[[], int],
    as_of: date,
) -> _RefreshResult:
    try:
        tickers = delta_client.fetch_option_tickers(
            (selected_quality.symbol,),
        )

        if len(tickers) != 1:
            return _RefreshResult(
                record=None,
                detail=(
                    "exact ticker refresh did not return "
                    "exactly one contract"
                ),
            )

        ticker = tickers[0]

        if ticker.symbol != selected_quality.symbol:
            return _RefreshResult(
                record=None,
                detail="exact ticker refresh changed symbol",
            )

        if ticker.underlying != signal.underlying:
            return _RefreshResult(
                record=None,
                detail=(
                    "exact ticker refresh changed "
                    "underlying"
                ),
            )

        received_ns = clock_ns()
        latest_event_ns = (
            ticker.exchange_timestamp
            * 1_000
        )

        max_future_skew_ns = int(
            proposal_config.entry_safety
            .max_future_skew_seconds
            * Decimal("1000000000")
        )

        if (
            latest_event_ns
            > received_ns + max_future_skew_ns
        ):
            return _RefreshResult(
                record=None,
                detail=(
                    "Delta timestamp is ahead of "
                    "paper-live clock"
                ),
            )

        # Re-run the V3.4 quality layer on the exact refreshed
        # ticker. With a one-contract chain we ignore the new
        # relative score; this call is specifically enforcing
        # V3.4's hard DTE/delta/spread/Greeks/liquidity floors.
        exact_chain = DeltaOptionChainSnapshot(
            underlying=cast(
                DeltaUnderlying,
                signal.underlying,
            ),
            tickers=(ticker,),
            rejected_records=(),
        )

        refreshed_quality = (
            rank_v34_contract_quality(
                exact_chain,
                contract_type=_contract_type_for(signal),
                as_of=as_of,
                config=quality_config,
            )
        )

        if (
            not refreshed_quality
            or refreshed_quality[0].symbol
            != selected_quality.symbol
        ):
            return _RefreshResult(
                record=None,
                detail="quality",
            )

        captured_ns = max(
            received_ns,
            latest_event_ns,
        )

        snapshot = build_market_snapshot(
            catalog=catalog,
            chain=exact_chain,
            as_of=as_of,
            captured_ns=captured_ns,
            eligibility_config=eligibility_config,
        )

        record = next(
            (
                row
                for row in snapshot.records
                if (
                    row.product.symbol
                    == selected_quality.symbol
                    and row.ticker.symbol
                    == selected_quality.symbol
                )
            ),
            None,
        )

        if record is None:
            detail = (
                "; ".join(snapshot.errors)
                if snapshot.errors
                else "refreshed market record unavailable"
            )

            return _RefreshResult(
                record=None,
                detail=detail,
            )

        return _RefreshResult(
            record=record,
            detail=None,
        )

    except Exception as error:
        return _RefreshResult(
            record=None,
            detail=f"exact refresh failed: {error}",
        )


def _single_record_snapshot(
    record: DeltaOptionMarketRecord,
) -> DeltaMarketSnapshot:
    return DeltaMarketSnapshot(
        underlying=cast(
            DeltaUnderlying,
            record.ticker.underlying,
        ),
        captured_ns=record.quote.ts_init
        if record.quote is not None
        else record.greeks.ts_init,
        product_count=1,
        ticker_count=1,
        records=(record,),
        unmatched_product_ids=(),
        errors=(),
    )


def _candidate_order_key(
    signal: V34ShadowSignal,
    quality: V34ShadowQualitySelection | None,
) -> tuple[float, float, str]:
    selected = _quality_for_signal(
        signal,
        (
            {}
            if quality is None
            else {quality.underlying: quality}
        ),
    )

    total_score = (
        signal.call_score
        if signal.decision is V34Decision.CALL
        else signal.put_score
    )

    quality_score = (
        selected.score
        if selected is not None
        else -1.0
    )

    return (
        -total_score,
        -quality_score,
        signal.underlying,
    )


def _quality_for_signal(
    signal: V34ShadowSignal,
    quality_by_underlying: dict[
        DeltaUnderlying,
        V34ShadowQualitySelection,
    ],
) -> V34ContractQuality | None:
    row = quality_by_underlying.get(
        cast(
            DeltaUnderlying,
            signal.underlying,
        ),
    )

    if row is None:
        return None

    if signal.decision is V34Decision.CALL:
        return row.best_call

    if signal.decision is V34Decision.PUT:
        return row.best_put

    return None


def _contract_type_for(
    signal: V34ShadowSignal,
) -> DeltaOptionContractType:
    if signal.decision is V34Decision.CALL:
        return "call_options"

    if signal.decision is V34Decision.PUT:
        return "put_options"

    raise ValueError(
        "WAIT V3.4 signal has no option direction"
    )


def _has_directional_position(
    positions: tuple[PaperPosition, ...],
    contract_type: DeltaOptionContractType,
) -> bool:
    return any(
        position.contract_type == contract_type
        for position in positions
    )


def _result(
    status: V34PaperEntryStatus,
    signal: V34ShadowSignal,
    *,
    quality: V34ContractQuality | None = None,
    proposal: PaperEntryProposal | None = None,
    position: PaperPosition | None = None,
    risk_decisions: tuple[
        PortfolioRiskDecision,
        ...,
    ] = (),
    detail: str | None = None,
) -> V34PaperEntryResult:
    return V34PaperEntryResult(
        status=status,
        signal=signal,
        quality=quality,
        proposal=proposal,
        position=position,
        risk_decisions=risk_decisions,
        detail=detail,
    )
