ALTER TABLE icloud_batches
ADD COLUMN selection_mode TEXT NOT NULL DEFAULT 'age_cutoff'
CHECK (selection_mode IN ('age_cutoff', 'large_video'));

ALTER TABLE icloud_batches
ADD COLUMN selection_threshold_bytes INTEGER
CHECK (selection_threshold_bytes IS NULL OR selection_threshold_bytes > 0);

CREATE TABLE icloud_video_measurements (
    profile_id TEXT NOT NULL REFERENCES archive_profiles(id),
    photos_local_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    threshold_bytes INTEGER NOT NULL CHECK (threshold_bytes > 0),
    observed_bytes INTEGER NOT NULL CHECK (observed_bytes >= 0),
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    exceeds_threshold INTEGER NOT NULL CHECK (exceeds_threshold IN (0, 1)),
    measured_at TEXT NOT NULL,
    CHECK (
        (exceeds_threshold = 1 AND observed_bytes > threshold_bytes)
        OR
        (exceeds_threshold = 0 AND complete = 1 AND observed_bytes <= threshold_bytes)
    ),
    PRIMARY KEY (profile_id, photos_local_id, resource_key, threshold_bytes)
);

CREATE INDEX idx_icloud_video_measurements_lookup
ON icloud_video_measurements(profile_id, threshold_bytes, photos_local_id);
