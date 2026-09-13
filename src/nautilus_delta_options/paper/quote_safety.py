"""Receipt-clock validation shared by market observation boundaries."""

from dataclasses import replace
from decimal import Decimal

from nautilus_delta_options.delta.public_client import DeltaOptionChainSnapshot

MAX_QUOTE_AGE_NS = 15_000_000_000
MAX_FUTURE_SKEW_NS = 5_000_000_000


def validate_quote_time(event_ns: int, observed_ns: int) -> None:
    if event_ns <= 0 or observed_ns <= 0:
        raise ValueError("Quote and observation timestamps must be positive")
    age = observed_ns - event_ns
    if age > MAX_QUOTE_AGE_NS:
        raise ValueError("Stale quote")
    if age < -MAX_FUTURE_SKEW_NS:
        raise ValueError("Future quote")


def fresh_chain(chain: DeltaOptionChainSnapshot, observed_ns: int) -> DeltaOptionChainSnapshot:
    rows = []
    for ticker in chain.tickers:
        try:
            validate_quote_time(ticker.exchange_timestamp * 1000, observed_ns)
        except ValueError:
            continue
        numeric = (
            ticker.mark_price,
            ticker.spot_price,
            ticker.delta,
            ticker.gamma,
            ticker.theta,
            ticker.vega,
            ticker.mark_iv,
            ticker.best_bid,
            ticker.best_ask,
            ticker.bid_size,
            ticker.ask_size,
        )
        if any(isinstance(x, Decimal) and not x.is_finite() for x in numeric):
            continue
        rows.append(ticker)
    if not rows:
        raise ValueError("No fresh Delta option observations")
    return replace(chain, tickers=tuple(rows))
