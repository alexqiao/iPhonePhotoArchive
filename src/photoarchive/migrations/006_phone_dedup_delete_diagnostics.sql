ALTER TABLE phone_assets ADD COLUMN delete_error_detail TEXT;
ALTER TABLE phone_items ADD COLUMN delete_error_detail TEXT;

CREATE INDEX idx_phone_items_fingerprint ON phone_items(item_fingerprint, expected_size);
