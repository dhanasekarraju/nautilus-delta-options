import json
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.product import DeltaOptionProduct

type DeltaUnderlying = Literal["BTC", "ETH"]


@dataclass(frozen=True, slots=True)
class DeltaOptionChainSnapshot:
    underlying: DeltaUnderlying
    tickers: tuple[DeltaOptionTicker, ...]
    rejected_records: tuple[str, ...]

    @property
    def tradeable_tickers(self) -> tuple[DeltaOptionTicker, ...]:
        return tuple(ticker for ticker in self.tickers if ticker.has_tradeable_quote)


@dataclass(frozen=True, slots=True)
class DeltaOptionProductsSnapshot:
    underlying: DeltaUnderlying
    products: tuple[DeltaOptionProduct, ...]
    rejected_records: tuple[str, ...]


class DeltaPublicClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.india.delta.exchange",
        timeout_seconds: float = 20.0,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("base_url must use HTTPS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def fetch_option_chain(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionChainSnapshot:
        payload = self._fetch_options_payload(
            path="/v2/tickers",
            underlying=underlying,
        )
        return parse_option_chain_payload(payload, underlying=underlying)

    def fetch_option_products(
        self,
        underlying: DeltaUnderlying,
    ) -> DeltaOptionProductsSnapshot:
        products: list[DeltaOptionProduct] = []
        rejected: list[str] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(100):
            payload = self._fetch_options_payload(
                path="/v2/products", underlying=underlying, after=cursor,
            )
            page = parse_option_products_payload(payload, underlying=underlying)
            products.extend(page.products)
            rejected.extend(page.rejected_records)
            response = cast(Mapping[str, object], payload)
            meta = response.get("meta")
            after = meta.get("after") if isinstance(meta, Mapping) else None
            if after is None:
                return DeltaOptionProductsSnapshot(underlying, tuple(products), tuple(rejected))
            if not isinstance(after, str) or not after or after in seen:
                raise ValueError("Invalid or repeated product pagination cursor")
            seen.add(after)
            cursor = after
        raise ValueError("Product pagination exceeded safety limit")

    def fetch_option_tickers(
        self,
        symbols: Sequence[str],
    ) -> tuple[DeltaOptionTicker, ...]:
        if not symbols:
            raise ValueError("At least one option symbol is required")
        if len(symbols) > 10:
            raise ValueError("At most 10 option symbols may be requested")

        normalized_symbols: list[str] = []

        for symbol in symbols:
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("Option symbols must be non-empty strings")

            normalized = symbol.strip()
            if "," in normalized:
                raise ValueError("Option symbols must not contain commas")

            normalized_symbols.append(normalized)

        if len(normalized_symbols) != len(set(normalized_symbols)):
            raise ValueError("Option symbols must be unique")

        encoded_symbols = urllib.parse.quote(
            ",".join(normalized_symbols),
            safe=",",
        )
        url = f"{self._base_url}/v2/tickers/{encoded_symbols}"

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "nautilus-delta-options/0.1",
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=self._timeout_seconds,
        ) as response:
            payload = cast(object, json.load(response))

        return parse_option_tickers_payload(
            payload,
            expected_symbols=normalized_symbols,
        )

    def _fetch_options_payload(
        self,
        *,
        path: str,
        underlying: DeltaUnderlying,
        after: str | None = None,
    ) -> object:
        params = urllib.parse.urlencode(
            {
                "contract_types": "call_options,put_options",
                "underlying_asset_symbols": underlying,
            },
        )
        if after is not None:
            params += "&" + urllib.parse.urlencode({"after": after})
        url = f"{self._base_url}{path}?{params}"

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "nautilus-delta-options/0.1",
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=self._timeout_seconds,
        ) as response:
            return cast(object, json.load(response))


def parse_option_chain_payload(
    payload: object,
    *,
    underlying: DeltaUnderlying,
) -> DeltaOptionChainSnapshot:
    raw_records = _response_records(payload)

    tickers: list[DeltaOptionTicker] = []
    rejected_records: list[str] = []

    for index, raw_record in enumerate(raw_records):
        if not isinstance(raw_record, Mapping):
            rejected_records.append(f"record {index}: must be an object")
            continue

        try:
            ticker = DeltaOptionTicker.from_api(
                cast(Mapping[str, object], raw_record),
            )
            if ticker.underlying != underlying:
                raise ValueError(
                    f"expected underlying {underlying}, got {ticker.underlying}",
                )
        except ValueError as exc:
            rejected_records.append(f"record {index}: {exc}")
            continue

        tickers.append(ticker)

    return DeltaOptionChainSnapshot(
        underlying=underlying,
        tickers=tuple(tickers),
        rejected_records=tuple(rejected_records),
    )


def parse_option_products_payload(
    payload: object,
    *,
    underlying: DeltaUnderlying,
) -> DeltaOptionProductsSnapshot:
    raw_records = _response_records(payload)

    products: list[DeltaOptionProduct] = []
    rejected_records: list[str] = []

    for index, raw_record in enumerate(raw_records):
        if not isinstance(raw_record, Mapping):
            rejected_records.append(f"record {index}: must be an object")
            continue

        try:
            product = DeltaOptionProduct.from_api(
                cast(Mapping[str, object], raw_record),
            )
            if product.underlying != underlying:
                raise ValueError(
                    f"expected underlying {underlying}, got {product.underlying}",
                )
        except ValueError as exc:
            rejected_records.append(f"record {index}: {exc}")
            continue

        products.append(product)

    return DeltaOptionProductsSnapshot(
        underlying=underlying,
        products=tuple(products),
        rejected_records=tuple(rejected_records),
    )


def parse_option_tickers_payload(
    payload: object,
    *,
    expected_symbols: Sequence[str],
) -> tuple[DeltaOptionTicker, ...]:
    if not expected_symbols:
        raise ValueError("At least one expected symbol is required")

    if not isinstance(payload, Mapping):
        raise ValueError("Delta response must be an object")

    response = cast(Mapping[str, object], payload)

    if response.get("success") is not True:
        raise ValueError("Delta response indicated failure")

    raw_result = response.get("result")

    if isinstance(raw_result, Mapping):
        raw_records: list[object] = [raw_result]
    elif isinstance(raw_result, list):
        raw_records = cast(list[object], raw_result)
    else:
        raise ValueError(
            "Delta ticker response result must be an object or list"
        )

    tickers: list[DeltaOptionTicker] = []

    for index, raw_record in enumerate(raw_records):
        if not isinstance(raw_record, Mapping):
            raise ValueError(
                f"ticker record {index}: must be an object"
            )

        tickers.append(
            DeltaOptionTicker.from_api(
                cast(Mapping[str, object], raw_record),
            ),
        )

    tickers_by_symbol = {
        ticker.symbol: ticker
        for ticker in tickers
    }

    if len(tickers_by_symbol) != len(tickers):
        raise ValueError(
            "Delta ticker response contains duplicate symbols"
        )

    expected = tuple(expected_symbols)
    expected_set = set(expected)
    actual_set = set(tickers_by_symbol)

    if actual_set != expected_set:
        missing = sorted(expected_set - actual_set)
        unexpected = sorted(actual_set - expected_set)

        raise ValueError(
            "Delta ticker response symbols do not match request: "
            f"missing={missing}, unexpected={unexpected}"
        )

    return tuple(
        tickers_by_symbol[symbol]
        for symbol in expected
    )


def _response_records(payload: object) -> list[object]:
    if not isinstance(payload, Mapping):
        raise ValueError("Delta response must be an object")

    response = cast(Mapping[str, object], payload)

    if response.get("success") is not True:
        raise ValueError("Delta response indicated failure")

    raw_result = response.get("result")
    if not isinstance(raw_result, list):
        raise ValueError("Delta response result must be a list")

    return cast(list[object], raw_result)
