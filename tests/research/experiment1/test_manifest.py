from nautilus_delta_options.research.experiment1.manifest import (
    build_schedule,
    manifest_sha256,
    schedule_sha256,
)
from nautilus_delta_options.research.experiment1.models import (
    ExperimentManifest,
)

FIVE_MINUTES_NS = 300_000_000_000


def test_schedule_has_both_assets_for_every_candle() -> None:
    slots = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=0,
        window_end_utc_ns=3 * FIVE_MINUTES_NS,
    )

    assert len(slots) == 6

    assert [
        (
            slot.asset,
            slot.candle_close_boundary_ns,
        )
        for slot in slots
    ] == [
        ("BTC", FIVE_MINUTES_NS),
        ("ETH", FIVE_MINUTES_NS),
        ("BTC", 2 * FIVE_MINUTES_NS),
        ("ETH", 2 * FIVE_MINUTES_NS),
        ("BTC", 3 * FIVE_MINUTES_NS),
        ("ETH", 3 * FIVE_MINUTES_NS),
    ]


def test_schedule_uses_exact_thirty_minute_endpoint() -> None:
    slots = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=0,
        window_end_utc_ns=FIVE_MINUTES_NS,
    )

    for slot in slots:
        assert (
            slot.endpoint_close_boundary_ns
            - slot.candle_close_boundary_ns
            == 1_800_000_000_000
        )


def test_schedule_hash_is_deterministic() -> None:
    first = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=0,
        window_end_utc_ns=2 * FIVE_MINUTES_NS,
    )
    second = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=0,
        window_end_utc_ns=2 * FIVE_MINUTES_NS,
    )

    assert schedule_sha256(first) == schedule_sha256(second)


def test_manifest_hash_changes_with_schedule() -> None:
    manifest = ExperimentManifest(
        experiment_id="exp-1",
        protocol_version="1",
        protocol_sha256="protocol-sha",
        window_start_utc_ns=0,
        window_end_utc_ns=2 * FIVE_MINUTES_NS,
        endpoint_collection_end_utc_ns=(
            2 * FIVE_MINUTES_NS
            + 1_800_000_000_000
        ),
        signal_engine_source_sha256="signal-sha",
        collector_build_id="collector-build",
        collector_source_sha256="collector-sha",
        effective_config_json="{}",
        config_sha256="config-sha",
        runtime_identity_json="{}",
        population_rules_json="{}",
        missing_data_rules_json="{}",
        endpoint_rules_json="{}",
        clustering_rules_json="{}",
        failure_rules_json="{}",
        created_at_utc_ns=1,
    )

    schedule_a = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=0,
        window_end_utc_ns=FIVE_MINUTES_NS,
    )
    schedule_b = build_schedule(
        experiment_id="exp-1",
        window_start_utc_ns=0,
        window_end_utc_ns=2 * FIVE_MINUTES_NS,
    )

    hash_a = manifest_sha256(
        manifest,
        schedule_hash=schedule_sha256(schedule_a),
    )
    hash_b = manifest_sha256(
        manifest,
        schedule_hash=schedule_sha256(schedule_b),
    )

    assert hash_a != hash_b
