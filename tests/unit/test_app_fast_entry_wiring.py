import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import nautilus_delta_options.web.app as app_module
from nautilus_delta_options.paper.fast_entry import (
    PaperFastEntryCycle,
)
from nautilus_delta_options.paper.proposals import (
    PaperProposalConfig,
)


def _route_endpoint(application: Any, path: str) -> Any:
    for route in application.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint

    raise AssertionError(f"Route not found: {path}")


def test_health_proves_fast_entry_is_the_only_entry_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_signal_ages: list[Decimal] = []
    real_proposal_config = PaperProposalConfig

    def capture_proposal_config(
        *args: Any,
        **kwargs: Any,
    ) -> PaperProposalConfig:
        config = real_proposal_config(*args, **kwargs)

        captured_signal_ages.append(
            config.entry_safety.max_signal_age_seconds
        )

        return config

    monkeypatch.setattr(
        app_module,
        "PaperProposalConfig",
        capture_proposal_config,
    )

    application = app_module.create_app(
        database=tmp_path / "paper.sqlite",
        interval_seconds=60,
        entry_interval_seconds=2,
        exit_interval_seconds=5,
        entries_enabled=True,
    )

    health = _route_endpoint(
        application,
        "/health",
    )()

    assert health["entries_enabled"] is True
    assert health["entry_owner"] == "fast_entry"
    assert health["observer_entries_enabled"] is False
    assert health["fast_entry_interval_seconds"] == 2
    assert health["position_exit_interval_seconds"] == 5

    assert captured_signal_ages == [
        Decimal("15"),
    ]


class _StopPolling(Exception):
    pass


def test_fast_entry_poller_preserves_candle_progress_and_entry_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run_fast_entry_cycle(
        **kwargs: Any,
    ) -> PaperFastEntryCycle:
        calls.append(kwargs)

        return PaperFastEntryCycle(
            candle_close_ms=123_456_789,
            signals=(),
            entry_results=(),
            warnings=(),
        )

    sleep_calls = 0

    async def fake_sleep(
        _seconds: float,
    ) -> None:
        nonlocal sleep_calls
        sleep_calls += 1

        if sleep_calls >= 2:
            raise _StopPolling

    monkeypatch.setattr(
        app_module,
        "run_fast_entry_cycle",
        fake_run_fast_entry_cycle,
    )
    monkeypatch.setattr(
        app_module.asyncio,
        "sleep",
        fake_sleep,
    )

    proposal_config = PaperProposalConfig()

    with pytest.raises(_StopPolling):
        asyncio.run(
            app_module._poll_fast_entries(
                client=object(),  # type: ignore[arg-type]
                signal_client=object(),  # type: ignore[arg-type]
                session=object(),  # type: ignore[arg-type]
                entries_enabled=True,
                proposal_config=proposal_config,
                interval_seconds=2,
            )
        )

    assert len(calls) == 2

    assert calls[0]["entries_enabled"] is True
    assert calls[0]["previous_candle_close_ms"] is None

    assert calls[1]["entries_enabled"] is True
    assert (
        calls[1]["previous_candle_close_ms"]
        == 123_456_789
    )
