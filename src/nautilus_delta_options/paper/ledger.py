from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

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
        gst_rate: Decimal | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if minimum_reward_risk <= 0:
            raise ValueError("minimum_reward_risk must be positive")

        if gst_rate is None:
            gst_rate = Decimal("0.18")
        if gst_rate < 0:
            raise ValueError("gst_rate cannot be negative")

        self._initial_cash = initial_cash
        self._cash = initial_cash
        self._minimum_reward_risk = minimum_reward_risk
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
            opened_ns=record.quote.ts_event,
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
        position = self._require_position(trade_id)

        if record.ticker.best_bid is None:
            return None

        if record.ticker.best_bid <= position.stop_price:
            return self.close_long(
                trade_id,
                record,
                reason=ExitReason.STOP,
            )

        if record.ticker.best_bid >= position.target_price:
            return self.close_long(
                trade_id,
                record,
                reason=ExitReason.TARGET,
            )

        return None

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

        ticker = record.ticker

        if ticker.best_bid is None or ticker.bid_size is None:
            raise ValueError("Ticker is missing executable bid data")
        if ticker.bid_size < position.contracts:
            raise ValueError("Position exceeds available bid depth")
        if record.quote.ts_event < position.opened_ns:
            raise ValueError("Exit quote predates the position")

        if reason == ExitReason.STOP and ticker.best_bid > position.stop_price:
            raise ValueError("STOP reason used before stop was reached")
        if reason == ExitReason.TARGET and ticker.best_bid < position.target_price:
            raise ValueError("TARGET reason used before target was reached")

        fee = calculate_option_trade_fee(
            spot_price=ticker.spot_price,
            option_price=ticker.best_bid,
            contracts=position.contracts,
            contract_value=position.contract_value,
            schedule=self._fee_schedule(record),
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
            closed_ns=record.quote.ts_event,
        )

        self._cash += exit_proceeds
        del self._positions[trade_id]
        self._closed_trades.append(closed_trade)

        return closed_trade

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
