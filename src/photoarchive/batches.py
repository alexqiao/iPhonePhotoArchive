from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from photoarchive.config import AppConfig
from photoarchive.database import Database
from photoarchive.domain import BatchNotReadyError, ConflictError, IntegrityError
from photoarchive.folder_source import FolderArchiveSource, SwiftMetadataReader
from photoarchive.paths import ensure_private_directory, safe_path

PROFILE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,31})$")


def _write_marker(path: Path, payload: dict[str, object]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    partial = path.with_name(path.name + ".partial")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(partial, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.replace(partial, path)


class BatchManager:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.root = config.folder_import.inbox_root

    def add_profile(self, profile_id: str, display_name: str) -> Path:
        if not PROFILE_ID.fullmatch(profile_id):
            raise ValueError(
                "profile id must contain 1-32 lowercase letters, digits, hyphens, or underscores"
            )
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("profile display name cannot be empty")
        self.database.migrate()
        self.database.create_profile(profile_id, display_name)
        root = ensure_private_directory(self.root)
        profile_root = safe_path(root, profile_id)
        for name in ("pending", "completed"):
            ensure_private_directory(profile_root / name)
        return profile_root

    def prepare(
        self, profile_id: str, *, open_image_capture: bool | None = None
    ) -> tuple[str, Path]:
        self.database.migrate()
        self.database.get_profile(profile_id)
        now = datetime.now(ZoneInfo(self.config.timezone))
        batch_id = f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        source_relative = str(Path(profile_id, "pending", batch_id))
        completed_relative = str(Path(profile_id, "completed", batch_id))
        source = safe_path(self.root, source_relative)
        source.mkdir(mode=0o700, parents=True, exist_ok=False)
        _write_marker(
            source / ".photoarchive-batch.json",
            {
                "schema_version": 1,
                "batch_id": batch_id,
                "profile_id": profile_id,
                "created_at": now.isoformat(),
            },
        )
        self.database.create_batch(batch_id, profile_id, source_relative, completed_relative)
        should_open = (
            self.config.folder_import.open_image_capture
            if open_image_capture is None
            else open_image_capture
        )
        if should_open:
            try:
                subprocess.run(["open", "-a", "Image Capture"], check=False, timeout=10)
                subprocess.run(["open", str(source)], check=False, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        return batch_id, source

    def prepare_phone(
        self,
        profile_id: str,
        *,
        device_id: str,
        cutoff_at_utc: str,
        icloud_photos_enabled: bool | None,
    ) -> tuple[str, Path]:
        batch_id, source = self.prepare(profile_id, open_image_capture=False)
        self.database.create_phone_batch(
            batch_id,
            device_id,
            cutoff_at_utc,
            icloud_photos_enabled,
        )
        marker = source / ".photoarchive-batch.json"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload.update(
            {
                "schema_version": 2,
                "source": "iphone",
                "cutoff_at_utc": cutoff_at_utc,
            }
        )
        _write_marker(marker, payload)
        return batch_id, source

    def batch_path(self, batch_id: str) -> Path:
        batch = self.database.get_batch(batch_id)
        completed = safe_path(self.root, str(batch["completed_relative_path"]))
        source = safe_path(self.root, str(batch["source_relative_path"]))
        if completed.is_dir() and not source.exists():
            return completed
        return source

    def source(self, batch_id: str) -> FolderArchiveSource:
        batch = self.database.get_batch(batch_id)
        return FolderArchiveSource(
            self.batch_path(batch_id),
            profile_id=str(batch["profile_id"]),
            batch_id=batch_id,
            config=self.config.folder_import,
            metadata_reader=SwiftMetadataReader(self.config.media_helper.helper_path),
        )

    @staticmethod
    def _marker_matches(path: Path, batch_id: str, profile_id: str) -> bool:
        marker = path / ".photoarchive-batch.json"
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        marker_batch = payload.get("batch_id")
        marker_profile = payload.get("profile_id")
        return (
            isinstance(marker_batch, str)
            and isinstance(marker_profile, str)
            and (marker_batch == batch_id and marker_profile == profile_id)
        )

    def finalize(self, batch_id: str) -> Path:
        batch = self.database.get_batch(batch_id)
        job_id = batch["job_id"]
        if not job_id:
            raise BatchNotReadyError("batch has no archive job")
        counts = self.database.state_counts(str(job_id))
        if not counts or set(counts) != {"SAFE_TO_DELETE"}:
            raise BatchNotReadyError("every file must pass verification before the batch can move")
        if self.database.review_count(str(job_id)):
            raise BatchNotReadyError("batch still has items that require review")

        source = safe_path(self.root, str(batch["source_relative_path"]))
        completed = safe_path(self.root, str(batch["completed_relative_path"]))
        operation_key = f"finalize:{batch_id}"
        if completed.exists():
            if source.exists() or not self._marker_matches(
                completed, batch_id, str(batch["profile_id"])
            ):
                raise ConflictError("completed batch path already contains different content")
            self.database.begin_batch_operation(batch_id, "MOVE_TO_COMPLETED", operation_key)
            self.database.finish_batch_operation(
                operation_key,
                "SUCCEEDED",
                {
                    "completed_relative_path": batch["completed_relative_path"],
                    "reconciled": True,
                },
            )
            self.database.set_batch_state(batch_id, "COMPLETED")
            return completed
        if not source.is_dir() or not self._marker_matches(
            source, batch_id, str(batch["profile_id"])
        ):
            raise IntegrityError("pending batch marker is missing or does not match")
        completed.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if source.stat().st_dev != completed.parent.stat().st_dev:
            raise IntegrityError("pending and completed directories must use the same filesystem")

        operation = self.database.begin_batch_operation(
            batch_id, "MOVE_TO_COMPLETED", operation_key
        )
        if operation["status"] == "SUCCEEDED":
            if completed.is_dir() and not source.exists():
                self.database.set_batch_state(batch_id, "COMPLETED")
                return completed
            raise IntegrityError("completed batch operation does not match the filesystem")
        os.replace(source, completed)
        directory_descriptor = os.open(completed.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        self.database.finish_batch_operation(
            operation_key,
            "SUCCEEDED",
            {"completed_relative_path": batch["completed_relative_path"]},
        )
        self.database.set_batch_state(batch_id, "COMPLETED")
        return completed
