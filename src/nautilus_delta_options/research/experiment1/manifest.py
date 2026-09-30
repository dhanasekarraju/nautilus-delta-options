from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict

from nautilus_delta_options.research.experiment1.models import (
    EvaluationSlot,
    ExperimentManifest,
)

_NS_PER_SECOND = 1_000_000_000
_RESOLUTION_NS = 300 * _NS_PER_SECOND
_HORIZON_NS = 1800 * _NS_PER_SECOND

_SOURCE_SYMBOLS = {
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
}


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_schedule(
    *,
    experiment_id: str,
    window_start_utc_ns: int,
    window_end_utc_ns: int,
) -> tuple[EvaluationSlot, ...]:
    if window_start_utc_ns % _RESOLUTION_NS != 0:
        raise ValueError("window start must align to a five-minute boundary")
    if window_end_utc_ns % _RESOLUTION_NS != 0:
        raise ValueError("window end must align to a five-minute boundary")
    if window_end_utc_ns <= window_start_utc_ns:
        raise ValueError("window end must follow start")

    slots: list[EvaluationSlot] = []

    close_boundary_ns = window_start_utc_ns

    while close_boundary_ns < window_end_utc_ns:
        open_ns = close_boundary_ns - _RESOLUTION_NS

        for asset in ("BTC", "ETH"):
            slot_key = (
                f"{experiment_id}:"
                f"{asset}:"
                f"{close_boundary_ns}"
            )

            slots.append(
                EvaluationSlot(
                    slot_id=sha256_text(slot_key),
                    experiment_id=experiment_id,
                    asset=asset,
                    source_symbol=_SOURCE_SYMBOLS[asset],
                    candle_open_ns=open_ns,
                    candle_close_boundary_ns=close_boundary_ns,
                    scheduled_evaluation_boundary_ns=close_boundary_ns,
                    endpoint_close_boundary_ns=(
                        close_boundary_ns + _HORIZON_NS
                    ),
                )
            )

        close_boundary_ns += _RESOLUTION_NS

    return tuple(slots)


def schedule_sha256(slots: Iterable[EvaluationSlot]) -> str:
    payload = [
        asdict(slot)
        for slot in slots
    ]
    return sha256_text(canonical_json(payload))


def manifest_payload(
    manifest: ExperimentManifest,
    *,
    schedule_hash: str,
) -> dict[str, object]:
    payload = asdict(manifest)

    payload["assets_json"] = canonical_json(["BTC", "ETH"])
    payload["source_symbols_json"] = canonical_json(_SOURCE_SYMBOLS)
    payload["schedule_sha256"] = schedule_hash

    return payload


def manifest_sha256(
    manifest: ExperimentManifest,
    *,
    schedule_hash: str,
) -> str:
    return sha256_text(
        canonical_json(
            manifest_payload(
                manifest,
                schedule_hash=schedule_hash,
            )
        )
    )
