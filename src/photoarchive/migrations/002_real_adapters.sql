ALTER TABLE archive_jobs ADD COLUMN photos_adapter TEXT NOT NULL DEFAULT 'fixture';
ALTER TABLE archive_jobs ADD COLUMN onedrive_adapter TEXT NOT NULL DEFAULT 'fake';

CREATE TABLE upload_sessions (
    id TEXT PRIMARY KEY,
    resource_id TEXT,
    remote_path TEXT NOT NULL UNIQUE,
    expected_size INTEGER NOT NULL CHECK (expected_size >= 0),
    next_start INTEGER NOT NULL DEFAULT 0 CHECK (next_start >= 0),
    expiration_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'COMPLETE')),
    updated_at TEXT NOT NULL
);
