"""Pure, opt-in payoff research; no directional signal or order authority."""

import json
from dataclasses import asdict, dataclass, replace
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.paper.ledger import PaperPosition
from nautilus_delta_options.paper.quote_safety import validate_quote_time
from nautilus_delta_options.payoff.fees import OptionFeeSchedule, calculate_option_trade_fee

ZERO = Decimal("0")


class PayoffProfile(StrEnum):
    BASELINE = "baseline_v34"
    EXPERIMENTAL = "astra_payoff_experimental"


@dataclass(frozen=True)
class PayoffState:
    profile: PayoffProfile
    stop: Decimal
    target: Decimal | None
    risk: Decimal
    activated: bool = False
    best_net: Decimal | None = None
    mfe: Decimal | None = None
    mae: Decimal | None = None
    last_event_ns: int = 0
    closed: bool = False


def initial_state(
    position: PaperPosition,
    profile: PayoffProfile,
    *,
    gst: Decimal = Decimal("0.18"),
    penalty: Decimal = ZERO,
) -> PayoffState:
    if not position.planned_loss.is_finite() or position.planned_loss <= 0:
        raise ValueError("Research requires positive recorded initial risk")
    initial_net = None
    entry_event_ns = position.last_quote_ns
    if position.entry_observation:
        data = json.loads(position.entry_observation)
        market = data["ticker"]
        entry_event_ns = int(market["exchange_timestamp"]) * 1000
        validate_quote_time(entry_event_ns, data["observed_ns"])
        if market["best_bid"] is not None and market["bid_size"] is not None:
            bid, size = Decimal(market["best_bid"]), Decimal(market["bid_size"])
            spot = Decimal(market["spot_price"])
            if bid > 0 and size >= position.contracts:
                fill = max(ZERO, bid - penalty)
                initial_net = (
                    fill * position.contracts * position.contract_value
                    - _fee(position, fill, spot, gst)
                    - position.entry_debit
                )
    return PayoffState(
        profile=profile,
        stop=position.stop_price,
        target=position.target_price if profile == PayoffProfile.BASELINE else None,
        risk=position.planned_loss,
        last_event_ns=entry_event_ns,
        mfe=initial_net,
        mae=initial_net,
        best_net=initial_net,
    )


def _fee(position: PaperPosition, bid: Decimal, spot: Decimal, gst: Decimal) -> Decimal:
    return calculate_option_trade_fee(
        spot_price=spot,
        option_price=bid,
        contracts=position.contracts,
        contract_value=position.contract_value,
        schedule=OptionFeeSchedule(position.taker_fee, position.premium_cap_rate, gst),
    ).total_fee


def _floor_bid(
    position: PaperPosition,
    floor: Decimal,
    spot: Decimal,
    gst: Decimal,
    penalty: Decimal,
    tick: Decimal,
) -> Decimal:
    q = position.contracts * position.contract_value
    gross_required = position.entry_debit + floor
    fixed_fee = spot * q * position.taker_fee * (1 + gst)
    proportional_fee = q * position.premium_cap_rate * (1 + gst)
    if not 0 <= proportional_fee < q:
        raise ValueError("Fee cap cannot support a net break-even calculation")
    # Proceeds = max(q*b - fixed_fee, (q-proportional_fee)*b).
    bid = min((gross_required + fixed_fee) / q, gross_required / (q - proportional_fee))
    return ((bid + penalty) / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def observe(
    position: PaperPosition,
    state: PayoffState,
    ticker: DeltaOptionTicker,
    *,
    observed_ns: int,
    gst: Decimal = Decimal("0.18"),
    penalty: Decimal = ZERO,
    max_hold_minutes: int = 240,
    settlement_buffer_minutes: int = 120,
) -> tuple[PayoffState, dict[str, object]]:
    """One causal observation. Returned event is audit data, never an order."""
    if not penalty.is_finite() or penalty < 0:
        raise ValueError("Execution penalty must be finite and nonnegative")
    if not gst.is_finite() or gst < 0:
        raise ValueError("GST must be finite and nonnegative")
    event: dict[str, object] = {
        "profile": state.profile.value,
        "trade_id": position.trade_id,
        "symbol": position.symbol,
        "observed_ns": observed_ns,
        "event_ns": ticker.exchange_timestamp * 1000,
        "initial_premium": str(position.entry_price),
        "entry_debit": str(position.entry_debit),
        "entry_fee": str(position.entry_fee),
        "initial_risk": str(state.risk),
        "previous_stop": str(state.stop),
        "stop": str(state.stop),
        "stop_changed": False,
        "mfe": str(state.mfe) if state.mfe is not None else None,
        "mae": str(state.mae) if state.mae is not None else None,
        "activated": state.activated,
        "target": str(state.target) if state.target is not None else None,
        "remaining_dte": str(
            Decimal(position.settlement_ns - observed_ns) / Decimal(86_400_000_000_000)
        ),
        "market": {
            k: str(v) if isinstance(v, Decimal) else v
            for k, v in asdict(ticker).items()
            if k != "expiry"
        },
        "exit_trigger": None,
        "theoretical_exit_price": None,
        "executable_exit_price": None,
        "execution_penalty_per_unit": str(penalty),
        "estimated_exit_fee": None,
        "net_pnl": None,
        "status": "ignored",
    }
    if state.closed:
        return state, event
    try:
        validate_quote_time(ticker.exchange_timestamp * 1000, observed_ns)
        if ticker.exchange_timestamp * 1000 < max(
            state.last_event_ns,
            position.opened_ns - 15_000_000_000,
        ):
            raise ValueError("Out-of-order observation")
        if (
            ticker.product_id != position.product_id
            or ticker.symbol != position.symbol
            or ticker.contract_value != position.contract_value
            or ticker.contract_type != position.contract_type
            or ticker.underlying != position.underlying
        ):
            raise ValueError("Contract identity mismatch")
        if ticker.trading_status != "operational":
            raise ValueError("Market is not operational")
        for value in (
            ticker.best_bid,
            ticker.best_ask,
            ticker.bid_size,
            ticker.ask_size,
            ticker.spot_price,
            ticker.tick_size,
        ):
            if value is not None and not value.is_finite():
                raise ValueError("Nonfinite market value")
        if ticker.spot_price <= 0 or ticker.tick_size <= 0:
            raise ValueError("Invalid spot or tick")
    except ValueError as error:
        event["status"] = str(error)
        return state, event
    if position.settlement_ns <= 0 or observed_ns >= position.settlement_ns:
        event["status"] = "unresolved settlement; verified reconciliation required"
        return state, event
    if (
        ticker.best_bid is None
        or ticker.best_bid <= 0
        or ticker.bid_size is None
        or ticker.bid_size < position.contracts
    ):
        event["status"] = "deferred: insufficient executable bid depth"
        return state, event
    bid = ticker.best_bid
    if ticker.best_ask is not None and bid > ticker.best_ask:
        event["status"] = "crossed quote"
        return state, event
    fill = max(ZERO, bid - penalty)
    fee = _fee(position, fill, ticker.spot_price, gst)
    net = fill * position.contracts * position.contract_value - fee - position.entry_debit
    event.update(
        {
            "status": "observed",
            "executable_exit_price": str(fill),
            "estimated_exit_fee": str(fee),
            "net_pnl": str(net),
            "spread": str(ticker.best_ask - bid) if ticker.best_ask is not None else None,
            "execution_penalty_total": str(penalty * position.contracts * position.contract_value),
        }
    )
    state = replace(
        state,
        mfe=net if state.mfe is None else max(state.mfe, net),
        mae=net if state.mae is None else min(state.mae, net),
        last_event_ns=ticker.exchange_timestamp * 1000,
    )
    minute = 60_000_000_000
    reason = None
    theoretical = None
    if (
        observed_ns - position.opened_ns >= max_hold_minutes * minute
        or position.settlement_ns - observed_ns <= settlement_buffer_minutes * minute
    ):
        reason, theoretical = "time", bid
    elif bid <= state.stop:
        reason, theoretical = "stop", state.stop
    elif state.target is not None and bid >= state.target:
        reason, theoretical = "target", state.target
    if reason is None and state.profile == PayoffProfile.EXPERIMENTAL:
        best = net if state.best_net is None else max(state.best_net, net)
        activated = state.activated or net >= state.risk
        stop = state.stop
        if activated:
            floor = max(ZERO, best - state.risk)
            stop = max(
                stop, _floor_bid(position, floor, ticker.spot_price, gst, penalty, ticker.tick_size)
            )
            if bid <= stop:
                reason, theoretical = "protected_stop", stop
        state = replace(state, activated=activated, best_net=best, stop=stop)
    state = replace(state, closed=reason is not None)
    event.update(
        {
            "stop": str(state.stop),
            "stop_changed": str(state.stop) != event["previous_stop"],
            "activated": state.activated,
            "mfe": str(state.mfe),
            "mae": str(state.mae),
            "best_net": str(state.best_net) if state.best_net is not None else None,
            "exit_trigger": reason,
            "theoretical_exit_price": (str(theoretical) if theoretical is not None else None),
        }
    )
    return state, event
