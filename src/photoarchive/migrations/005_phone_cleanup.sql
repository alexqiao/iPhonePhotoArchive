CREATE TABLE profile_devices (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES archive_profiles(id),
    device_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    product_kind TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_profile_one_active_device
ON profile_devices(profile_id) WHERE active = 1;

CREATE TABLE phone_batches (
    batch_id TEXT PRIMARY KEY REFERENCES import_batches(id),
    device_id TEXT NOT NULL REFERENCES profile_devices(id),
    cutoff_at_utc TEXT NOT NULL,
    icloud_photos_enabled INTEGER CHECK (icloud_photos_enabled IN (0, 1)),
    state TEXT NOT NULL CHECK (
        state IN (
            'PREPARED', 'IMPORTING', 'ARCHIVING', 'READY_FOR_PHONE_CLEANUP',
            'CLEANING_PHONE', 'COMPLETED', 'COMPLETED_WITH_PHONE_ITEMS_REMAINING',
            'NEEDS_ATTENTION'
        )
    ),
    deletion_plan_sha256 TEXT,
    confirmed_at TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE phone_assets (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES phone_batches(batch_id),
    device_asset_key TEXT NOT NULL,
    creation_at_utc TEXT,
    media_type TEXT NOT NULL,
    cleanup_state TEXT NOT NULL CHECK (
        cleanup_state IN ('PENDING', 'READY', 'DELETE_INTENT', 'DELETED', 'REMAINING', 'FAILED')
    ),
    warning TEXT,
    error_code TEXT,
    deleted_at TEXT,
    UNIQUE(batch_id, device_asset_key)
);

CREATE TABLE phone_items (
    id TEXT PRIMARY KEY,
    phone_asset_id TEXT NOT NULL REFERENCES phone_assets(id),
    session_token TEXT NOT NULL,
    item_fingerprint TEXT NOT NULL,
    ptp_object_handle INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    uti TEXT,
    expected_size INTEGER NOT NULL CHECK (expected_size >= 0),
    creation_at_utc TEXT,
    modification_at_utc TEXT,
    required INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
    downloadable INTEGER NOT NULL DEFAULT 1 CHECK (downloadable IN (0, 1)),
    warning TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('DISCOVERED', 'DOWNLOADED', 'VERIFIED', 'DELETED', 'FAILED', 'SKIPPED')
    ),
    resource_id TEXT REFERENCES asset_resources(id),
    error_code TEXT,
    deleted_at TEXT,
    UNIQUE(phone_asset_id, item_fingerprint),
    UNIQUE(phone_asset_id, relative_path)
);

CREATE INDEX idx_profile_devices_profile ON profile_devices(profile_id, active);
CREATE INDEX idx_phone_assets_batch_state ON phone_assets(batch_id, cleanup_state);
CREATE INDEX idx_phone_items_asset_status ON phone_items(phone_asset_id, status);
