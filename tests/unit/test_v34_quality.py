from datetime import date, timedelta
from decimal import Decimal

import pytest

from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.public_client import DeltaOptionChainSnapshot
from nautilus_delta_options.selection.v34_quality import (
    V34QualityConfig,
    rank_v34_contract_quality,
)


AS_OF = date(2026, 9, 10)


def _ticker(
    symbol: str,
    *,
    side: str = "call_options",
    dte: int = 2,
    delta: str = "0.50",
    bid: str = "99",
    ask: str = "100",
    bid_size: str = "20",
    ask_size: str = "20",
    mark_iv: str = "0.45",
    gamma: str = "0.00020",
    theta: str = "-3",
    vega: str = "12",
    oi: str = "100",
    volume: str = "50",
    mark_price: str = "100",
    spot: str = "78000",
) -> DeltaOptionTicker:
    if side == "put_options" and not delta.startswith("-"):
        delta = "-" + delta

    return DeltaOptionTicker(
        product_id=abs(hash(symbol)) % 1_000_000 + 1,
        symbol=symbol,
        underlying="BTC",
        contract_type=side,  # type: ignore[arg-type]
        strike_price=Decimal("78000"),
        expiry=AS_OF + timedelta(days=dte),
        mark_price=Decimal(mark_price),
        spot_price=Decimal(spot),
        contract_value=Decimal("0.001"),
        tick_size=Decimal("0.01"),
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        bid_size=Decimal(bid_size),
        ask_size=Decimal(ask_size),
        mark_iv=Decimal(mark_iv),
        bid_iv=Decimal(mark_iv) - Decimal("0.01"),
        ask_iv=Decimal(mark_iv) + Decimal("0.01"),
        delta=Decimal(delta),
        gamma=Decimal(gamma),
        theta=Decimal(theta),
        rho=Decimal("1"),
        vega=Decimal(vega),
        open_interest_contracts=Decimal(oi),
        volume=Decimal(volume),
        exchange_timestamp=1,
        trading_status="operational",
    )


def _chain(*tickers: DeltaOptionTicker) -> DeltaOptionChainSnapshot:
    return DeltaOptionChainSnapshot(
        underlying="BTC",
        tickers=tickers,
        rejected_records=(),
    )


def test_quality_config_blocks_zero_dte() -> None:
    with pytest.raises(ValueError, match="blocks 0DTE"):
        V34QualityConfig(min_dte=0)


def test_ranker_filters_wrong_side_zero_dte_and_missing_greeks() -> None:
    valid = _ticker("C-VALID")
    wrong_side = _ticker("P-WRONG", side="put_options")
    zero_dte = _ticker("C-0DTE", dte=0)
    missing_gamma = _ticker("C-NOGAMMA")
    missing_gamma = DeltaOptionTicker(
        **{
            field: getattr(missing_gamma, field)
            for field in missing_gamma.__dataclass_fields__
            if field != "gamma"
        },
        gamma=None,
    )

    ranked = rank_v34_contract_quality(
        _chain(valid, wrong_side, zero_dte, missing_gamma),
        contract_type="call_options",
        as_of=AS_OF,
    )

    assert [row.symbol for row in ranked] == ["C-VALID"]


def test_better_buy_side_quality_ranks_first() -> None:
    strong = _ticker(
        "C-STRONG",
        delta="0.50",
        bid="99.5",
        ask="100",
        gamma="0.00028",
        theta="-2",
        vega="14",
        mark_iv="0.42",
        oi="500",
        volume="300",
        bid_size="50",
        ask_size="50",
    )
    weak = _ticker(
        "C-WEAK",
        delta="0.63",
        bid="98",
        ask="100",
        gamma="0.00010",
        theta="-8",
        vega="5",
        mark_iv="0.55",
        oi="10",
        volume="1",
        bid_size="2",
        ask_size="2",
    )

    ranked = rank_v34_contract_quality(
        _chain(weak, strong),
        contract_type="call_options",
        as_of=AS_OF,
    )

    assert ranked[0].symbol == "C-STRONG"
    assert ranked[0].score > ranked[1].score
    assert 0 <= ranked[1].score <= 100
    assert 0 <= ranked[0].score <= 100


def test_dte_has_no_fixed_nearest_expiry_bonus() -> None:
    one_day = _ticker(
        "C-1D",
        dte=1,
        theta="-1",
        vega="15",
        gamma="0.00030",
        mark_iv="0.42",
        oi="300",
        volume="200",
    )
    two_day = _ticker(
        "C-2D",
        dte=2,
        theta="-8",
        vega="4",
        gamma="0.00008",
        mark_iv="0.50",
        oi="30",
        volume="10",
    )

    ranked = rank_v34_contract_quality(
        _chain(two_day, one_day),
        contract_type="call_options",
        as_of=AS_OF,
    )

    assert ranked[0].symbol == "C-1D"
    assert ranked[0].dte == 1


def test_component_score_sums_to_total() -> None:
    rows = rank_v34_contract_quality(
        _chain(
            _ticker("C-A", delta="0.47"),
            _ticker(
                "C-B",
                delta="0.56",
                theta="-4",
                gamma="0.00015",
                mark_iv="0.48",
            ),
        ),
        contract_type="call_options",
        as_of=AS_OF,
    )

    assert rows
    for row in rows:
        parts = row.components
        total = (
            parts.delta_fit
            + parts.spread
            + parts.convexity_efficiency
            + parts.theta
            + parts.vega
            + parts.iv
            + parts.liquidity
        )
        assert row.score == pytest.approx(total)


def test_quality_layer_uses_stricter_buy_side_spread_guard() -> None:
    too_wide = _ticker(
        "C-WIDE",
        bid="98.4",
        ask="100",
    )
    acceptable = _ticker(
        "C-OK",
        bid="98.6",
        ask="100",
    )

    ranked = rank_v34_contract_quality(
        _chain(too_wide, acceptable),
        contract_type="call_options",
        as_of=AS_OF,
    )

    assert [row.symbol for row in ranked] == ["C-OK"]


def test_convexity_is_rewarded_relative_to_theta_burden() -> None:
    high_gamma_heavy_theta = _ticker(
        "C-HIGH-GAMMA-HEAVY-THETA",
        gamma="0.00030",
        theta="-8",
    )
    lower_gamma_better_efficiency = _ticker(
        "C-LOWER-GAMMA-BETTER-EFF",
        gamma="0.00024",
        theta="-3",
    )

    ranked = rank_v34_contract_quality(
        _chain(high_gamma_heavy_theta, lower_gamma_better_efficiency),
        contract_type="call_options",
        as_of=AS_OF,
    )

    by_symbol = {row.symbol: row for row in ranked}
    assert (
        by_symbol["C-LOWER-GAMMA-BETTER-EFF"].convexity_efficiency
        > by_symbol["C-HIGH-GAMMA-HEAVY-THETA"].convexity_efficiency
    )
    assert (
        by_symbol["C-LOWER-GAMMA-BETTER-EFF"].components.convexity_efficiency
        > by_symbol["C-HIGH-GAMMA-HEAVY-THETA"].components.convexity_efficiency
    )


def test_absolute_liquidity_floor_filters_thin_contracts() -> None:
    liquid = _ticker(
        "C-LIQUID",
        oi="500",
        volume="25",
    )
    low_oi = _ticker(
        "C-LOW-OI",
        oi="34",
        volume="25",
        bid="99.8",
        ask="100",
    )
    low_volume = _ticker(
        "C-LOW-VOLUME",
        oi="500",
        volume="0.18",
        bid="99.8",
        ask="100",
    )

    ranked = rank_v34_contract_quality(
        _chain(low_oi, low_volume, liquid),
        contract_type="call_options",
        as_of=AS_OF,
    )

    assert [row.symbol for row in ranked] == ["C-LIQUID"]


def test_liquidity_floors_are_configurable_for_shadow_replay() -> None:
    thin = _ticker(
        "C-THIN",
        oi="34",
        volume="0.18",
    )

    ranked = rank_v34_contract_quality(
        _chain(thin),
        contract_type="call_options",
        as_of=AS_OF,
        config=V34QualityConfig(
            min_open_interest=1,
            min_volume=0,
        ),
    )

    assert [row.symbol for row in ranked] == ["C-THIN"]
