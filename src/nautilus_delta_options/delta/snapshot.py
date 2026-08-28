from dataclasses import dataclass
from datetime import date

from nautilus_trader.model import CryptoOption, OptionGreeks, QuoteTick

from nautilus_delta_options.delta.market_mapper import (
    map_delta_ticker_to_option_greeks,
    map_delta_ticker_to_quote_tick,
)
from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.nautilus_mapper import (
    map_delta_product_to_crypto_option,
)
from nautilus_delta_options.delta.product import DeltaOptionProduct
from nautilus_delta_options.delta.public_client import (
    DeltaOptionChainSnapshot,
    DeltaOptionProductsSnapshot,
    DeltaUnderlying,
)
from nautilus_delta_options.selection.eligibility import (
    EligibilityConfig,
    EligibilityResult,
    evaluate_market_eligibility,
)


@dataclass(frozen=True, slots=True)
class DeltaOptionMarketRecord:
    product: DeltaOptionProduct
    ticker: DeltaOptionTicker
    instrument: CryptoOption
    quote: QuoteTick
    greeks: OptionGreeks
    eligibility: EligibilityResult


@dataclass(frozen=True, slots=True)
class DeltaMarketSnapshot:
    underlying: DeltaUnderlying
    captured_ns: int
    product_count: int
    ticker_count: int
    records: tuple[DeltaOptionMarketRecord, ...]
    unmatched_product_ids: tuple[int, ...]
    errors: tuple[str, ...]

    @property
    def eligible_records(self) -> tuple[DeltaOptionMarketRecord, ...]:
        return tuple(record for record in self.records if record.eligibility.eligible)


def build_market_snapshot(
    *,
    catalog: DeltaOptionProductsSnapshot,
    chain: DeltaOptionChainSnapshot,
    as_of: date,
    captured_ns: int,
    eligibility_config: EligibilityConfig,
) -> DeltaMarketSnapshot:
    if catalog.underlying != chain.underlying:
        raise ValueError("Catalog and chain underlyings must match")
    if captured_ns <= 0:
        raise ValueError("captured_ns must be positive")

    errors = [
        *(f"product: {error}" for error in catalog.rejected_records),
        *(f"ticker: {error}" for error in chain.rejected_records),
    ]

    products_by_id: dict[int, DeltaOptionProduct] = {}

    for catalog_product in catalog.products:
        if catalog_product.product_id in products_by_id:
            errors.append(f"duplicate product_id: {catalog_product.product_id}")
            continue
        products_by_id[catalog_product.product_id] = catalog_product

    ticker_ids = {ticker.product_id for ticker in chain.tickers}
    unmatched_product_ids = tuple(
        sorted(set(products_by_id) - ticker_ids),
    )

    records: list[DeltaOptionMarketRecord] = []

    for ticker in chain.tickers:
        matched_product = products_by_id.get(ticker.product_id)

        if matched_product is None:
            errors.append(
                f"{ticker.symbol}: missing product {ticker.product_id}",
            )
            continue

        try:
            _validate_product_ticker_pair(matched_product, ticker)

            instrument = map_delta_product_to_crypto_option(
                matched_product,
                ts_event_ns=captured_ns,
                ts_init_ns=captured_ns,
            )
            quote = map_delta_ticker_to_quote_tick(
                ticker,
                instrument,
                ts_init_ns=captured_ns,
            )
            greeks = map_delta_ticker_to_option_greeks(
                ticker,
                instrument,
                ts_init_ns=captured_ns,
            )
            eligibility = evaluate_market_eligibility(
                ticker,
                as_of=as_of,
                config=eligibility_config,
            )
        except (TypeError, ValueError) as exc:
            errors.append(f"{ticker.symbol}: {exc}")
            continue

        records.append(
            DeltaOptionMarketRecord(
                product=matched_product,
                ticker=ticker,
                instrument=instrument,
                quote=quote,
                greeks=greeks,
                eligibility=eligibility,
            ),
        )

    return DeltaMarketSnapshot(
        underlying=chain.underlying,
        captured_ns=captured_ns,
        product_count=len(catalog.products),
        ticker_count=len(chain.tickers),
        records=tuple(records),
        unmatched_product_ids=unmatched_product_ids,
        errors=tuple(errors),
    )


def _validate_product_ticker_pair(
    product: DeltaOptionProduct,
    ticker: DeltaOptionTicker,
) -> None:
    checks = (
        (product.product_id == ticker.product_id, "product_id mismatch"),
        (product.symbol == ticker.symbol, "symbol mismatch"),
        (product.underlying == ticker.underlying, "underlying mismatch"),
        (
            product.contract_type == ticker.contract_type,
            "contract_type mismatch",
        ),
        (product.strike_price == ticker.strike_price, "strike_price mismatch"),
        (
            product.contract_value == ticker.contract_value,
            "contract_value mismatch",
        ),
        (product.tick_size == ticker.tick_size, "tick_size mismatch"),
    )

    for matches, message in checks:
        if not matches:
            raise ValueError(message)
