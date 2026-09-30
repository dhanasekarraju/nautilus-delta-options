import pytest

from nautilus_delta_options.research.experiment1.manifest import (
    build_schedule,
    manifest_sha256,
    schedule_sha256,
)
from nautilus_delta_options.research.experiment1.models import (
    EvaluationSlot,
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
        ("BTC", 0),
        ("ETH", 0),
        ("BTC", FIVE_MINUTES_NS),
        ("ETH", FIVE_MINUTES_NS),
        ("BTC", 2 * FIVE_MINUTES_NS),
        ("ETH", 2 * FIVE_MINUTES_NS),
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
        endpoint_acquisition_deadline_utc_ns=(
            2 * FIVE_MINUTES_NS
            + 1_860_000_000_000
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



def test_one_hour_window_is_close_indexed_and_end_exclusive() -> None:
    hour_ns = 12 * FIVE_MINUTES_NS

    slots = build_schedule(
        experiment_id="exp-hour",
        window_start_utc_ns=0,
        window_end_utc_ns=hour_ns,
    )

    btc_slots = [
        slot
        for slot in slots
        if slot.asset == "BTC"
    ]
    eth_slots = [
        slot
        for slot in slots
        if slot.asset == "ETH"
    ]

    assert len(btc_slots) == 12
    assert len(eth_slots) == 12

    expected_closes = [
        index * FIVE_MINUTES_NS
        for index in range(12)
    ]

    assert [
        slot.candle_close_boundary_ns
        for slot in btc_slots
    ] == expected_closes

    assert [
        slot.candle_close_boundary_ns
        for slot in eth_slots
    ] == expected_closes

    first = btc_slots[0]
    last = btc_slots[-1]

    assert first.candle_close_boundary_ns == 0
    assert first.candle_open_ns == -FIVE_MINUTES_NS

    assert last.candle_close_boundary_ns == (
        hour_ns - FIVE_MINUTES_NS
    )

    # The end boundary itself is never a signal slot.
    assert all(
        slot.candle_close_boundary_ns != hour_ns
        for slot in slots
    )


def test_final_endpoint_precedes_conservative_collection_tail() -> None:
    hour_ns = 12 * FIVE_MINUTES_NS

    manifest = ExperimentManifest(
        experiment_id="exp-hour",
        protocol_version="1",
        protocol_sha256="protocol-sha",
        window_start_utc_ns=0,
        window_end_utc_ns=hour_ns,
        endpoint_collection_end_utc_ns=(
            hour_ns + 1_800_000_000_000
        ),
        endpoint_acquisition_deadline_utc_ns=(
            hour_ns + 1_860_000_000_000
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

    slots = build_schedule(
        experiment_id=manifest.experiment_id,
        window_start_utc_ns=manifest.window_start_utc_ns,
        window_end_utc_ns=manifest.window_end_utc_ns,
    )

    final_endpoint = max(
        slot.endpoint_close_boundary_ns
        for slot in slots
    )

    # Last signal close is end-5m, therefore its P30 is end+25m.
    assert final_endpoint == (
        manifest.window_end_utc_ns
        + 1_500_000_000_000
    )

    # Manifest deliberately retains one extra five-minute collection
    # tail: end+30m.
    assert (
        manifest.endpoint_collection_end_utc_ns
        - final_endpoint
        == FIVE_MINUTES_NS
    )


@pytest.mark.parametrize(
    ("asset", "source_symbol"),
    [
        ("BTC", "ETHUSD"),
        ("ETH", "BTCUSD"),
    ],
)
def test_slot_rejects_asset_source_mismatch(
    asset: str,
    source_symbol: str,
) -> None:
    with pytest.raises(
        ValueError,
        match="source_symbol does not match asset",
    ):
        EvaluationSlot(
            slot_id="bad-source",
            experiment_id="exp-1",
            asset=asset,  # type: ignore[arg-type]
            source_symbol=source_symbol,
            candle_open_ns=0,
            candle_close_boundary_ns=FIVE_MINUTES_NS,
            scheduled_evaluation_boundary_ns=FIVE_MINUTES_NS,
            endpoint_close_boundary_ns=(
                FIVE_MINUTES_NS + 1_800_000_000_000
            ),
        )


def test_slot_rejects_non_five_minute_candle() -> None:
    with pytest.raises(
        ValueError,
        match="exactly five minutes",
    ):
        EvaluationSlot(
            slot_id="bad-width",
            experiment_id="exp-1",
            asset="BTC",
            source_symbol="BTCUSD",
            candle_open_ns=0,
            candle_close_boundary_ns=2 * FIVE_MINUTES_NS,
            scheduled_evaluation_boundary_ns=(
                2 * FIVE_MINUTES_NS
            ),
            endpoint_close_boundary_ns=(
                2 * FIVE_MINUTES_NS
                + 1_800_000_000_000
            ),
        )


def test_slot_rejects_misaligned_boundary() -> None:
    with pytest.raises(
        ValueError,
        match="align to five minutes",
    ):
        EvaluationSlot(
            slot_id="bad-alignment",
            experiment_id="exp-1",
            asset="BTC",
            source_symbol="BTCUSD",
            candle_open_ns=1,
            candle_close_boundary_ns=(
                FIVE_MINUTES_NS + 1
            ),
            scheduled_evaluation_boundary_ns=(
                FIVE_MINUTES_NS + 1
            ),
            endpoint_close_boundary_ns=(
                FIVE_MINUTES_NS
                + 1
                + 1_800_000_000_000
            ),
        )


def test_slot_requires_evaluation_at_exact_close() -> None:
    with pytest.raises(
        ValueError,
        match="evaluation boundary must equal",
    ):
        EvaluationSlot(
            slot_id="bad-eval-time",
            experiment_id="exp-1",
            asset="BTC",
            source_symbol="BTCUSD",
            candle_open_ns=0,
            candle_close_boundary_ns=FIVE_MINUTES_NS,
            scheduled_evaluation_boundary_ns=(
                2 * FIVE_MINUTES_NS
            ),
            endpoint_close_boundary_ns=(
                FIVE_MINUTES_NS + 1_800_000_000_000
            ),
        )


def test_manifest_requires_aligned_window() -> None:
    with pytest.raises(
        ValueError,
        match="window must align to five minutes",
    ):
        ExperimentManifest(
            experiment_id="exp-1",
            protocol_version="1",
            protocol_sha256="protocol-sha",
            window_start_utc_ns=1,
            window_end_utc_ns=FIVE_MINUTES_NS + 1,
            endpoint_collection_end_utc_ns=(
                FIVE_MINUTES_NS
                + 1
                + 1_800_000_000_000
            ),
            endpoint_acquisition_deadline_utc_ns=(
                FIVE_MINUTES_NS
                + 1
                + 1_860_000_000_000
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
