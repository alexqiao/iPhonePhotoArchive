CREATE TABLE archive_profiles (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    archive_subdirectory TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE import_batches (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES archive_profiles(id),
    source_relative_path TEXT NOT NULL UNIQUE,
    completed_relative_path TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (
        state IN ('PREPARED', 'ARCHIVING', 'NEEDS_ATTENTION', 'COMPLETED')
    ),
    job_id TEXT REFERENCES archive_jobs(id),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE batch_files (
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    resource_id TEXT NOT NULL REFERENCES asset_resources(id),
    relative_path TEXT NOT NULL,
    source_device INTEGER NOT NULL,
    source_inode INTEGER NOT NULL,
    source_size INTEGER NOT NULL CHECK (source_size >= 0),
    source_mtime_ns INTEGER NOT NULL,
    metadata_warning TEXT,
    PRIMARY KEY (batch_id, relative_path),
    UNIQUE (batch_id, resource_id)
);

CREATE TABLE batch_operations (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('INTENT', 'SUCCEEDED', 'FAILED')),
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

ALTER TABLE archive_jobs ADD COLUMN source_adapter TEXT;
ALTER TABLE archive_jobs ADD COLUMN target_adapter TEXT;
ALTER TABLE archive_jobs ADD COLUMN profile_id TEXT REFERENCES archive_profiles(id);
ALTER TABLE archive_jobs ADD COLUMN batch_id TEXT REFERENCES import_batches(id);

ALTER TABLE asset_resources ADD COLUMN source_relative_path TEXT;
ALTER TABLE asset_resources ADD COLUMN expected_mtime_ns INTEGER;
ALTER TABLE asset_resources ADD COLUMN expected_device INTEGER;
ALTER TABLE asset_resources ADD COLUMN expected_inode INTEGER;

UPDATE archive_jobs SET source_adapter = photos_adapter WHERE source_adapter IS NULL;
UPDATE archive_jobs SET target_adapter = onedrive_adapter WHERE target_adapter IS NULL;

CREATE INDEX idx_batches_profile_state ON import_batches(profile_id, state, created_at);
CREATE INDEX idx_batch_files_resource ON batch_files(resource_id);
