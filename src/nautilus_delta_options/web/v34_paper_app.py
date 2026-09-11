from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from nautilus_delta_options.delta.history import (
    DeltaHistoryClient,
)
from nautilus_delta_options.delta.public_client import (
    DeltaPublicClient,
)
from nautilus_delta_options.paper.fast_exit import (
    run_fast_exit_cycle,
)
from nautilus_delta_options.paper.observer import (
    PaperDryRunObserver,
)
from nautilus_delta_options.paper.persistence import (
    SQLitePaperLedgerStore,
)
from nautilus_delta_options.paper.session import (
    PaperLedgerSession,
)
from nautilus_delta_options.paper.v34_paper_live import (
    V34PaperEntryCycle,
    default_v34_paper_eligibility_config,
    default_v34_paper_proposal_config,
    run_v34_paper_entry_cycle,
)
from nautilus_delta_options.paper.v34_shadow import (
    V34ShadowCycle,
    V34ShadowObserver,
    v34_shadow_signal_payload,
)
from nautilus_delta_options.signals.v34 import (
    V34Decision,
)

_LOGGER = logging.getLogger(__name__)

_DASHBOARD_PATH = Path(__file__).with_name(
    "v34_paper_dashboard.html"
)

_TARGET_CLOSED_TRADES = 20


@dataclass(slots=True)
class V34PaperServiceState:
    started_at: str
    updated_at: str | None = None

    latest_cycle: V34ShadowCycle | None = None
    latest_entry_cycle: V34PaperEntryCycle | None = None

    latest_fast_exit_at: str | None = None
    latest_slow_exit_at: str | None = None

    fast_exit_warnings: tuple[str, ...] = ()
    slow_exit_warnings: tuple[str, ...] = ()

    error: str | None = None


def create_v34_paper_app(
    *,
    database: Path | None = None,
    signal_interval_seconds: float | None = None,
    fast_exit_interval_seconds: float | None = None,
    slow_exit_interval_seconds: float | None = None,
    entries_enabled: bool | None = None,
) -> FastAPI:
    resolved_database = database or Path(
        os.getenv(
            "V34_PAPER_DATABASE",
            "/data/v34-paper-live.sqlite",
        ),
    )

    signal_interval = (
        signal_interval_seconds
        if signal_interval_seconds is not None
        else _positive_float_env(
            "V34_SIGNAL_POLL_SECONDS",
            5.0,
        )
    )

    fast_exit_interval = (
        fast_exit_interval_seconds
        if fast_exit_interval_seconds is not None
        else _positive_float_env(
            "V34_FAST_EXIT_SECONDS",
            5.0,
        )
    )

    slow_exit_interval = (
        slow_exit_interval_seconds
        if slow_exit_interval_seconds is not None
        else _positive_float_env(
            "V34_SLOW_EXIT_SECONDS",
            60.0,
        )
    )

    resolved_entries_enabled = (
        entries_enabled
        if entries_enabled is not None
        else _boolean_env(
            "V34_PAPER_ENTRIES_ENABLED",
            False,
        )
    )

    initial_cash = _decimal_env(
        "V34_PAPER_INITIAL_CASH",
        "250",
    )
    minimum_reward_risk = _decimal_env(
        "V34_MINIMUM_REWARD_RISK",
        "1.5",
    )
    max_positions = _positive_int_env(
        "V34_MAX_POSITIONS",
        3,
    )

    store = SQLitePaperLedgerStore(
        resolved_database,
    )
    session = PaperLedgerSession.load_or_create(
        store,
        initial_cash=initial_cash,
        minimum_reward_risk=minimum_reward_risk,
        max_positions=max_positions,
    )

    delta_client = DeltaPublicClient()
    history_client = DeltaHistoryClient()

    proposal_config = (
        default_v34_paper_proposal_config()
    )
    eligibility_config = (
        default_v34_paper_eligibility_config()
    )

    v34_observer = V34ShadowObserver(
        history_client=history_client,
        delta_client=delta_client,
    )

    # Entry authority stays OFF in this observer.
    # It is reused only for the proven slower
    # time-exit / normal-exit lifecycle.
    exit_observer = PaperDryRunObserver(
        client=delta_client,
        session=session,
        eligibility_config=eligibility_config,
        proposal_config=proposal_config,
        signal_client=None,
        entries_enabled=False,
    )

    state = V34PaperServiceState(
        started_at=datetime.now(UTC).isoformat(),
    )

    @asynccontextmanager
    async def lifespan(
        _: FastAPI,
    ) -> AsyncIterator[None]:
        signal_task = asyncio.create_task(
            _poll_v34_signals(
                observer=v34_observer,
                delta_client=delta_client,
                session=session,
                state=state,
                entries_enabled=resolved_entries_enabled,
                interval_seconds=signal_interval,
            ),
        )

        fast_exit_task = asyncio.create_task(
            _poll_fast_exits(
                delta_client=delta_client,
                session=session,
                state=state,
                interval_seconds=fast_exit_interval,
            ),
        )

        slow_exit_task = asyncio.create_task(
            _poll_slow_exits(
                observer=exit_observer,
                state=state,
                interval_seconds=slow_exit_interval,
            ),
        )

        tasks = (
            signal_task,
            fast_exit_task,
            slow_exit_task,
        )

        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

    application = FastAPI(
        title="Nautilus V3.4 Paper Live",
        version="3.4-paper",
        lifespan=lifespan,
    )

    @application.get(
        "/",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def dashboard() -> HTMLResponse:
        return HTMLResponse(
            _DASHBOARD_PATH.read_text(
                encoding="utf-8",
            ),
        )

    @application.get("/api/dashboard")
    def dashboard_data() -> dict[str, object]:
        return _dashboard_payload(
            session=session,
            state=state,
            entries_enabled=resolved_entries_enabled,
        )

    @application.get("/health")
    def health() -> dict[str, object]:
        ledger = session.snapshot()

        return {
            "status": (
                "error"
                if state.error is not None
                else "ready"
            ),
            "mode": "paper_live",
            "strategy": "v3.4",
            "real_orders_enabled": False,
            "paper_entries_enabled": resolved_entries_enabled,
            "database": str(resolved_database),
            "closed_trades": len(
                ledger.closed_trades,
            ),
            "open_positions": len(
                ledger.open_positions,
            ),
            "target_closed_trades": (
                _TARGET_CLOSED_TRADES
            ),
            "sample_complete": (
                len(ledger.closed_trades)
                >= _TARGET_CLOSED_TRADES
            ),
        }

    return application


async def _poll_v34_signals(
    *,
    observer: V34ShadowObserver,
    delta_client: DeltaPublicClient,
    session: PaperLedgerSession,
    state: V34PaperServiceState,
    entries_enabled: bool,
    interval_seconds: float,
) -> None:
    while True:
        try:
            cycle = await asyncio.to_thread(
                observer.run_cycle,
            )

            state.updated_at = (
                datetime.now(UTC).isoformat()
            )
            state.error = None

            # Keep the last genuine completed-candle
            # evaluation visible on the dashboard.
            # Between 5m boundaries the observer returns
            # evaluated=False; that must not blank the UI.
            if cycle.evaluated:
                state.latest_cycle = cycle
                ledger = session.snapshot()

                reserved = (
                    len(ledger.closed_trades)
                    + len(ledger.open_positions)
                )

                if (
                    entries_enabled
                    and reserved < _TARGET_CLOSED_TRADES
                ):
                    remaining = (
                        _TARGET_CLOSED_TRADES
                        - reserved
                    )

                    entry_cycle = _limit_cycle_to_budget(
                        cycle,
                        remaining,
                    )

                    state.latest_entry_cycle = (
                        await asyncio.to_thread(
                            run_v34_paper_entry_cycle,
                            cycle=entry_cycle,
                            delta_client=delta_client,
                            session=session,
                        )
                    )

                    for result in (
                        state.latest_entry_cycle.results
                    ):
                        if result.position is not None:
                            _LOGGER.info(
                                "V34_PAPER_ENTRY "
                                "trade_id=%s "
                                "underlying=%s "
                                "decision=%s "
                                "symbol=%s",
                                result.position.trade_id,
                                result.signal.underlying,
                                result.signal.decision.value,
                                result.position.symbol,
                            )

                else:
                    state.latest_entry_cycle = None

        except asyncio.CancelledError:
            raise

        except Exception as error:
            state.error = (
                f"V3.4 signal loop: {error}"
            )
            _LOGGER.exception(
                "V34_PAPER_SIGNAL_LOOP_FAILED",
            )

        await asyncio.sleep(interval_seconds)


async def _poll_fast_exits(
    *,
    delta_client: DeltaPublicClient,
    session: PaperLedgerSession,
    state: V34PaperServiceState,
    interval_seconds: float,
) -> None:
    while True:
        try:
            cycle = await asyncio.to_thread(
                run_fast_exit_cycle,
                client=delta_client,
                session=session,
            )

            state.latest_fast_exit_at = (
                datetime.now(UTC).isoformat()
            )
            state.fast_exit_warnings = (
                cycle.warnings
            )

            for trade in cycle.closed_trades:
                _LOGGER.info(
                    "V34_PAPER_FAST_EXIT "
                    "trade_id=%s "
                    "symbol=%s "
                    "reason=%s "
                    "net_pnl=%s",
                    trade.position.trade_id,
                    trade.position.symbol,
                    trade.reason.value,
                    trade.net_pnl,
                )

        except asyncio.CancelledError:
            raise

        except Exception as error:
            state.fast_exit_warnings = (
                str(error),
            )
            _LOGGER.exception(
                "V34_PAPER_FAST_EXIT_FAILED",
            )

        await asyncio.sleep(interval_seconds)


async def _poll_slow_exits(
    *,
    observer: PaperDryRunObserver,
    state: V34PaperServiceState,
    interval_seconds: float,
) -> None:
    while True:
        try:
            cycle = await asyncio.to_thread(
                observer.run_cycle,
            )

            state.latest_slow_exit_at = (
                datetime.now(UTC).isoformat()
            )
            state.slow_exit_warnings = (
                cycle.warnings
            )

            for trade in cycle.closed_trades:
                _LOGGER.info(
                    "V34_PAPER_SLOW_EXIT "
                    "trade_id=%s "
                    "symbol=%s "
                    "reason=%s "
                    "net_pnl=%s",
                    trade.position.trade_id,
                    trade.position.symbol,
                    trade.reason.value,
                    trade.net_pnl,
                )

        except asyncio.CancelledError:
            raise

        except Exception as error:
            state.slow_exit_warnings = (
                str(error),
            )
            _LOGGER.exception(
                "V34_PAPER_SLOW_EXIT_FAILED",
            )

        await asyncio.sleep(interval_seconds)


def _limit_cycle_to_budget(
    cycle: V34ShadowCycle,
    remaining: int,
) -> V34ShadowCycle:
    """Do not reserve more positions than the 20-trade sample.

    Normally V3.4 can potentially produce one CALL and one PUT
    winner on the same candle. If only one sample slot remains,
    retain the stronger active direction and all BTC/ETH signals
    from that direction so normal same-direction arbitration
    remains intact.
    """

    if remaining >= 2:
        return cycle

    if remaining <= 0:
        return V34ShadowCycle(
            candle_close_ms=cycle.candle_close_ms,
            evaluated=cycle.evaluated,
            signals=tuple(
                signal
                for signal in cycle.signals
                if signal.decision is V34Decision.WAIT
            ),
            warnings=cycle.warnings,
            quality=tuple(
                quality
                for quality in cycle.quality
                if any(
                    signal.underlying
                    == quality.underlying
                    for signal in cycle.signals
                    if signal.decision
                    is V34Decision.WAIT
                )
            ),
        )

    active = tuple(
        signal
        for signal in cycle.signals
        if signal.decision is not V34Decision.WAIT
    )

    directions = {
        signal.decision
        for signal in active
    }

    if len(directions) <= 1:
        return cycle

    def side_score(
        decision: V34Decision,
    ) -> float:
        return max(
            (
                signal.call_score
                if decision is V34Decision.CALL
                else signal.put_score
            )
            for signal in active
            if signal.decision is decision
        )

    chosen = max(
        directions,
        key=side_score,
    )

    kept_signals = tuple(
        signal
        for signal in cycle.signals
        if (
            signal.decision is V34Decision.WAIT
            or signal.decision is chosen
        )
    )

    underlyings = {
        signal.underlying
        for signal in kept_signals
    }

    kept_quality = tuple(
        quality
        for quality in cycle.quality
        if quality.underlying in underlyings
    )

    return V34ShadowCycle(
        candle_close_ms=cycle.candle_close_ms,
        evaluated=cycle.evaluated,
        signals=kept_signals,
        warnings=cycle.warnings,
        quality=kept_quality,
    )


def _dashboard_payload(
    *,
    session: PaperLedgerSession,
    state: V34PaperServiceState,
    entries_enabled: bool,
) -> dict[str, object]:
    ledger = session.snapshot()

    closed = tuple(ledger.closed_trades)
    positions = tuple(ledger.open_positions)

    wins = tuple(
        trade
        for trade in closed
        if trade.net_pnl > 0
    )
    losses = tuple(
        trade
        for trade in closed
        if trade.net_pnl < 0
    )

    realized = sum(
        (trade.net_pnl for trade in closed),
        Decimal("0"),
    )

    gross_win = sum(
        (trade.net_pnl for trade in wins),
        Decimal("0"),
    )

    gross_loss = abs(
        sum(
            (trade.net_pnl for trade in losses),
            Decimal("0"),
        ),
    )

    if gross_loss > 0:
        profit_factor: str | None = str(
            gross_win / gross_loss,
        )
    elif gross_win > 0:
        profit_factor = "Infinity"
    else:
        profit_factor = None

    entry_status_by_underlying: dict[
        str,
        dict[str, object],
    ] = {}

    if state.latest_entry_cycle is not None:
        for result in (
            state.latest_entry_cycle.results
        ):
            entry_status_by_underlying[
                result.signal.underlying
            ] = {
                "status": result.status.value,
                "detail": result.detail,
                "opened_trade_id": (
                    result.position.trade_id
                    if result.position is not None
                    else None
                ),
                "opened_symbol": (
                    result.position.symbol
                    if result.position is not None
                    else None
                ),
            }

    signals: list[dict[str, object]] = []

    if state.latest_cycle is not None:
        quality_by_underlying = {
            row.underlying: row
            for row in state.latest_cycle.quality
        }

        for signal in state.latest_cycle.signals:
            payload = v34_shadow_signal_payload(
                signal,
                quality_by_underlying.get(
                    signal.underlying,
                ),
            )

            payload["paper_entry"] = (
                entry_status_by_underlying.get(
                    signal.underlying,
                )
            )

            signals.append(payload)

    reserved = len(closed) + len(positions)

    return {
        "status": (
            "error"
            if state.error is not None
            else "ready"
        ),
        "mode": "V3.4 PAPER-LIVE",
        "paper_only": True,
        "real_orders_enabled": False,
        "paper_entries_enabled": entries_enabled,
        "error": state.error,
        "started_at": state.started_at,
        "updated_at": state.updated_at,
        "sample": {
            "target": _TARGET_CLOSED_TRADES,
            "closed": len(closed),
            "open": len(positions),
            "reserved": reserved,
            "remaining_to_reserve": max(
                0,
                _TARGET_CLOSED_TRADES - reserved,
            ),
            "complete": (
                len(closed)
                >= _TARGET_CLOSED_TRADES
            ),
            "new_entries_paused": (
                reserved
                >= _TARGET_CLOSED_TRADES
            ),
        },
        "wallet": {
            "initial_cash": str(
                ledger.initial_cash,
            ),
            "available_cash": str(
                ledger.cash,
            ),
            "realized_pnl": str(realized),
            "minimum_reward_risk": str(
                ledger.minimum_reward_risk,
            ),
            "max_positions": ledger.max_positions,
        },
        "performance": {
            "closed_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": (
                len(closed)
                - len(wins)
                - len(losses)
            ),
            "win_rate": (
                (len(wins) / len(closed) * 100)
                if closed
                else None
            ),
            "profit_factor": profit_factor,
            "net_pnl": str(realized),
        },
        "signals": signals,
        "open_positions": [
            _position_payload(position)
            for position in positions
        ],
        "closed_trades": [
            _closed_trade_payload(trade)
            for trade in reversed(closed[-20:])
        ],
        "warnings": {
            "shadow": (
                list(state.latest_cycle.warnings)
                if state.latest_cycle is not None
                else []
            ),
            "fast_exit": list(
                state.fast_exit_warnings,
            ),
            "slow_exit": list(
                state.slow_exit_warnings,
            ),
        },
        "loops": {
            "fast_exit_at": (
                state.latest_fast_exit_at
            ),
            "slow_exit_at": (
                state.latest_slow_exit_at
            ),
        },
    }


def _position_payload(
    position: object,
) -> dict[str, object]:
    from nautilus_delta_options.paper.ledger import (
        PaperPosition,
    )

    if not isinstance(position, PaperPosition):
        raise TypeError("Expected PaperPosition")

    return {
        "trade_id": position.trade_id,
        "underlying": position.underlying,
        "contract_type": position.contract_type,
        "symbol": position.symbol,
        "contracts": str(position.contracts),
        "entry_price": str(position.entry_price),
        "entry_spot": str(position.entry_spot),
        "entry_debit": str(position.entry_debit),
        "stop_price": str(position.stop_price),
        "target_price": str(position.target_price),
        "stop_spot": str(position.stop_spot),
        "target_spot": str(position.target_spot),
        "reward_risk": str(
            position.planned_reward_risk,
        ),
        "opened_ns": position.opened_ns,
    }


def _closed_trade_payload(
    trade: object,
) -> dict[str, object]:
    from nautilus_delta_options.paper.ledger import (
        PaperClosedTrade,
    )

    if not isinstance(trade, PaperClosedTrade):
        raise TypeError("Expected PaperClosedTrade")

    return {
        "trade_id": trade.position.trade_id,
        "underlying": trade.position.underlying,
        "contract_type": (
            trade.position.contract_type
        ),
        "symbol": trade.position.symbol,
        "entry_price": str(
            trade.position.entry_price,
        ),
        "exit_price": str(trade.exit_price),
        "net_pnl": str(trade.net_pnl),
        "gross_pnl": str(trade.gross_pnl),
        "reason": trade.reason.value,
        "opened_ns": trade.position.opened_ns,
        "closed_ns": trade.closed_ns,
    }


def _boolean_env(
    name: str,
    default: bool,
) -> bool:
    raw = os.getenv(name)

    if raw is None:
        return default

    value = raw.strip().lower()

    if value in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True

    if value in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False

    raise ValueError(
        f"{name} must be a boolean",
    )


def _positive_float_env(
    name: str,
    default: float,
) -> float:
    value = float(
        os.getenv(name, str(default)),
    )

    if value <= 0:
        raise ValueError(
            f"{name} must be positive",
        )

    return value


def _positive_int_env(
    name: str,
    default: int,
) -> int:
    value = int(
        os.getenv(name, str(default)),
    )

    if value <= 0:
        raise ValueError(
            f"{name} must be positive",
        )

    return value


def _decimal_env(
    name: str,
    default: str,
) -> Decimal:
    value = Decimal(
        os.getenv(name, default),
    )

    if not value.is_finite():
        raise ValueError(
            f"{name} must be finite",
        )

    return value


app = create_v34_paper_app()
