import time
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from nautilus_delta_options.delta.models import DeltaOptionTicker
from nautilus_delta_options.delta.snapshot import DeltaOptionMarketRecord
from nautilus_delta_options.payoff.fees import (
    OptionFeeSchedule,
    calculate_option_trade_fee,
)
from nautilus_delta_options.payoff.long_option import (
    evaluate_long_option_payoff_plan,
)


class ExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    TIME = "time"
    MANUAL = "manual"
    SETTLEMENT = "settlement"


@dataclass(frozen=True, slots=True)
class PaperPosition:
    trade_id: int
    product_id: int
    symbol: str
    underlying: str
    contract_type: str
    contracts: Decimal
    contract_value: Decimal
    entry_price: Decimal
    entry_spot: Decimal
    entry_premium: Decimal
    entry_fee: Decimal
    entry_debit: Decimal
    stop_price: Decimal
    target_price: Decimal
    planned_reward_risk: Decimal
    opened_ns: int
    stop_spot: Decimal = Decimal("0")
    target_spot: Decimal = Decimal("0")
    planned_loss: Decimal = Decimal("0")
    planned_reward: Decimal = Decimal("0")
    taker_fee: Decimal = Decimal("0.0001")
    premium_cap_rate: Decimal = Decimal("0.035")
    last_quote_ns: int = 0
    last_bid: Decimal | None = None
    last_exit_fee: Decimal | None = None
    settlement_ns: int = 0
    strike_price: Decimal = Decimal("0")
    unresolved_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PaperClosedTrade:
    position: PaperPosition
    exit_price: Decimal
    exit_spot: Decimal
    exit_fee: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    reason: ExitReason
    closed_ns: int


class PaperLedger:
    def __init__(
        self,
        *,
        initial_cash: Decimal,
        minimum_reward_risk: Decimal,
        max_positions: int,
        gst_rate: Decimal | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if minimum_reward_risk <= 0:
            raise ValueError("minimum_reward_risk must be positive")
        if max_positions <= 0:
            raise ValueError("max_positions must be positive")

        if gst_rate is None:
            gst_rate = Decimal("0.18")
        if gst_rate < 0:
            raise ValueError("gst_rate cannot be negative")

        self._initial_cash = initial_cash
        self._cash = initial_cash
        self._minimum_reward_risk = minimum_reward_risk
        self._max_positions = max_positions
        self._gst_rate = gst_rate
        self._next_trade_id = 1
        self._positions: dict[int, PaperPosition] = {}
        self._closed_trades: list[PaperClosedTrade] = []

    @property
    def initial_cash(self) -> Decimal:
        return self._initial_cash

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def open_positions(self) -> tuple[PaperPosition, ...]:
        return tuple(self._positions.values())

    @property
    def closed_trades(self) -> tuple[PaperClosedTrade, ...]:
        return tuple(self._closed_trades)

    @property
    def realized_pnl(self) -> Decimal:
        return sum(
            (trade.net_pnl for trade in self._closed_trades),
            Decimal("0"),
        )

    @property
    def minimum_reward_risk(self) -> Decimal:
        return self._minimum_reward_risk

    @property
    def max_positions(self) -> int:
        return self._max_positions

    @property
    def gst_rate(self) -> Decimal:
        return self._gst_rate

    @property
    def next_trade_id(self) -> int:
        return self._next_trade_id

    @classmethod
    def from_state(
        cls,
        *,
        initial_cash: Decimal,
        cash: Decimal,
        minimum_reward_risk: Decimal,
        max_positions: int,
        gst_rate: Decimal,
        next_trade_id: int,
        open_positions: tuple[PaperPosition, ...],
        closed_trades: tuple[PaperClosedTrade, ...],
    ) -> "PaperLedger":
        if cash < 0:
            raise ValueError("cash cannot be negative")
        if next_trade_id <= 0:
            raise ValueError("next_trade_id must be positive")
        if len(open_positions) > max_positions:
            raise ValueError("Open positions exceed max_positions")

        trade_ids = [position.trade_id for position in open_positions] + [
            trade.position.trade_id for trade in closed_trades
        ]

        if any(trade_id <= 0 for trade_id in trade_ids):
            raise ValueError("Trade IDs must be positive")
        if len(trade_ids) != len(set(trade_ids)):
            raise ValueError("Trade IDs must be unique")
        if next_trade_id <= max(trade_ids, default=0):
            raise ValueError("next_trade_id must exceed existing trade IDs")

        ledger = cls(
            initial_cash=initial_cash,
            minimum_reward_risk=minimum_reward_risk,
            max_positions=max_positions,
            gst_rate=gst_rate,
        )
        ledger._cash = cash
        ledger._next_trade_id = next_trade_id
        ledger._positions = {position.trade_id: position for position in open_positions}
        ledger._closed_trades = list(closed_trades)
        return ledger

    def open_long(
        self,
        record: DeltaOptionMarketRecord,
        *,
        contracts: Decimal,
        stop_exit_bid: Decimal,
        target_exit_bid: Decimal,
        stop_spot: Decimal,
        target_spot: Decimal,
    ) -> PaperPosition:
        if not record.eligibility.eligible:
            raise ValueError("Market record is not eligible")
        if record.quote is None:
            raise ValueError("Market record has no executable quote")
        if contracts <= 0 or contracts != contracts.to_integral_value():
            raise ValueError("contracts must be a positive whole number")
        if len(self._positions) >= self._max_positions:
            raise ValueError("Maximum open positions reached")
        if any(
            position.product_id == record.product.product_id
            for position in self._positions.values()
        ):
            raise ValueError("A position already exists for this contract")

        ticker = record.ticker

        if ticker.best_ask is None or ticker.ask_size is None:
            raise ValueError("Ticker is missing executable ask data")
        if contracts > ticker.ask_size:
            raise ValueError("contracts exceed available ask depth")
        if contracts > record.product.position_size_limit:
            raise ValueError("contracts exceed product position limit")
        if not stop_exit_bid < ticker.best_ask < target_exit_bid:
            raise ValueError("Expected stop < entry ask < target")
        if stop_exit_bid % record.product.tick_size != 0:
            raise ValueError("stop price is not aligned to tick size")
        if target_exit_bid % record.product.tick_size != 0:
            raise ValueError("target price is not aligned to tick size")

        fee_schedule = self._fee_schedule(record)
        payoff = evaluate_long_option_payoff_plan(
            entry_ask=ticker.best_ask,
            stop_exit_bid=stop_exit_bid,
            target_exit_bid=target_exit_bid,
            entry_spot=ticker.spot_price,
            stop_spot=stop_spot,
            target_spot=target_spot,
            contracts=contracts,
            contract_value=ticker.contract_value,
            minimum_reward_risk=self._minimum_reward_risk,
            fee_schedule=fee_schedule,
        )

        if not payoff.approved:
            raise ValueError(
                "Trade rejected by payoff gate: "
                f"{payoff.reward_risk_ratio} < "
                f"{self._minimum_reward_risk}",
            )

        underlying_quantity = contracts * ticker.contract_value
        entry_premium = ticker.best_ask * underlying_quantity
        entry_fee = payoff.stop_outcome.entry_fee.total_fee
        entry_debit = entry_premium + entry_fee

        if entry_debit > self._cash:
            raise ValueError("Insufficient virtual cash")

        position = PaperPosition(
            trade_id=self._next_trade_id,
            product_id=record.product.product_id,
            symbol=ticker.symbol,
            underlying=ticker.underlying,
            contract_type=ticker.contract_type,
            contracts=contracts,
            contract_value=ticker.contract_value,
            entry_price=ticker.best_ask,
            entry_spot=ticker.spot_price,
            entry_premium=entry_premium,
            entry_fee=entry_fee,
            entry_debit=entry_debit,
            stop_price=stop_exit_bid,
            target_price=target_exit_bid,
            planned_reward_risk=payoff.reward_risk_ratio,
            opened_ns=record.quote.ts_init,
            last_quote_ns=record.quote.ts_event,
            last_bid=ticker.best_bid,
            last_exit_fee=calculate_option_trade_fee(
                spot_price=ticker.spot_price,
                option_price=ticker.best_bid or Decimal("0"),
                contracts=contracts,
                contract_value=ticker.contract_value,
                schedule=fee_schedule,
            ).total_fee,
            settlement_ns=int(record.product.settlement_time.timestamp() * 1_000_000_000),
            strike_price=ticker.strike_price,
            stop_spot=stop_spot,
            target_spot=target_spot,
            planned_loss=payoff.planned_loss,
            planned_reward=payoff.planned_reward,
            taker_fee=record.product.taker_fee,
            premium_cap_rate=record.product.premium_cap_rate,
        )

        self._cash -= entry_debit
        self._positions[position.trade_id] = position
        self._next_trade_id += 1

        return position

    def process_exit(
        self,
        trade_id: int,
        record: DeltaOptionMarketRecord,
    ) -> PaperClosedTrade | None:
        if record.quote is None:
            return None
        return self.process_exit_ticker(
            trade_id,
            record.ticker,
            observed_ns=record.quote.ts_init,
        )

    def process_exit_ticker(
        self,
        trade_id: int,
        ticker: DeltaOptionTicker,
        *,
        observed_ns: int | None = None,
        apply_time_policy: bool = False,
    ) -> PaperClosedTrade | None:
        position = self._require_position(trade_id)
        observed = time.time_ns() if observed_ns is None else observed_ns
        event_ns = self._validate_exit_ticker(position, ticker, observed_ns=observed)
        executable = (
            ticker.best_bid is not None
            and ticker.best_bid > 0
            and ticker.bid_size is not None
            and ticker.bid_size >= position.contracts
        )
        fee = None
        if executable:
            assert ticker.best_bid is not None
            fee = calculate_option_trade_fee(
                spot_price=ticker.spot_price,
                option_price=ticker.best_bid,
                contracts=position.contracts,
                contract_value=position.contract_value,
                schedule=self._position_fee_schedule(position),
            ).total_fee
        position = replace(
            position,
            last_quote_ns=event_ns,
            last_bid=ticker.best_bid if executable else None,
            last_exit_fee=fee,
            unresolved_reason=None if executable else "insufficient executable bid depth",
        )
        self._positions[trade_id] = position
        if not executable or ticker.best_bid is None:
            raise ValueError("Insufficient available bid depth")
        reason = (
            ExitReason.STOP
            if ticker.best_bid <= position.stop_price
            else ExitReason.TARGET
            if ticker.best_bid >= position.target_price
            else None
        )
        if apply_time_policy:
            from nautilus_delta_options.paper.exit_policy import PaperExitPolicyConfig

            policy = PaperExitPolicyConfig()
            minute = 60_000_000_000
            if (
                observed - position.opened_ns >= policy.max_hold_minutes * minute
                or (position.settlement_ns > 0 and position.settlement_ns - observed
                    <= policy.close_before_settlement_minutes * minute)
            ):
                reason = ExitReason.TIME
        if reason is None:
            return None
        return self._close_long_with_ticker(
            position,
            ticker,
            reason=reason,
            event_ns=observed,
            fee_schedule=self._position_fee_schedule(position),
        )

    def note_unresolved(self, trade_id: int, reason: str) -> None:
        position = self._require_position(trade_id)
        self._positions[trade_id] = replace(
            position,
            last_bid=None,
            last_exit_fee=None,
            unresolved_reason=reason,
        )

    def liquidation_equity(self, observed_ns: int) -> tuple[Decimal, bool]:
        from nautilus_delta_options.paper.quote_safety import validate_quote_time

        value = self.cash
        complete = True
        for position in self.open_positions:
            try:
                validate_quote_time(position.last_quote_ns, observed_ns)
            except ValueError:
                complete = False
                continue
            if position.last_bid is None or position.last_exit_fee is None:
                complete = False
                continue
            value += (
                position.last_bid * position.contracts * position.contract_value
                - position.last_exit_fee
            )
        return value, complete

    def settle_long(
        self,
        trade_id: int,
        *,
        settlement_spot: Decimal,
        settlement_fee: Decimal,
        observed_ns: int,
        reference: str,
    ) -> PaperClosedTrade:
        """Explicit reconciliation using a verified settlement source; never infer from no bid."""
        position = self._require_position(trade_id)
        if not reference.strip():
            raise ValueError("A verified settlement reference is required")
        if not position.settlement_ns or observed_ns < position.settlement_ns:
            raise ValueError("Settlement is not due or legacy settlement metadata is missing")
        if (
            not settlement_spot.is_finite()
            or settlement_spot <= 0
            or not settlement_fee.is_finite()
            or settlement_fee < 0
        ):
            raise ValueError("Invalid settlement price or fee")
        if position.strike_price <= 0:
            raise ValueError("Verified strike is required for settlement")
        intrinsic = max(
            Decimal("0"),
            (settlement_spot - position.strike_price)
            if position.contract_type == "call_options"
            else (position.strike_price - settlement_spot),
        )
        gross = (intrinsic - position.entry_price) * position.contracts * position.contract_value
        trade = PaperClosedTrade(
            position=replace(position, unresolved_reason="settlement reference: " + reference),
            exit_price=intrinsic,
            exit_spot=settlement_spot,
            exit_fee=settlement_fee,
            gross_pnl=gross,
            net_pnl=gross - position.entry_fee - settlement_fee,
            reason=ExitReason.SETTLEMENT,
            closed_ns=observed_ns,
        )
        self._cash += intrinsic * position.contracts * position.contract_value - settlement_fee
        del self._positions[trade_id]
        self._closed_trades.append(trade)
        return trade

    def close_long(
        self,
        trade_id: int,
        record: DeltaOptionMarketRecord,
        *,
        reason: ExitReason = ExitReason.MANUAL,
    ) -> PaperClosedTrade:
        position = self._require_position(trade_id)

        if record.product.product_id != position.product_id:
            raise ValueError("Exit record does not match the position")
        if record.quote is None:
            raise ValueError("Exit record has no executable quote")

        event_ns = self._validate_exit_ticker(
            position,
            record.ticker,
            observed_ns=record.quote.ts_init,
        )

        if record.quote.ts_event != event_ns:
            raise ValueError("Exit quote timestamp does not match ticker timestamp")

        return self._close_long_with_ticker(
            position,
            record.ticker,
            reason=reason,
            event_ns=record.quote.ts_init,
            fee_schedule=self._fee_schedule(record),
        )

    def _close_long_with_ticker(
        self,
        position: PaperPosition,
        ticker: DeltaOptionTicker,
        *,
        reason: ExitReason,
        event_ns: int,
        fee_schedule: OptionFeeSchedule,
    ) -> PaperClosedTrade:
        if ticker.best_bid is None or ticker.bid_size is None:
            raise ValueError("Ticker is missing executable bid data")
        if ticker.best_bid <= 0:
            raise ValueError("Ticker best bid must be positive")
        if ticker.bid_size <= 0:
            raise ValueError("Ticker bid size must be positive")
        if ticker.bid_size < position.contracts:
            raise ValueError("Position exceeds available bid depth")

        if reason == ExitReason.STOP and ticker.best_bid > position.stop_price:
            raise ValueError("STOP reason used before stop was reached")
        if reason == ExitReason.TARGET and ticker.best_bid < position.target_price:
            raise ValueError("TARGET reason used before target was reached")

        fee = calculate_option_trade_fee(
            spot_price=ticker.spot_price,
            option_price=ticker.best_bid,
            contracts=position.contracts,
            contract_value=position.contract_value,
            schedule=fee_schedule,
        )

        underlying_quantity = position.contracts * position.contract_value
        gross_pnl = (ticker.best_bid - position.entry_price) * underlying_quantity
        net_pnl = gross_pnl - position.entry_fee - fee.total_fee
        exit_proceeds = (ticker.best_bid * underlying_quantity) - fee.total_fee

        closed_trade = PaperClosedTrade(
            position=position,
            exit_price=ticker.best_bid,
            exit_spot=ticker.spot_price,
            exit_fee=fee.total_fee,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            reason=reason,
            closed_ns=max(event_ns, position.opened_ns),
        )

        self._cash += exit_proceeds
        del self._positions[position.trade_id]
        self._closed_trades.append(closed_trade)

        return closed_trade

    def _validate_exit_ticker(
        self,
        position: PaperPosition,
        ticker: DeltaOptionTicker,
        *,
        observed_ns: int | None = None,
    ) -> int:
        checks = (
            (
                ticker.product_id == position.product_id,
                "product_id mismatch",
            ),
            (
                ticker.symbol == position.symbol,
                "symbol mismatch",
            ),
            (
                ticker.underlying == position.underlying,
                "underlying mismatch",
            ),
            (
                ticker.contract_type == position.contract_type,
                "contract_type mismatch",
            ),
            (
                ticker.contract_value == position.contract_value,
                "contract_value mismatch",
            ),
        )

        for matches, message in checks:
            if not matches:
                raise ValueError(f"Exit ticker does not match position: {message}")

        if ticker.trading_status != "operational":
            raise ValueError("Exit ticker is not operational")

        if ticker.exchange_timestamp <= 0:
            raise ValueError("Exit ticker timestamp must be positive")

        event_ns = ticker.exchange_timestamp * 1_000

        observed = time.time_ns() if observed_ns is None else observed_ns
        from nautilus_delta_options.paper.quote_safety import validate_quote_time

        validate_quote_time(event_ns, observed)
        if event_ns < position.last_quote_ns:
            raise ValueError("Exit ticker predates the position or latest observation")
        if event_ns < position.opened_ns - 15_000_000_000:
            raise ValueError("Exit ticker predates the position")

        return event_ns

    def _require_position(self, trade_id: int) -> PaperPosition:
        position = self._positions.get(trade_id)
        if position is None:
            raise ValueError(f"Unknown trade_id: {trade_id}")
        return position

    def _fee_schedule(
        self,
        record: DeltaOptionMarketRecord,
    ) -> OptionFeeSchedule:
        return OptionFeeSchedule(
            notional_rate=record.product.taker_fee,
            premium_cap_rate=record.product.premium_cap_rate,
            gst_rate=self._gst_rate,
        )

    def _position_fee_schedule(
        self,
        position: PaperPosition,
    ) -> OptionFeeSchedule:
        return OptionFeeSchedule(
            notional_rate=position.taker_fee,
            premium_cap_rate=position.premium_cap_rate,
            gst_rate=self._gst_rate,
        )
