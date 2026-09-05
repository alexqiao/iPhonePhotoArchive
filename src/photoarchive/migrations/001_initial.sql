CREATE TABLE IF NOT EXISTS archive_jobs (
    id TEXT PRIMARY KEY,
    cutoff_at_utc TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    fixture_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('PLANNED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED')
    ),
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE assets (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL,
    photos_local_id TEXT NOT NULL,
    creation_at_utc TEXT NOT NULL,
    media_type TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('DISCOVERED', 'EXPORTED', 'UPLOADED', 'VERIFIED', 'SAFE_TO_DELETE', 'FAILED')
    ),
    fingerprint_sha256 TEXT,
    last_stable_state TEXT,
    error_code TEXT,
    unresolved_warning_count INTEGER NOT NULL DEFAULT 0 CHECK (unresolved_warning_count >= 0),
    review_required INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0, 1)),
    expected_resource_types_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (library_id, photos_local_id)
);

CREATE TABLE asset_resources (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(id),
    resource_key TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    uti TEXT,
    original_name TEXT NOT NULL,
    archive_name TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    expected_size INTEGER NOT NULL CHECK (expected_size >= 0),
    actual_size INTEGER CHECK (actual_size IS NULL OR actual_size >= 0),
    sha256 TEXT,
    quickxor TEXT,
    required INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
    UNIQUE (asset_id, resource_key)
);

CREATE TABLE job_assets (
    job_id TEXT NOT NULL REFERENCES archive_jobs(id),
    asset_id TEXT NOT NULL REFERENCES assets(id),
    action TEXT NOT NULL,
    result TEXT,
    PRIMARY KEY (job_id, asset_id)
);

CREATE TABLE archive_files (
    id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL UNIQUE REFERENCES asset_resources(id),
    local_path TEXT NOT NULL,
    remote_path TEXT NOT NULL,
    drive_item_id TEXT NOT NULL,
    etag TEXT NOT NULL,
    quickxor TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    updated_at TEXT NOT NULL,
    UNIQUE (remote_path)
);

CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    asset_id TEXT NOT NULL REFERENCES assets(id),
    resource_id TEXT REFERENCES asset_resources(id),
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('INTENT', 'SUCCEEDED', 'FAILED')),
    request_json TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE verification_records (
    id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES asset_resources(id),
    check_type TEXT NOT NULL,
    expected TEXT,
    actual TEXT,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    checked_at TEXT NOT NULL,
    run_id TEXT NOT NULL,
    UNIQUE (resource_id, check_type, run_id)
);

CREATE TABLE state_transitions (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE run_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    level TEXT NOT NULL,
    event_code TEXT NOT NULL,
    job_id TEXT,
    asset_key TEXT,
    duration_ms INTEGER,
    retry INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_assets_state_creation ON assets(state, creation_at_utc);
CREATE INDEX idx_job_assets_job ON job_assets(job_id, asset_id);
CREATE INDEX idx_verification_resource ON verification_records(resource_id, passed, checked_at);
CREATE INDEX idx_operations_asset_status ON operations(asset_id, status);
CREATE INDEX idx_events_run ON run_events(run_id, created_at);
