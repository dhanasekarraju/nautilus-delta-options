import json
import urllib.parse
import urllib.request
from collections.abc import Mapping
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
        payload = self._fetch_options_payload(
            path="/v2/products",
            underlying=underlying,
        )
        return parse_option_products_payload(payload, underlying=underlying)

    def _fetch_options_payload(
        self,
        *,
        path: str,
        underlying: DeltaUnderlying,
    ) -> object:
        params = urllib.parse.urlencode(
            {
                "contract_types": "call_options,put_options",
                "underlying_asset_symbols": underlying,
            },
        )
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
