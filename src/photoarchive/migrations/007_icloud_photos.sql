CREATE TABLE icloud_batches (
    batch_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES archive_profiles(id),
    job_id TEXT NOT NULL UNIQUE REFERENCES archive_jobs(id),
    library_id TEXT NOT NULL,
    cutoff_at_utc TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'ARCHIVING', 'READY_FOR_ICLOUD_CLEANUP', 'CLEANING_ICLOUD',
            'COMPLETED', 'COMPLETED_WITH_ITEMS_REMAINING', 'NEEDS_ATTENTION'
        )
    ),
    deletion_plan_sha256 TEXT,
    confirmed_at TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE icloud_batch_assets (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES icloud_batches(batch_id),
    asset_id TEXT NOT NULL REFERENCES assets(id),
    cleanup_state TEXT NOT NULL CHECK (
        cleanup_state IN ('PENDING', 'READY', 'DELETE_INTENT', 'DELETED', 'REMAINING', 'FAILED')
    ),
    error_code TEXT,
    delete_error_detail TEXT,
    deleted_at TEXT,
    UNIQUE(batch_id, asset_id)
);

CREATE INDEX idx_icloud_batch_assets_state
ON icloud_batch_assets(batch_id, cleanup_state);
