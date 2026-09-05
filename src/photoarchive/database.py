from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, cast

from photoarchive.domain import (
    AssetState,
    DiscoveredAsset,
    IntegrityError,
    RemoteObject,
    validate_transition,
)
from photoarchive.paths import archive_names, asset_id, resource_id


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()
        if self.path.exists():
            os.chmod(self.path, 0o600)

    def migrate(self) -> list[str]:
        applied: list[str] = []
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    checksum TEXT NOT NULL
                )
                """
            )
            migration_root = resources.files("photoarchive.migrations")
            for entry in sorted(migration_root.iterdir(), key=lambda item: item.name):
                if not entry.name.endswith(".sql"):
                    continue
                sql = entry.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                row = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = ?",
                    (entry.name,),
                ).fetchone()
                if row is not None:
                    if row["checksum"] != checksum:
                        raise RuntimeError(f"migration checksum mismatch: {entry.name}")
                    continue
                escaped_version = entry.name.replace("'", "''")
                escaped_time = utc_now().replace("'", "''")
                escaped_checksum = checksum.replace("'", "''")
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + sql
                    + "\nINSERT INTO schema_migrations(version, applied_at, checksum) "
                    + f"VALUES ('{escaped_version}', '{escaped_time}', '{escaped_checksum}');\n"
                    + "COMMIT;"
                )
                applied.append(entry.name)
        return applied

    def create_job(
        self,
        cutoff_at_utc: str,
        config_hash: str,
        source_reference: str,
        *,
        source_adapter: str = "fixture",
        target_adapter: str = "fake",
        profile_id: str | None = None,
        batch_id: str | None = None,
    ) -> str:
        job_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO archive_jobs(
                    id, cutoff_at_utc, config_hash, fixture_path, status, created_at,
                    photos_adapter, onedrive_adapter, source_adapter, target_adapter,
                    profile_id, batch_id
                ) VALUES (?, ?, ?, ?, 'PLANNED', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    cutoff_at_utc,
                    config_hash,
                    source_reference,
                    now,
                    source_adapter,
                    target_adapter,
                    source_adapter,
                    target_adapter,
                    profile_id,
                    batch_id,
                ),
            )
            connection.execute("COMMIT")
        return job_id

    def create_profile(self, profile_id: str, display_name: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO archive_profiles(id, display_name, archive_subdirectory, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (profile_id, display_name, profile_id, utc_now()),
            )
            connection.execute("COMMIT")

    def list_profiles(self) -> Sequence[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM archive_profiles ORDER BY id").fetchall()

    def get_profile(self, profile_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM archive_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown profile: {profile_id}")
        return cast(sqlite3.Row, row)

    def bind_profile_device(
        self,
        profile_id: str,
        device_key: str,
        display_name: str,
        product_kind: str,
        *,
        replace: bool = False,
    ) -> sqlite3.Row:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT profile_id FROM profile_devices WHERE device_key = ?",
                (device_key,),
            ).fetchone()
            if owner is not None and owner["profile_id"] != profile_id:
                connection.execute("ROLLBACK")
                raise IntegrityError("this phone is already bound to another profile")
            current = connection.execute(
                "SELECT * FROM profile_devices WHERE profile_id = ? AND active = 1",
                (profile_id,),
            ).fetchone()
            if current is not None and current["device_key"] != device_key and not replace:
                connection.execute("ROLLBACK")
                raise IntegrityError("profile is already bound to another phone; use --replace")
            if current is not None and current["device_key"] != device_key:
                connection.execute(
                    "UPDATE profile_devices SET active = 0, last_seen_at = ? WHERE id = ?",
                    (now, current["id"]),
                )
            device_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO profile_devices(
                    id, profile_id, device_key, display_name, product_kind,
                    active, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(device_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    product_kind = excluded.product_kind,
                    active = 1,
                    last_seen_at = excluded.last_seen_at
                """,
                (device_id, profile_id, device_key, display_name, product_kind, now, now),
            )
            row = connection.execute(
                "SELECT * FROM profile_devices WHERE device_key = ?", (device_key,)
            ).fetchone()
            connection.execute("COMMIT")
        assert row is not None
        return cast(sqlite3.Row, row)

    def get_active_profile_device(self, profile_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM profile_devices WHERE profile_id = ? AND active = 1",
                (profile_id,),
            ).fetchone()
        return cast(sqlite3.Row | None, row)

    def create_batch(
        self,
        batch_id: str,
        profile_id: str,
        source_relative_path: str,
        completed_relative_path: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO import_batches(
                    id, profile_id, source_relative_path, completed_relative_path,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PREPARED', ?, ?)
                """,
                (
                    batch_id,
                    profile_id,
                    source_relative_path,
                    completed_relative_path,
                    now,
                    now,
                ),
            )
            connection.execute("COMMIT")

    def create_phone_batch(
        self,
        batch_id: str,
        device_id: str,
        cutoff_at_utc: str,
        icloud_photos_enabled: bool | None,
    ) -> None:
        now = utc_now()
        icloud = None if icloud_photos_enabled is None else int(icloud_photos_enabled)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO phone_batches(
                    batch_id, device_id, cutoff_at_utc, icloud_photos_enabled,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PREPARED', ?, ?)
                """,
                (batch_id, device_id, cutoff_at_utc, icloud, now, now),
            )
            connection.execute("COMMIT")

    def get_phone_batch(self, batch_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT phone_batches.*, import_batches.profile_id, import_batches.job_id,
                       import_batches.source_relative_path,
                       import_batches.completed_relative_path,
                       profile_devices.device_key, profile_devices.display_name AS device_name,
                       profile_devices.product_kind
                FROM phone_batches
                JOIN import_batches ON import_batches.id = phone_batches.batch_id
                JOIN profile_devices ON profile_devices.id = phone_batches.device_id
                WHERE phone_batches.batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown phone batch: {batch_id}")
        return cast(sqlite3.Row, row)

    def set_phone_batch_state(
        self,
        batch_id: str,
        state: str,
        *,
        error_code: str | None = None,
        plan_sha256: str | None = None,
        confirmed: bool = False,
    ) -> None:
        now = utc_now()
        completed = now if state in {"COMPLETED", "COMPLETED_WITH_PHONE_ITEMS_REMAINING"} else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE phone_batches
                SET state = ?, error_code = ?,
                    deletion_plan_sha256 = COALESCE(?, deletion_plan_sha256),
                    confirmed_at = CASE WHEN ? THEN ? ELSE confirmed_at END,
                    completed_at = COALESCE(completed_at, ?), updated_at = ?
                WHERE batch_id = ?
                """,
                (
                    state,
                    error_code,
                    plan_sha256,
                    int(confirmed),
                    now,
                    completed,
                    now,
                    batch_id,
                ),
            )

    def set_phone_batch_icloud_status(self, batch_id: str, enabled: bool) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE phone_batches
                SET icloud_photos_enabled = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (int(enabled), utc_now(), batch_id),
            )

    def record_phone_item(
        self,
        batch_id: str,
        *,
        asset_id: str,
        asset_key: str,
        media_type: str,
        asset_creation_at_utc: str | None,
        asset_warning: str | None,
        item_id: str,
        session_token: str,
        item_fingerprint: str,
        ptp_object_handle: int,
        original_name: str,
        relative_path: str,
        uti: str | None,
        expected_size: int,
        creation_at_utc: str | None,
        modification_at_utc: str | None,
        required: bool,
        downloadable: bool,
        warning: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO phone_assets(
                    id, batch_id, device_asset_key, creation_at_utc,
                    media_type, cleanup_state, warning
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
                ON CONFLICT(batch_id, device_asset_key) DO NOTHING
                """,
                (
                    asset_id,
                    batch_id,
                    asset_key,
                    asset_creation_at_utc,
                    media_type,
                    asset_warning,
                ),
            )
            connection.execute(
                """
                INSERT INTO phone_items(
                    id, phone_asset_id, session_token, item_fingerprint,
                    ptp_object_handle, original_name, relative_path, uti,
                    expected_size, creation_at_utc, modification_at_utc,
                    required, downloadable, warning, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DISCOVERED')
                ON CONFLICT(phone_asset_id, item_fingerprint) DO UPDATE SET
                    session_token = excluded.session_token,
                    ptp_object_handle = excluded.ptp_object_handle
                """,
                (
                    item_id,
                    asset_id,
                    session_token,
                    item_fingerprint,
                    ptp_object_handle,
                    original_name,
                    relative_path,
                    uti,
                    expected_size,
                    creation_at_utc,
                    modification_at_utc,
                    int(required),
                    int(downloadable),
                    warning,
                ),
            )
            connection.execute("COMMIT")

    def list_phone_items(self, batch_id: str) -> Sequence[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT phone_items.*, phone_assets.device_asset_key,
                       phone_assets.media_type, phone_assets.cleanup_state,
                       phone_assets.creation_at_utc AS asset_creation_at_utc,
                       phone_assets.warning AS asset_warning
                FROM phone_items
                JOIN phone_assets ON phone_assets.id = phone_items.phone_asset_id
                WHERE phone_assets.batch_id = ?
                ORDER BY phone_assets.device_asset_key, phone_items.relative_path
                """,
                (batch_id,),
            ).fetchall()

    def set_phone_item_status(
        self,
        item_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        deleted_at = utc_now() if status == "DELETED" else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE phone_items
                SET status = ?, error_code = ?, delete_error_detail = ?,
                    deleted_at = COALESCE(deleted_at, ?)
                WHERE id = ?
                """,
                (status, error_code, error_detail, deleted_at, item_id),
            )

    def set_phone_asset_state(
        self,
        asset_id: str,
        state: str,
        *,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        deleted_at = utc_now() if state == "DELETED" else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE phone_assets
                SET cleanup_state = ?, error_code = ?, delete_error_detail = ?,
                    deleted_at = COALESCE(deleted_at, ?)
                WHERE id = ?
                """,
                (state, error_code, error_detail, deleted_at, asset_id),
            )

    def reusable_phone_resource(
        self,
        device_key: str,
        item_fingerprint: str,
        expected_size: int,
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT phone_items.resource_id, asset_resources.asset_id,
                       asset_resources.sha256, asset_resources.quickxor,
                       archive_files.remote_path, archive_files.size
                FROM phone_items
                JOIN phone_assets ON phone_assets.id = phone_items.phone_asset_id
                JOIN phone_batches ON phone_batches.batch_id = phone_assets.batch_id
                JOIN profile_devices ON profile_devices.id = phone_batches.device_id
                JOIN asset_resources ON asset_resources.id = phone_items.resource_id
                JOIN assets ON assets.id = asset_resources.asset_id
                JOIN archive_files ON archive_files.resource_id = asset_resources.id
                WHERE profile_devices.device_key = ?
                  AND phone_items.item_fingerprint = ?
                  AND phone_items.expected_size = ?
                  AND archive_files.size = ?
                  AND assets.state = 'SAFE_TO_DELETE'
                  AND assets.review_required = 0
                  AND asset_resources.sha256 IS NOT NULL
                  AND asset_resources.quickxor IS NOT NULL
                ORDER BY phone_batches.created_at, phone_items.id
                """,
                (device_key, item_fingerprint, expected_size, expected_size),
            ).fetchall()
        if not rows:
            return None
        evidence = {
            (int(row["size"]), str(row["sha256"]), str(row["quickxor"])) for row in rows
        }
        if len(evidence) != 1:
            return None
        return cast(sqlite3.Row, rows[0])

    def reuse_phone_item_archive(self, item_id: str, resource_record_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE phone_items
                SET resource_id = ?, status = 'VERIFIED', error_code = NULL,
                    delete_error_detail = NULL
                WHERE id = ?
                """,
                (resource_record_id, item_id),
            )

    def attach_phone_archive_assets(self, job_id: str, batch_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO job_assets(job_id, asset_id, action, result)
                SELECT DISTINCT ?, asset_resources.asset_id, 'ARCHIVE', NULL
                FROM phone_items
                JOIN phone_assets ON phone_assets.id = phone_items.phone_asset_id
                JOIN asset_resources ON asset_resources.id = phone_items.resource_id
                WHERE phone_assets.batch_id = ?
                """,
                (job_id, batch_id),
            )

    def repair_legacy_phone_relationship_warnings(self, batch_id: str) -> int:
        """Clear the obsolete singleton-relatedUUID warning from existing phone batches."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE phone_items
                SET warning = NULL, status = 'DISCOVERED', error_code = NULL
                WHERE phone_asset_id IN (
                    SELECT id FROM phone_assets WHERE batch_id = ?
                )
                  AND status = 'SKIPPED'
                  AND warning = 'INCOMPLETE_RELATED_ASSET'
                  AND error_code = 'INCOMPLETE_RELATED_ASSET'
                """,
                (batch_id,),
            ).rowcount
            connection.execute(
                """
                UPDATE phone_assets
                SET warning = NULL, cleanup_state = 'PENDING', error_code = NULL
                WHERE batch_id = ?
                  AND warning = 'INCOMPLETE_RELATED_ASSET'
                  AND cleanup_state != 'DELETED'
                """,
                (batch_id,),
            )
            connection.execute("COMMIT")
        return updated

    def map_phone_items_to_archive_resources(self, batch_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE phone_items
                SET resource_id = COALESCE((
                    SELECT batch_files.resource_id
                    FROM batch_files
                    WHERE batch_files.batch_id = ?
                      AND batch_files.relative_path = phone_items.relative_path
                ), resource_id)
                WHERE phone_asset_id IN (
                    SELECT id FROM phone_assets WHERE batch_id = ?
                )
                """,
                (batch_id, batch_id),
            )
            connection.execute(
                """
                UPDATE batch_files
                SET metadata_warning = NULL
                WHERE batch_id = ? AND relative_path IN (
                    SELECT phone_items.relative_path
                    FROM phone_items
                    JOIN phone_assets ON phone_assets.id = phone_items.phone_asset_id
                    WHERE phone_assets.batch_id = ?
                      AND phone_items.creation_at_utc IS NOT NULL
                      AND phone_items.warning IS NULL
                )
                """,
                (batch_id, batch_id),
            )
            connection.execute(
                """
                UPDATE assets
                SET creation_at_utc = (
                    SELECT MIN(phone_assets.creation_at_utc)
                    FROM asset_resources
                    JOIN batch_files ON batch_files.resource_id = asset_resources.id
                    JOIN phone_items
                      ON phone_items.relative_path = batch_files.relative_path
                    JOIN phone_assets ON phone_assets.id = phone_items.phone_asset_id
                    WHERE asset_resources.asset_id = assets.id
                      AND batch_files.batch_id = ?
                      AND phone_assets.batch_id = ?
                )
                WHERE id IN (
                    SELECT asset_resources.asset_id
                    FROM asset_resources
                    JOIN batch_files ON batch_files.resource_id = asset_resources.id
                    WHERE batch_files.batch_id = ?
                )
                """,
                (batch_id, batch_id, batch_id),
            )
            connection.execute("COMMIT")

    def phone_asset_gate_rows(self, batch_id: str) -> Sequence[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT phone_assets.id, phone_assets.device_asset_key,
                       phone_assets.warning, phone_assets.cleanup_state,
                       COUNT(phone_items.id) AS item_count,
                       SUM(CASE WHEN phone_items.required = 1 THEN 1 ELSE 0 END) AS required_count,
                       SUM(CASE WHEN phone_items.required = 1
                                      AND phone_items.downloadable = 1
                                      AND phone_items.warning IS NULL
                                      AND phone_assets.creation_at_utc IS NOT NULL
                                      AND phone_assets.creation_at_utc < phone_batches.cutoff_at_utc
                                      AND phone_items.resource_id IS NOT NULL
                                      AND assets.state = 'SAFE_TO_DELETE'
                                      AND assets.review_required = 0
                                      AND batch_files.metadata_warning IS NULL
                                THEN 1 ELSE 0 END) AS verified_count
                FROM phone_assets
                JOIN phone_batches ON phone_batches.batch_id = phone_assets.batch_id
                JOIN phone_items ON phone_items.phone_asset_id = phone_assets.id
                LEFT JOIN asset_resources ON asset_resources.id = phone_items.resource_id
                LEFT JOIN assets ON assets.id = asset_resources.asset_id
                LEFT JOIN batch_files
                  ON batch_files.resource_id = phone_items.resource_id
                 AND batch_files.batch_id = ?
                WHERE phone_assets.batch_id = ?
                GROUP BY phone_assets.id
                ORDER BY phone_assets.device_asset_key
                """,
                (batch_id, batch_id),
            ).fetchall()

    def create_icloud_batch(
        self,
        batch_id: str,
        profile_id: str,
        job_id: str,
        library_id: str,
        cutoff_at_utc: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO icloud_batches(
                    batch_id, profile_id, job_id, library_id, cutoff_at_utc,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'ARCHIVING', ?, ?)
                """,
                (batch_id, profile_id, job_id, library_id, cutoff_at_utc, now, now),
            )
            assets = connection.execute(
                "SELECT asset_id FROM job_assets WHERE job_id = ? ORDER BY asset_id",
                (job_id,),
            ).fetchall()
            for row in assets:
                record_id = hashlib.sha256(
                    f"{batch_id}\0{row['asset_id']}".encode()
                ).hexdigest()[:32]
                connection.execute(
                    """
                    INSERT INTO icloud_batch_assets(id, batch_id, asset_id, cleanup_state)
                    VALUES (?, ?, ?, 'PENDING')
                    """,
                    (record_id, batch_id, row["asset_id"]),
                )
            connection.execute("COMMIT")

    def get_icloud_batch(self, batch_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT icloud_batches.*, archive_profiles.display_name AS profile_name
                FROM icloud_batches
                JOIN archive_profiles ON archive_profiles.id = icloud_batches.profile_id
                WHERE icloud_batches.batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown iCloud batch: {batch_id}")
        return cast(sqlite3.Row, row)

    def get_icloud_batch_for_job(self, job_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM icloud_batches WHERE job_id = ?", (job_id,)
            ).fetchone()
        return cast(sqlite3.Row | None, row)

    def set_icloud_batch_state(
        self,
        batch_id: str,
        state: str,
        *,
        error_code: str | None = None,
        plan_sha256: str | None = None,
        confirmed: bool = False,
    ) -> None:
        now = utc_now()
        completed = now if state in {"COMPLETED", "COMPLETED_WITH_ITEMS_REMAINING"} else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE icloud_batches
                SET state = ?, error_code = ?,
                    deletion_plan_sha256 = COALESCE(?, deletion_plan_sha256),
                    confirmed_at = CASE WHEN ? THEN ? ELSE confirmed_at END,
                    completed_at = COALESCE(completed_at, ?), updated_at = ?
                WHERE batch_id = ?
                """,
                (
                    state,
                    error_code,
                    plan_sha256,
                    int(confirmed),
                    now,
                    completed,
                    now,
                    batch_id,
                ),
            )

    def list_icloud_batch_assets(self, batch_id: str) -> Sequence[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT icloud_batch_assets.id AS batch_asset_id,
                       icloud_batch_assets.batch_id,
                       icloud_batch_assets.cleanup_state,
                       icloud_batch_assets.error_code,
                       icloud_batch_assets.delete_error_detail,
                       icloud_batch_assets.deleted_at,
                       assets.*
                FROM icloud_batch_assets
                JOIN assets ON assets.id = icloud_batch_assets.asset_id
                WHERE icloud_batch_assets.batch_id = ?
                ORDER BY assets.creation_at_utc, assets.id
                """,
                (batch_id,),
            ).fetchall()

    def set_icloud_asset_state(
        self,
        batch_asset_id: str,
        state: str,
        *,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        deleted_at = utc_now() if state == "DELETED" else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE icloud_batch_assets
                SET cleanup_state = ?, error_code = ?, delete_error_detail = ?,
                    deleted_at = COALESCE(deleted_at, ?)
                WHERE id = ?
                """,
                (state, error_code, error_detail, deleted_at, batch_asset_id),
            )

    def icloud_asset_gate_rows(self, batch_id: str) -> Sequence[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT icloud_batch_assets.id AS batch_asset_id,
                       icloud_batch_assets.asset_id,
                       icloud_batch_assets.cleanup_state,
                       assets.state AS archive_state,
                       assets.review_required,
                       COUNT(CASE WHEN asset_resources.required = 1 THEN 1 END) AS required_count,
                       SUM(CASE WHEN asset_resources.required = 1
                                      AND archive_files.id IS NOT NULL
                                      AND asset_resources.actual_size IS NOT NULL
                                      AND asset_resources.sha256 IS NOT NULL
                                      AND asset_resources.quickxor IS NOT NULL
                                THEN 1 ELSE 0 END) AS archived_count
                FROM icloud_batch_assets
                JOIN assets ON assets.id = icloud_batch_assets.asset_id
                LEFT JOIN asset_resources ON asset_resources.asset_id = assets.id
                LEFT JOIN archive_files ON archive_files.resource_id = asset_resources.id
                WHERE icloud_batch_assets.batch_id = ?
                GROUP BY icloud_batch_assets.id, icloud_batch_assets.asset_id
                ORDER BY icloud_batch_assets.asset_id
                """,
                (batch_id,),
            ).fetchall()

    def get_batch(self, batch_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM import_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown batch: {batch_id}")
        return cast(sqlite3.Row, row)

    def list_batches(self, profile_id: str | None = None) -> Sequence[sqlite3.Row]:
        with self.connect() as connection:
            if profile_id is None:
                return connection.execute(
                    "SELECT * FROM import_batches ORDER BY created_at DESC"
                ).fetchall()
            return connection.execute(
                """
                SELECT * FROM import_batches
                WHERE profile_id = ? ORDER BY created_at DESC
                """,
                (profile_id,),
            ).fetchall()

    def set_batch_state(
        self,
        batch_id: str,
        state: str,
        *,
        error_code: str | None = None,
    ) -> None:
        completed_at = utc_now() if state == "COMPLETED" else None
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE import_batches
                SET state = ?, error_code = ?, updated_at = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE id = ?
                """,
                (state, error_code, utc_now(), completed_at, batch_id),
            )
            connection.execute("COMMIT")

    def attach_batch_job(self, batch_id: str, job_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE import_batches
                SET job_id = COALESCE(job_id, ?), state = 'ARCHIVING', updated_at = ?
                WHERE id = ? AND (job_id IS NULL OR job_id = ?)
                """,
                (job_id, utc_now(), batch_id, job_id),
            )
            if connection.total_changes != 1:
                connection.execute("ROLLBACK")
                raise RuntimeError("batch is already attached to another job")
            connection.execute("COMMIT")

    def record_batch_file(
        self,
        batch_id: str,
        resource_record_id: str,
        relative_path: str,
        *,
        device: int,
        inode: int,
        size: int,
        mtime_ns: int,
        metadata_warning: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO batch_files(
                    batch_id, resource_id, relative_path, source_device, source_inode,
                    source_size, source_mtime_ns, metadata_warning
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id, relative_path) DO NOTHING
                """,
                (
                    batch_id,
                    resource_record_id,
                    relative_path,
                    device,
                    inode,
                    size,
                    mtime_ns,
                    metadata_warning,
                ),
            )
            row = connection.execute(
                """
                SELECT resource_id, source_device, source_inode, source_size, source_mtime_ns
                FROM batch_files WHERE batch_id = ? AND relative_path = ?
                """,
                (batch_id, relative_path),
            ).fetchone()
            assert row is not None
            expected = (resource_record_id, device, inode, size, mtime_ns)
            actual = (
                row["resource_id"],
                row["source_device"],
                row["source_inode"],
                row["source_size"],
                row["source_mtime_ns"],
            )
            if actual != expected:
                raise IntegrityError("import batch file changed after planning")

    def begin_batch_operation(self, batch_id: str, kind: str, key: str) -> sqlite3.Row:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO batch_operations(
                    id, batch_id, kind, idempotency_key, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'INTENT', ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (str(uuid.uuid4()), batch_id, kind, key, now, now),
            )
            row = connection.execute(
                "SELECT * FROM batch_operations WHERE idempotency_key = ?", (key,)
            ).fetchone()
            connection.execute("COMMIT")
        assert row is not None
        return cast(sqlite3.Row, row)

    def finish_batch_operation(self, key: str, status: str, result: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE batch_operations
                SET status = ?, result_json = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (status, json.dumps(result, sort_keys=True), utc_now(), key),
            )

    def get_job(self, job_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM archive_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown job: {job_id}")
        return cast(sqlite3.Row, row)

    def set_job_status(self, job_id: str, status: str) -> None:
        now = utc_now()
        started_at = now if status == "RUNNING" else None
        completed_at = now if status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"} else None
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE archive_jobs
                SET status = ?,
                    started_at = COALESCE(started_at, ?),
                    completed_at = ?
                WHERE id = ?
                """,
                (status, started_at, completed_at, job_id),
            )
            connection.execute("COMMIT")

    def persist_asset(self, job_id: str, asset: DiscoveredAsset, run_id: str) -> str:
        parent_id = asset_id(asset.library_id, asset.photos_local_id)
        now = utc_now()
        names = archive_names(asset.resources)
        available_types = {
            resource.resource_type for resource in asset.resources if resource.required
        }
        missing = set(asset.required_resource_types) - available_types
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM assets WHERE library_id = ? AND photos_local_id = ?",
                (asset.library_id, asset.photos_local_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO assets(
                        id, library_id, photos_local_id, creation_at_utc, media_type, state,
                        last_stable_state, unresolved_warning_count,
                        expected_resource_types_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'DISCOVERED', 'DISCOVERED', ?, ?, ?, ?)
                    """,
                    (
                        parent_id,
                        asset.library_id,
                        asset.photos_local_id,
                        asset.creation_at_utc.isoformat().replace("+00:00", "Z"),
                        asset.media_type,
                        len(missing),
                        json.dumps(asset.required_resource_types),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO state_transitions(
                        id, asset_id, from_state, to_state, reason, run_id, created_at
                    ) VALUES (?, ?, NULL, 'DISCOVERED', 'PLAN_DISCOVERED', ?, ?)
                    """,
                    (str(uuid.uuid4()), parent_id, run_id, now),
                )
            for resource in asset.resources:
                connection.execute(
                    """
                    INSERT INTO asset_resources(
                        id, asset_id, resource_key, resource_type, uti, original_name,
                        archive_name, source_ref, expected_size, required,
                        source_relative_path, expected_mtime_ns, expected_device, expected_inode,
                        source_sha256, source_quickxor
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(asset_id, resource_key) DO NOTHING
                    """,
                    (
                        resource_id(parent_id, resource.resource_key),
                        parent_id,
                        resource.resource_key,
                        resource.resource_type,
                        resource.uti,
                        resource.original_name,
                        names[resource.resource_key],
                        str(resource.source_path),
                        resource.expected_size
                        if resource.expected_size is not None
                        else resource.source_path.stat().st_size,
                        int(resource.required),
                        resource.source_relative_path,
                        resource.expected_mtime_ns,
                        resource.expected_device,
                        resource.expected_inode,
                        resource.source_sha256,
                        resource.source_quickxor,
                    ),
                )
            connection.execute(
                """
                INSERT INTO job_assets(job_id, asset_id, action)
                VALUES (?, ?, 'ARCHIVE')
                ON CONFLICT(job_id, asset_id) DO NOTHING
                """,
                (job_id, parent_id),
            )
            connection.execute("COMMIT")
        return parent_id

    def list_assets(self, job_id: str) -> Sequence[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT assets.*
                FROM assets
                JOIN job_assets ON job_assets.asset_id = assets.id
                WHERE job_assets.job_id = ?
                ORDER BY assets.creation_at_utc, assets.id
                """,
                (job_id,),
            ).fetchall()

    def get_asset(self, parent_asset_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM assets WHERE id = ?", (parent_asset_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown asset: {parent_asset_id}")
        return cast(sqlite3.Row, row)

    def list_resources(self, parent_asset_id: str) -> Sequence[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM asset_resources WHERE asset_id = ? ORDER BY resource_key",
                (parent_asset_id,),
            ).fetchall()

    def asset_metadata_warnings(self, parent_asset_id: str) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT batch_files.metadata_warning
                FROM batch_files
                JOIN asset_resources ON asset_resources.id = batch_files.resource_id
                WHERE asset_resources.asset_id = ?
                  AND batch_files.metadata_warning IS NOT NULL
                ORDER BY batch_files.metadata_warning
                """,
                (parent_asset_id,),
            ).fetchall()
        return [str(row["metadata_warning"]) for row in rows]

    def transition(
        self,
        parent_asset_id: str,
        target: AssetState,
        reason: str,
        run_id: str,
        *,
        error_code: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, last_stable_state FROM assets WHERE id = ?",
                (parent_asset_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise KeyError(f"unknown asset: {parent_asset_id}")
            current = AssetState(row["state"])
            last_stable = AssetState(row["last_stable_state"]) if row["last_stable_state"] else None
            validate_transition(current, target, last_stable_state=last_stable)
            stable_value = current.value if target is AssetState.FAILED else target.value
            connection.execute(
                """
                UPDATE assets
                SET state = ?, last_stable_state = ?, error_code = ?, updated_at = ?
                WHERE id = ?
                """,
                (target.value, stable_value, error_code, now, parent_asset_id),
            )
            connection.execute(
                """
                INSERT INTO state_transitions(
                    id, asset_id, from_state, to_state, reason, run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    parent_asset_id,
                    current.value,
                    target.value,
                    reason,
                    run_id,
                    now,
                ),
            )
            connection.execute("COMMIT")

    def update_resource_export(
        self,
        resource_record_id: str,
        size: int,
        sha256: str,
        quickxor: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE asset_resources
                SET actual_size = ?, sha256 = ?, quickxor = ?
                WHERE id = ?
                """,
                (size, sha256, quickxor, resource_record_id),
            )
            connection.execute("COMMIT")

    def set_asset_fingerprint(self, parent_asset_id: str, fingerprint: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE assets SET fingerprint_sha256 = ?, updated_at = ? WHERE id = ?",
                (fingerprint, utc_now(), parent_asset_id),
            )
            connection.execute("COMMIT")

    def begin_operation(
        self,
        run_id: str,
        parent_asset_id: str,
        resource_record_id: str | None,
        kind: str,
        idempotency_key: str,
        request: dict[str, Any],
    ) -> sqlite3.Row:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO operations(
                    id, run_id, asset_id, resource_id, kind, idempotency_key,
                    status, request_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'INTENT', ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    run_id,
                    parent_asset_id,
                    resource_record_id,
                    kind,
                    idempotency_key,
                    json.dumps(request, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM operations WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            connection.execute("COMMIT")
        assert row is not None
        return cast(sqlite3.Row, row)

    def finish_operation(
        self,
        idempotency_key: str,
        status: str,
        result: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE operations
                SET status = ?, result_json = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (status, json.dumps(result, sort_keys=True), utc_now(), idempotency_key),
            )
            connection.execute("COMMIT")

    def upsert_archive_file(
        self,
        resource_record_id: str,
        local_path: Path,
        remote: RemoteObject,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO archive_files(
                    id, resource_id, local_path, remote_path, drive_item_id,
                    etag, quickxor, size, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_id) DO UPDATE SET
                    local_path = excluded.local_path,
                    remote_path = excluded.remote_path,
                    drive_item_id = excluded.drive_item_id,
                    etag = excluded.etag,
                    quickxor = excluded.quickxor,
                    size = excluded.size,
                    updated_at = excluded.updated_at
                """,
                (
                    str(uuid.uuid4()),
                    resource_record_id,
                    str(local_path),
                    remote.remote_path,
                    remote.drive_item_id,
                    remote.etag,
                    remote.quickxor,
                    remote.size,
                    utc_now(),
                ),
            )
            connection.execute("COMMIT")

    def get_archive_file(self, resource_record_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM archive_files WHERE resource_id = ?", (resource_record_id,)
            ).fetchone()
        return cast(sqlite3.Row | None, row)

    def get_upload_session(self, remote_path: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM upload_sessions WHERE remote_path = ?", (remote_path,)
            ).fetchone()
        return cast(sqlite3.Row | None, row)

    def save_upload_session(
        self,
        session_id: str,
        remote_path: str,
        expected_size: int,
        next_start: int,
        expiration_at: str | None,
        *,
        resource_record_id: str | None,
        status: str = "ACTIVE",
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO upload_sessions(
                    id, resource_id, remote_path, expected_size, next_start,
                    expiration_at, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(remote_path) DO UPDATE SET
                    id = excluded.id,
                    resource_id = excluded.resource_id,
                    expected_size = excluded.expected_size,
                    next_start = excluded.next_start,
                    expiration_at = excluded.expiration_at,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    resource_record_id,
                    remote_path,
                    expected_size,
                    next_start,
                    expiration_at,
                    status,
                    utc_now(),
                ),
            )
            connection.execute("COMMIT")

    def delete_upload_session(self, remote_path: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM upload_sessions WHERE remote_path = ?", (remote_path,))
            connection.execute("COMMIT")

    def record_verification(
        self,
        resource_record_id: str,
        check_type: str,
        expected: str,
        actual: str,
        passed: bool,
        run_id: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO verification_records(
                    id, resource_id, check_type, expected, actual, passed, checked_at, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_id, check_type, run_id) DO UPDATE SET
                    expected = excluded.expected,
                    actual = excluded.actual,
                    passed = excluded.passed,
                    checked_at = excluded.checked_at
                """,
                (
                    str(uuid.uuid4()),
                    resource_record_id,
                    check_type,
                    expected,
                    actual,
                    int(passed),
                    utc_now(),
                    run_id,
                ),
            )
            connection.execute("COMMIT")

    def set_review_required(
        self,
        parent_asset_id: str,
        required: bool,
        error_code: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE assets
                SET review_required = ?, error_code = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(required), error_code, utc_now(), parent_asset_id),
            )
            connection.execute("COMMIT")

    def event(
        self,
        run_id: str,
        level: str,
        event_code: str,
        *,
        job_id: str | None = None,
        asset_key: str | None = None,
        duration_ms: int | None = None,
        retry: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO run_events(
                    id, run_id, level, event_code, job_id, asset_key,
                    duration_ms, retry, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    run_id,
                    level,
                    event_code,
                    job_id,
                    asset_key,
                    duration_ms,
                    retry,
                    json.dumps(details or {}, sort_keys=True),
                    utc_now(),
                ),
            )

    def state_counts(self, job_id: str) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT assets.state, COUNT(*) AS count
                FROM assets
                JOIN job_assets ON job_assets.asset_id = assets.id
                WHERE job_assets.job_id = ?
                GROUP BY assets.state
                """,
                (job_id,),
            ).fetchall()
        return {row["state"]: row["count"] for row in rows}

    def review_count(self, job_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM assets
                JOIN job_assets ON job_assets.asset_id = assets.id
                WHERE job_assets.job_id = ? AND assets.review_required = 1
                """,
                (job_id,),
            ).fetchone()
        assert row is not None
        return int(row["count"])

    def set_job_asset_result(self, job_id: str, parent_asset_id: str, result: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE job_assets SET result = ? WHERE job_id = ? AND asset_id = ?",
                (result, job_id, parent_asset_id),
            )
