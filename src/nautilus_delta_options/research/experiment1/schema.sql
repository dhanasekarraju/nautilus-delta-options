PRAGMA foreign_keys = ON;

CREATE TABLE journal_schema_identity (
    identity_id INTEGER PRIMARY KEY,

    application_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    schema_sql_sha256 TEXT NOT NULL,
    schema_descriptor_sha256 TEXT NOT NULL,
    serialization_version INTEGER NOT NULL,
    created_at_utc_ns INTEGER NOT NULL,

    CHECK (identity_id = 1),
    CHECK (schema_version > 0),
    CHECK (serialization_version > 0),
    CHECK (length(schema_sql_sha256) = 64),
    CHECK (length(schema_descriptor_sha256) = 64)
);

CREATE TABLE experiment_manifest (
    experiment_id TEXT PRIMARY KEY,
    protocol_version TEXT NOT NULL,
    protocol_sha256 TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    schedule_sha256 TEXT NOT NULL,

    window_start_utc_ns INTEGER NOT NULL,
    window_end_utc_ns INTEGER NOT NULL,
    endpoint_collection_end_utc_ns INTEGER NOT NULL,
    endpoint_acquisition_deadline_utc_ns INTEGER NOT NULL,

    assets_json TEXT NOT NULL,
    source_symbols_json TEXT NOT NULL,
    venue TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    resolution_seconds INTEGER NOT NULL,
    timestamp_convention TEXT NOT NULL,
    primary_horizon_seconds INTEGER NOT NULL,

    signal_engine_source_sha256 TEXT NOT NULL,
    collector_build_id TEXT NOT NULL,
    collector_source_sha256 TEXT NOT NULL,

    effective_config_json TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    runtime_identity_json TEXT NOT NULL,

    population_rules_json TEXT NOT NULL,
    missing_data_rules_json TEXT NOT NULL,
    endpoint_rules_json TEXT NOT NULL,
    clustering_rules_json TEXT NOT NULL,
    failure_rules_json TEXT NOT NULL,

    created_at_utc_ns INTEGER NOT NULL,

    CHECK (window_end_utc_ns > window_start_utc_ns),
    CHECK (
        window_start_utc_ns % 300000000000 = 0
    ),
    CHECK (
        window_end_utc_ns % 300000000000 = 0
    ),
    CHECK (
        endpoint_collection_end_utc_ns
        = window_end_utc_ns + 1800000000000
    ),
    CHECK (
        endpoint_acquisition_deadline_utc_ns
        >= endpoint_collection_end_utc_ns
    ),
    CHECK (resolution_seconds = 300),
    CHECK (primary_horizon_seconds = 1800)
);

CREATE TABLE evaluation_slots (
    slot_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,

    asset TEXT NOT NULL,
    source_symbol TEXT NOT NULL,

    candle_open_ns INTEGER NOT NULL,
    candle_close_boundary_ns INTEGER NOT NULL,
    scheduled_evaluation_boundary_ns INTEGER NOT NULL,
    endpoint_close_boundary_ns INTEGER NOT NULL,

    FOREIGN KEY (experiment_id)
        REFERENCES experiment_manifest(experiment_id),

    UNIQUE (
        experiment_id,
        asset,
        candle_close_boundary_ns
    ),

    CHECK (asset IN ('BTC', 'ETH')),
    CHECK (
        (asset = 'BTC' AND source_symbol = 'BTCUSD')
        OR
        (asset = 'ETH' AND source_symbol = 'ETHUSD')
    ),
    CHECK (
        candle_open_ns % 300000000000 = 0
    ),
    CHECK (
        candle_close_boundary_ns % 300000000000 = 0
    ),
    CHECK (
        candle_close_boundary_ns
        = candle_open_ns + 300000000000
    ),
    CHECK (
        scheduled_evaluation_boundary_ns
        = candle_close_boundary_ns
    ),
    CHECK (
        endpoint_close_boundary_ns
        = candle_close_boundary_ns + 1800000000000
    )
);

CREATE TRIGGER evaluation_slots_validate_manifest_window
BEFORE INSERT ON evaluation_slots
WHEN NOT EXISTS (
    SELECT 1
    FROM experiment_manifest
    WHERE experiment_id = NEW.experiment_id
      AND NEW.candle_close_boundary_ns
          >= window_start_utc_ns
      AND NEW.candle_close_boundary_ns
          < window_end_utc_ns
)
BEGIN
    SELECT RAISE(
        ABORT,
        'evaluation slot outside manifest window'
    );
END;

CREATE TABLE journal_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,

    experiment_id TEXT NOT NULL,
    run_id TEXT NOT NULL,

    slot_id TEXT,
    attempt_id TEXT,

    event_type TEXT NOT NULL,

    event_time_utc_ns INTEGER NOT NULL,
    monotonic_elapsed_ns INTEGER,

    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,

    FOREIGN KEY (experiment_id)
        REFERENCES experiment_manifest(experiment_id),

    FOREIGN KEY (slot_id)
        REFERENCES evaluation_slots(slot_id),

    CHECK (
        event_type IN (
            'run_started',
            'observer_cycle_started',
            'observer_cycle_completed',
            'observer_cycle_aborted',
            'evaluation_started',
            'evaluation_completed',
            'evaluation_failed',
            'evaluation_skipped',
            'collection_fault',
            'run_stopped',
            'gap_assessment'
        )
    )
);

CREATE INDEX journal_events_experiment_sequence_idx
    ON journal_events (
        experiment_id,
        sequence
    );

CREATE INDEX journal_events_slot_idx
    ON journal_events (
        experiment_id,
        slot_id,
        sequence
    );

CREATE INDEX journal_events_attempt_idx
    ON journal_events (
        experiment_id,
        attempt_id,
        sequence
    );

CREATE TABLE source_responses (
    response_id TEXT PRIMARY KEY,

    experiment_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    request_id TEXT NOT NULL,

    response_kind TEXT NOT NULL,
    request_url TEXT NOT NULL,

    asset TEXT NOT NULL,
    source_symbol TEXT NOT NULL,

    request_started_utc_ns INTEGER NOT NULL,
    response_received_utc_ns INTEGER NOT NULL,
    request_elapsed_ns INTEGER NOT NULL,
    clock_anomaly INTEGER NOT NULL,

    source_payload_sha256 TEXT NOT NULL,
    raw_response_body BLOB NOT NULL,
    parse_status TEXT NOT NULL,

    FOREIGN KEY (experiment_id)
        REFERENCES experiment_manifest(experiment_id),

    CHECK (
        response_kind IN (
            'history',
            'option_chain'
        )
    ),
    CHECK (length(request_url) > 0),
    CHECK (asset IN ('BTC', 'ETH')),
    CHECK (request_elapsed_ns >= 0),
    CHECK (clock_anomaly IN (0, 1))
);

CREATE INDEX source_responses_lookup_idx
    ON source_responses (
        experiment_id,
        asset,
        response_received_utc_ns
    );

CREATE TABLE candle_observations (
    observation_id TEXT PRIMARY KEY,

    experiment_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    response_id TEXT NOT NULL,

    asset TEXT NOT NULL,
    source_symbol TEXT NOT NULL,

    source_timestamp_raw TEXT NOT NULL,
    normalized_open_ns INTEGER NOT NULL,
    normalized_close_boundary_ns INTEGER NOT NULL,

    open_decimal TEXT NOT NULL,
    high_decimal TEXT NOT NULL,
    low_decimal TEXT NOT NULL,
    close_decimal TEXT NOT NULL,
    volume_decimal TEXT NOT NULL,

    request_started_utc_ns INTEGER NOT NULL,
    response_received_utc_ns INTEGER NOT NULL,
    request_elapsed_ns INTEGER NOT NULL,

    source_payload_sha256 TEXT NOT NULL,
    raw_candle_json TEXT NOT NULL,

    parse_status TEXT NOT NULL,

    FOREIGN KEY (experiment_id)
        REFERENCES experiment_manifest(experiment_id),

    FOREIGN KEY (response_id)
        REFERENCES source_responses(response_id),

    CHECK (asset IN ('BTC', 'ETH')),
    CHECK (
        normalized_close_boundary_ns
        > normalized_open_ns
    ),
    CHECK (request_elapsed_ns >= 0)
);

CREATE INDEX candle_observations_lookup_idx
    ON candle_observations (
        experiment_id,
        asset,
        normalized_close_boundary_ns,
        response_received_utc_ns
    );

CREATE TABLE evaluation_candle_inputs (
    experiment_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    observation_id TEXT NOT NULL,

    PRIMARY KEY (
        experiment_id,
        attempt_id,
        ordinal
    ),

    FOREIGN KEY (experiment_id)
        REFERENCES experiment_manifest(experiment_id),

    FOREIGN KEY (observation_id)
        REFERENCES candle_observations(observation_id),

    CHECK (ordinal >= 0)
);


CREATE TABLE population_bindings (
    slot_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    recorded_run_id TEXT NOT NULL,

    binding_kind TEXT NOT NULL,

    canonical_event_id TEXT UNIQUE,
    canonical_attempt_id TEXT,
    canonical_decision TEXT,
    canonical_captured_ns INTEGER,

    reason TEXT,
    recorded_at_utc_ns INTEGER NOT NULL,

    FOREIGN KEY (experiment_id)
        REFERENCES experiment_manifest(experiment_id),

    FOREIGN KEY (slot_id)
        REFERENCES evaluation_slots(slot_id),

    FOREIGN KEY (canonical_event_id)
        REFERENCES journal_events(event_id),

    CHECK (
        binding_kind IN (
            'canonical',
            'gap'
        )
    ),

    CHECK (
        (
            binding_kind = 'canonical'
            AND canonical_event_id IS NOT NULL
            AND canonical_attempt_id IS NOT NULL
            AND canonical_decision IN (
                'wait',
                'call',
                'put'
            )
            AND canonical_captured_ns IS NOT NULL
            AND canonical_captured_ns > 0
            AND reason IS NULL
        )
        OR
        (
            binding_kind = 'gap'
            AND canonical_event_id IS NULL
            AND canonical_attempt_id IS NULL
            AND canonical_decision IS NULL
            AND canonical_captured_ns IS NULL
            AND reason IS NOT NULL
            AND length(reason) > 0
        )
    )
);

CREATE INDEX population_bindings_experiment_idx
    ON population_bindings (
        experiment_id,
        binding_kind,
        slot_id
    );

CREATE TRIGGER population_bindings_validate_slot
BEFORE INSERT ON population_bindings
WHEN NOT EXISTS (
    SELECT 1
    FROM evaluation_slots
    WHERE slot_id = NEW.slot_id
      AND experiment_id = NEW.experiment_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'population binding does not match evaluation slot'
    );
END;

CREATE TRIGGER population_bindings_validate_canonical_event
BEFORE INSERT ON population_bindings
WHEN NEW.binding_kind = 'canonical'
 AND NOT EXISTS (
    SELECT 1
    FROM journal_events
    WHERE event_id = NEW.canonical_event_id
      AND experiment_id = NEW.experiment_id
      AND slot_id = NEW.slot_id
      AND attempt_id = NEW.canonical_attempt_id
      AND event_type = 'evaluation_completed'
)
BEGIN
    SELECT RAISE(
        ABORT,
        'canonical population binding does not match evaluation event'
    );
END;

CREATE TABLE signal_endpoints (
    endpoint_id TEXT PRIMARY KEY,

    experiment_id TEXT NOT NULL,
    signal_event_id TEXT NOT NULL,

    horizon_seconds INTEGER NOT NULL,

    p0_observation_id TEXT NOT NULL,
    target_close_boundary_ns INTEGER NOT NULL,
    p30_observation_id TEXT,

    status TEXT NOT NULL,
    reason TEXT,

    recorded_at_utc_ns INTEGER NOT NULL,

    FOREIGN KEY (experiment_id)
        REFERENCES experiment_manifest(experiment_id),

    FOREIGN KEY (signal_event_id)
        REFERENCES journal_events(event_id),

    FOREIGN KEY (p0_observation_id)
        REFERENCES candle_observations(observation_id),

    FOREIGN KEY (p30_observation_id)
        REFERENCES candle_observations(observation_id),

    UNIQUE (
        experiment_id,
        signal_event_id,
        horizon_seconds
    ),

    CHECK (horizon_seconds = 1800),
    CHECK (
        status IN (
            'complete',
            'missing',
            'invalid',
            'ambiguous'
        )
    )
);

CREATE TRIGGER experiment_manifest_no_update
BEFORE UPDATE ON experiment_manifest
BEGIN
    SELECT RAISE(ABORT, 'experiment_manifest is append-only');
END;

CREATE TRIGGER experiment_manifest_no_delete
BEFORE DELETE ON experiment_manifest
BEGIN
    SELECT RAISE(ABORT, 'experiment_manifest is append-only');
END;

CREATE TRIGGER evaluation_slots_no_update
BEFORE UPDATE ON evaluation_slots
BEGIN
    SELECT RAISE(ABORT, 'evaluation_slots is append-only');
END;

CREATE TRIGGER evaluation_slots_no_delete
BEFORE DELETE ON evaluation_slots
BEGIN
    SELECT RAISE(ABORT, 'evaluation_slots is append-only');
END;

CREATE TRIGGER journal_events_no_update
BEFORE UPDATE ON journal_events
BEGIN
    SELECT RAISE(ABORT, 'journal_events is append-only');
END;

CREATE TRIGGER journal_events_no_delete
BEFORE DELETE ON journal_events
BEGIN
    SELECT RAISE(ABORT, 'journal_events is append-only');
END;

CREATE TRIGGER source_responses_no_update
BEFORE UPDATE ON source_responses
BEGIN
    SELECT RAISE(ABORT, 'source_responses is append-only');
END;

CREATE TRIGGER source_responses_no_delete
BEFORE DELETE ON source_responses
BEGIN
    SELECT RAISE(ABORT, 'source_responses is append-only');
END;

CREATE TRIGGER candle_observations_no_update
BEFORE UPDATE ON candle_observations
BEGIN
    SELECT RAISE(ABORT, 'candle_observations is append-only');
END;

CREATE TRIGGER candle_observations_no_delete
BEFORE DELETE ON candle_observations
BEGIN
    SELECT RAISE(ABORT, 'candle_observations is append-only');
END;

CREATE TRIGGER evaluation_candle_inputs_no_update
BEFORE UPDATE ON evaluation_candle_inputs
BEGIN
    SELECT RAISE(ABORT, 'evaluation_candle_inputs is append-only');
END;

CREATE TRIGGER evaluation_candle_inputs_no_delete
BEFORE DELETE ON evaluation_candle_inputs
BEGIN
    SELECT RAISE(ABORT, 'evaluation_candle_inputs is append-only');
END;


CREATE TRIGGER population_bindings_no_update
BEFORE UPDATE ON population_bindings
BEGIN
    SELECT RAISE(
        ABORT,
        'population_bindings is append-only'
    );
END;

CREATE TRIGGER population_bindings_no_delete
BEFORE DELETE ON population_bindings
BEGIN
    SELECT RAISE(
        ABORT,
        'population_bindings is append-only'
    );
END;

CREATE TRIGGER signal_endpoints_no_update
BEFORE UPDATE ON signal_endpoints
BEGIN
    SELECT RAISE(ABORT, 'signal_endpoints is append-only');
END;

CREATE TRIGGER signal_endpoints_no_delete
BEFORE DELETE ON signal_endpoints
BEGIN
    SELECT RAISE(ABORT, 'signal_endpoints is append-only');
END;

CREATE TRIGGER journal_schema_identity_no_update
BEFORE UPDATE ON journal_schema_identity
BEGIN
    SELECT RAISE(
        ABORT,
        'journal_schema_identity is append-only'
    );
END;

CREATE TRIGGER journal_schema_identity_no_delete
BEFORE DELETE ON journal_schema_identity
BEGIN
    SELECT RAISE(
        ABORT,
        'journal_schema_identity is append-only'
    );
END;

