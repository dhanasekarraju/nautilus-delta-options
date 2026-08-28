from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from nautilus_delta_options.delta.public_client import (
    DeltaPublicClient,
)
from nautilus_delta_options.paper.ledger import PaperLedger
from nautilus_delta_options.paper.observer import (
    PaperDryRunCycle,
    PaperDryRunObserver,
)
from nautilus_delta_options.paper.persistence import (
    SQLitePaperLedgerStore,
)
from nautilus_delta_options.paper.session import PaperLedgerSession
from nautilus_delta_options.signals.binance import BinanceFuturesPublicClient
from nautilus_delta_options.signals.v31 import (
    V31CallSignal,
    evaluate_v31_call_signal,
)

_DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")


class DashboardState:
    def __init__(self) -> None:
        self.payload: dict[str, object] = {
            "status": "loading",
            "entries_enabled": False,
            "updated_at": None,
            "message": "Waiting for the first Delta market cycle.",
        }


def create_app(
    *,
    database: Path | None = None,
    interval_seconds: int | None = None,
) -> FastAPI:
    resolved_database = database or Path(
        os.getenv(
            "PAPER_DATABASE",
            "data/paper-ledger.sqlite",
        )
    )
    resolved_interval = interval_seconds or _integer_env(
        "OBSERVER_INTERVAL_SECONDS",
        60,
    )
    store = SQLitePaperLedgerStore(resolved_database)
    session = PaperLedgerSession.load_or_create(
        store,
        initial_cash=_decimal_env(
            "PAPER_INITIAL_CASH",
            "250",
        ),
        minimum_reward_risk=_decimal_env(
            "MINIMUM_REWARD_RISK",
            "1.5",
        ),
        max_positions=_integer_env(
            "MAX_POSITIONS",
            3,
        ),
    )
    observer = PaperDryRunObserver(
        client=DeltaPublicClient(),
        session=session,
    )
    signal_client = BinanceFuturesPublicClient()
    state = DashboardState()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        polling_task = asyncio.create_task(
            _poll_dashboard(
                observer=observer,
                ledger=session.ledger,
                state=state,
                interval_seconds=resolved_interval,
            )
        )

        try:
            yield
        finally:
            polling_task.cancel()
            with suppress(asyncio.CancelledError):
                await polling_task

    application = FastAPI(
        title="Nautilus Delta Options",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.get(
        "/",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def dashboard() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_PATH.read_text(encoding="utf-8"))

    @application.get("/api/dashboard")
    def dashboard_data() -> dict[str, object]:
        return state.payload

    @application.get("/api/signals")
    def signal_data() -> dict[str, object]:
        try:
            signals = _fetch_v31_signals(signal_client)
        except Exception as error:
            return {
                "status": "error",
                "message": str(error),
                "signals": [],
            }

        return {
            "status": "ready",
            "updated_at": datetime.now(UTC).isoformat(),
            "signals": [_signal_payload(signal) for signal in signals],
        }

    @application.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": state.payload.get(
                "status",
                "unknown",
            ),
            "entries_enabled": False,
        }

    return application


async def _poll_dashboard(
    *,
    observer: PaperDryRunObserver,
    ledger: PaperLedger,
    state: DashboardState,
    interval_seconds: int,
) -> None:
    while True:
        try:
            cycle = await asyncio.to_thread(observer.run_cycle)
            state.payload = _dashboard_payload(
                cycle,
                ledger,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.payload = {
                "status": "error",
                "entries_enabled": False,
                "updated_at": datetime.now(UTC).isoformat(),
                "message": str(error),
            }

        await asyncio.sleep(interval_seconds)


def _dashboard_payload(
    cycle: PaperDryRunCycle,
    ledger: PaperLedger,
) -> dict[str, object]:
    return {
        "status": "ready",
        "entries_enabled": False,
        "updated_at": datetime.now(UTC).isoformat(),
        "cycle_date": cycle.as_of.isoformat(),
        "wallet": {
            "initial_cash": str(ledger.initial_cash),
            "cash": str(ledger.cash),
            "realized_pnl": str(ledger.realized_pnl),
            "open_positions": len(ledger.open_positions),
            "closed_trades": len(ledger.closed_trades),
        },
        "markets": [
            {
                "underlying": snapshot.underlying,
                "products": snapshot.product_count,
                "tickers": snapshot.ticker_count,
                "joined": len(snapshot.records),
                "eligible": sum(record.eligibility.eligible for record in snapshot.records),
                "errors": len(snapshot.errors),
            }
            for snapshot in cycle.snapshots
        ],
        "proposals": [_proposal_payload(proposal) for proposal in cycle.proposals],
        "open_positions": [
            {
                "trade_id": position.trade_id,
                "symbol": position.symbol,
                "side": position.contract_type,
                "contracts": str(position.contracts),
                "entry_price": str(position.entry_price),
                "entry_debit": str(position.entry_debit),
                "stop_price": str(position.stop_price),
                "target_price": str(position.target_price),
                "payoff": str(position.planned_reward_risk),
            }
            for position in ledger.open_positions
        ],
        "recent_trades": [
            {
                "trade_id": trade.position.trade_id,
                "symbol": trade.position.symbol,
                "side": trade.position.contract_type,
                "contracts": str(trade.position.contracts),
                "net_pnl": str(trade.net_pnl),
                "reason": trade.reason.value,
            }
            for trade in ledger.closed_trades[-20:]
        ],
        "cycle_exits": len(cycle.closed_trades),
        "warnings": list(cycle.warnings),
    }


def _proposal_payload(
    proposal: object,
) -> dict[str, object]:
    from nautilus_delta_options.paper.proposals import (
        PaperEntryProposal,
    )

    if not isinstance(proposal, PaperEntryProposal):
        raise TypeError("Expected PaperEntryProposal")

    ticker = proposal.record.ticker
    sizing = proposal.sizing
    levels = proposal.levels
    spread = ticker.spread_fraction

    return {
        "symbol": ticker.symbol,
        "underlying": ticker.underlying,
        "side": ("CALL" if ticker.contract_type == "call_options" else "PUT"),
        "dte": proposal.record.eligibility.dte,
        "bid": str(ticker.best_bid),
        "ask": str(ticker.best_ask),
        "spread_percent": str(spread * Decimal("100") if spread is not None else Decimal("0")),
        "contracts": str(sizing.contracts),
        "entry_debit": str(sizing.total_entry_debit),
        "planned_loss": str(sizing.total_planned_loss),
        "planned_reward": str(sizing.total_planned_reward),
        "reward_risk": str(sizing.reward_risk_ratio),
        "stop_bid": str(levels.stop_exit_bid),
        "target_bid": str(levels.target_exit_bid),
        "sizing_limit": sizing.limiting_factor.value,
    }


def _fetch_v31_signals(
    client: BinanceFuturesPublicClient,
) -> tuple[V31CallSignal, ...]:
    return (
        evaluate_v31_call_signal(client.fetch_v31_candles("BTC")),
        evaluate_v31_call_signal(client.fetch_v31_candles("ETH")),
    )


def _signal_payload(
    signal: V31CallSignal,
) -> dict[str, object]:
    return {
        "underlying": signal.underlying,
        "decision": "CALL" if signal.active else "WAIT",
        "active": signal.active,
        "candle_closed_at": datetime.fromtimestamp(
            signal.candle_close_ms / 1_000,
            tz=UTC,
        ).isoformat(),
        "close": signal.close_price,
        "rsi": signal.rsi,
        "ema20": signal.ema20,
        "ema50": signal.ema50,
        "atr_percent": signal.atr_pct * 100,
        "volume": signal.volume,
        "conditions": {
            "RSI < 35": signal.rsi_below_35,
            "EMA20 > EMA50": signal.ema20_above_ema50,
            "ATR < 3.5%": signal.atr_pct_below_035,
            "Volume > 0": signal.positive_volume,
        },
        "signal_key": signal.signal_key,
    }


def _decimal_env(
    name: str,
    default: str,
) -> Decimal:
    result = Decimal(os.getenv(name, default))

    if not result.is_finite():
        raise ValueError(f"{name} must be finite")

    return result


def _integer_env(
    name: str,
    default: int,
) -> int:
    result = int(os.getenv(name, str(default)))

    if result <= 0:
        raise ValueError(f"{name} must be positive")

    return result


app = create_app()
