from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, cast

from nautilus_delta_options.delta.models import (
    DeltaOptionTicker,
)
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaOptionProductsSnapshot,
    DeltaUnderlying,
)
from nautilus_delta_options.delta.snapshot import (
    DeltaMarketSnapshot,
    build_market_snapshot,
)
from nautilus_delta_options.paper.proposals import (
    PaperEntryProposal,
    PaperProposalConfig,
    revalidate_paper_entry_proposal,
)
from nautilus_delta_options.paper.session import (
    PaperLedgerSession,
)
from nautilus_delta_options.paper.signal_entries_v32 import (
    PaperSignalEntryResult,
    process_v32_signals,
)
from nautilus_delta_options.selection.eligibility import (
    EligibilityConfig,
)
from nautilus_delta_options.selection.entry_safety import (
    signal_is_fresh,
)
from nautilus_delta_options.signals.binance import (
    BinanceCandleSnapshot,
)
from nautilus_delta_options.signals.v32 import (
    V32Signal,
    evaluate_v32_signal,
)


class DeltaFastEntryMarketData(Protocol):
    def fetch_option_chain(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionChainSnapshot: ...

    def fetch_option_products(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionProductsSnapshot: ...

    def fetch_option_tickers(
        self,
        symbols: Sequence[str],
    ) -> tuple[DeltaOptionTicker, ...]: ...


class BinanceFastEntryMarketData(Protocol):
    def fetch_v31_candles(
        self,
        underlying: DeltaUnderlying,
    ) -> BinanceCandleSnapshot: ...


@dataclass(frozen=True, slots=True)
class PaperFastEntryCycle:
    candle_close_ms: int | None
    signals: tuple[V32Signal, ...]
    entry_results: tuple[PaperSignalEntryResult, ...]
    warnings: tuple[str, ...]


def run_fast_entry_cycle(
    *,
    delta_client: DeltaFastEntryMarketData,
    signal_client: BinanceFastEntryMarketData,
    session: PaperLedgerSession,
    entries_enabled: bool,
    proposal_config: PaperProposalConfig,
    previous_candle_close_ms: int | None = None,
    underlyings: Sequence[DeltaUnderlying] = ("BTC", "ETH"),
    eligibility_config: EligibilityConfig | None = None,
    clock_ns: Callable[[], int] = time.time_ns,
    as_of: date | None = None,
) -> PaperFastEntryCycle:
    if not underlyings:
        raise ValueError("At least one underlying is required")

    if len(underlyings) != len(set(underlyings)):
        raise ValueError("Underlyings must be unique")

    resolved_eligibility = eligibility_config or EligibilityConfig()

    candle_snapshots = tuple(
        signal_client.fetch_v31_candles(underlying)
        for underlying in underlyings
    )

    signals = tuple(
        evaluate_v32_signal(snapshot)
        for snapshot in candle_snapshots
    )

    candle_closes = {
        signal.candle_close_ms
        for signal in signals
    }

    # BTC and ETH must reach the same completed 5m boundary
    # before either signal is allowed to enter. This preserves
    # the V3.2 same-candle correlation arbitration.
    if len(candle_closes) != 1:
        closes = ", ".join(
            f"{signal.underlying}={signal.candle_close_ms}"
            for signal in signals
        )

        return PaperFastEntryCycle(
            candle_close_ms=None,
            signals=signals,
            entry_results=(),
            warnings=(
                f"Completed-candle boundary not aligned: {closes}",
            ),
        )

    candle_close_ms = next(iter(candle_closes))

    # Evaluate each completed 5m candle only once per running
    # watcher. Persistence inside the V3.2 entry path remains
    # the authoritative duplicate-open protection.
    if (
        previous_candle_close_ms is not None
        and candle_close_ms <= previous_candle_close_ms
    ):
        return PaperFastEntryCycle(
            candle_close_ms=candle_close_ms,
            signals=signals,
            entry_results=(),
            warnings=(),
        )

    if not entries_enabled:
        entry_results = process_v32_signals(
            signals,
            (),
            session=session,
            entries_enabled=False,
            proposal_config=proposal_config,
            observed_ns=clock_ns(),
        )

        return PaperFastEntryCycle(
            candle_close_ms=candle_close_ms,
            signals=signals,
            entry_results=entry_results,
            warnings=(),
        )

    # WAIT-only candles do not need a Delta option-chain fetch.
    if not any(signal.active for signal in signals):
        entry_results = process_v32_signals(
            signals,
            (),
            session=session,
            entries_enabled=True,
            proposal_config=proposal_config,
            observed_ns=clock_ns(),
        )

        return PaperFastEntryCycle(
            candle_close_ms=candle_close_ms,
            signals=signals,
            entry_results=entry_results,
            warnings=(),
        )

    cycle_date = as_of or datetime.now(UTC).date()

    snapshots = tuple(
        _fetch_snapshot(
            delta_client=delta_client,
            underlying=underlying,
            as_of=cycle_date,
            eligibility_config=resolved_eligibility,
            proposal_config=proposal_config,
            clock_ns=clock_ns,
        )
        for underlying in underlyings
    )

    warnings = [
        f"{snapshot.underlying}: {error}"
        for snapshot in snapshots
        for error in snapshot.errors
    ]

    def revalidate_candidate(
        signal: V32Signal,
        proposal: PaperEntryProposal,
    ) -> PaperEntryProposal | None:
        try:
            tickers = delta_client.fetch_option_tickers(
                (proposal.record.product.symbol,),
            )

            if len(tickers) != 1:
                raise ValueError(
                    "Targeted Delta refresh did not "
                    "return exactly one ticker"
                )

            received_ns = clock_ns()

            if not signal_is_fresh(
                candle_close_ms=signal.candle_close_ms,
                observed_ns=received_ns,
                config=proposal_config.entry_safety,
            ):
                warnings.append(
                    f"{proposal.record.product.symbol}: "
                    "signal became stale during final refresh"
                )
                return None

            ticker = tickers[0]

            latest_event_ns = (
                ticker.exchange_timestamp * 1_000
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
                raise ValueError(
                    "Delta timestamp is ahead of the VPS "
                    "clock during final refresh"
                )

            captured_ns = max(
                received_ns,
                latest_event_ns,
            )

            refreshed_underlying = cast(
                DeltaUnderlying,
                proposal.record.ticker.underlying,
            )

            refreshed_snapshot = build_market_snapshot(
                catalog=DeltaOptionProductsSnapshot(
                    underlying=refreshed_underlying,
                    products=(proposal.record.product,),
                    rejected_records=(),
                ),
                chain=DeltaOptionChainSnapshot(
                    underlying=refreshed_underlying,
                    tickers=(ticker,),
                    rejected_records=(),
                ),
                as_of=cycle_date,
                captured_ns=captured_ns,
                eligibility_config=resolved_eligibility,
            )

            refreshed_record = next(
                (
                    record
                    for record
                    in refreshed_snapshot.records
                    if (
                        record.product.product_id
                        == proposal.record.product.product_id
                        and record.product.symbol
                        == proposal.record.product.symbol
                    )
                ),
                None,
            )

            if refreshed_record is None:
                warnings.append(
                    f"{proposal.record.product.symbol}: "
                    "final refreshed market record unavailable"
                )
                return None

            refreshed = revalidate_paper_entry_proposal(
                proposal,
                refreshed_record,
                ledger=session.snapshot(),
                config=proposal_config,
            )

            if refreshed is None:
                warnings.append(
                    f"{proposal.record.product.symbol}: "
                    "final payoff revalidation rejected"
                )

            return refreshed

        except Exception as error:
            warnings.append(
                f"{proposal.record.product.symbol}: "
                f"final refresh failed: {error}"
            )
            return None

    # Timestamp after the initial BTC/ETH Delta snapshots.
    observed_ns = clock_ns()

    entry_results = process_v32_signals(
        signals,
        snapshots,
        session=session,
        entries_enabled=True,
        proposal_config=proposal_config,
        observed_ns=observed_ns,
        candidate_revalidator=revalidate_candidate,
    )

    return PaperFastEntryCycle(
        candle_close_ms=candle_close_ms,
        signals=signals,
        entry_results=entry_results,
        warnings=tuple(warnings),
    )


def _fetch_snapshot(
    *,
    delta_client: DeltaFastEntryMarketData,
    underlying: DeltaUnderlying,
    as_of: date,
    eligibility_config: EligibilityConfig,
    proposal_config: PaperProposalConfig,
    clock_ns: Callable[[], int],
) -> DeltaMarketSnapshot:
    catalog = delta_client.fetch_option_products(underlying)
    chain = delta_client.fetch_option_chain(underlying)

    received_ns = clock_ns()

    latest_event_ns = max(
        (
            ticker.exchange_timestamp * 1_000
            for ticker in chain.tickers
        ),
        default=received_ns,
    )

    max_future_skew_ns = int(
        proposal_config.entry_safety.max_future_skew_seconds
        * Decimal("1000000000")
    )

    if latest_event_ns > received_ns + max_future_skew_ns:
        raise ValueError(
            f"{underlying}: Delta timestamps are ahead of "
            "the VPS clock; check NTP"
        )

    captured_ns = max(
        received_ns,
        latest_event_ns,
    )

    return build_market_snapshot(
        catalog=catalog,
        chain=chain,
        as_of=as_of,
        captured_ns=captured_ns,
        eligibility_config=eligibility_config,
    )
