from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import partial
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from photoarchive.audit import AuditLogger
from photoarchive.config import AppConfig
from photoarchive.database import Database
from photoarchive.domain import (
    AssetState,
    ConfigDriftError,
    ConflictError,
    DiscoveredAsset,
    DiscoveredResource,
    DiscoveryRequest,
    IntegrityError,
    PhotoArchiveError,
    UploadRequest,
)
from photoarchive.hashing import hash_file
from photoarchive.paths import archive_asset_directory, resource_id, safe_path
from photoarchive.protocols import ArchiveSource, ArchiveTarget


class InjectedCrash(BaseException):
    """Test-only process interruption raised after a committed state."""


T = TypeVar("T")
ProgressCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class PlanSummary:
    candidate_assets: int
    required_resources: int
    candidate_bytes: int
    cutoff_at_utc: str
    selection_mode: str = "cutoff"
    job_id: str | None = None


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink():
            raise IntegrityError("atomic write target is a symbolic link")
        if path.read_bytes() == payload:
            return
        raise ConflictError(f"existing file differs: {path.name}")
    partial = path.with_name(path.name + ".partial")
    if partial.exists() and partial.is_symlink():
        raise IntegrityError("atomic write partial target is a symbolic link")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(partial, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.replace(partial, path)
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


class Planner:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        source: ArchiveSource,
        *,
        candidate_limit: int | None = None,
        target_adapter: str | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.source = source
        self.candidate_limit = candidate_limit
        self.target_adapter = target_adapter

    def _assets(self) -> Iterable[DiscoveredAsset]:
        request = DiscoveryRequest(
            cutoff_at_utc=self.config.cutoff_at_utc(),
            media_types=frozenset(self.config.archive_policy.media_types),
            batch_size=self.config.archive_policy.batch_size,
        )
        assets = self.source.discover(request)
        return islice(assets, self.candidate_limit) if self.candidate_limit else assets

    def _summary(self, assets: list[DiscoveredAsset]) -> PlanSummary:
        resources = [
            resource for asset in assets for resource in asset.resources if resource.required
        ]
        return PlanSummary(
            candidate_assets=len(assets),
            required_resources=len(resources),
            candidate_bytes=sum(
                resource.expected_size
                if resource.expected_size is not None
                else resource.source_path.stat().st_size
                for resource in resources
            ),
            cutoff_at_utc=self.config.cutoff_at_utc().isoformat().replace("+00:00", "Z"),
            selection_mode=("entire_batch" if self.source.adapter_name == "folder" else "cutoff"),
        )

    def preview(self) -> PlanSummary:
        assets = list(self._assets())
        return self._summary(assets)

    def create_job(self) -> PlanSummary:
        self.database.migrate()
        assets = list(self._assets())
        summary = self._summary(assets)
        job_id = self.database.create_job(
            summary.cutoff_at_utc,
            self.config.fingerprint(),
            self.source.source_reference,
            source_adapter=self.source.adapter_name,
            target_adapter=self.target_adapter or "fake",
            profile_id=getattr(self.source, "profile_id", None),
            batch_id=getattr(self.source, "batch_id", None),
        )
        self._persist_assets(job_id, assets)
        return PlanSummary(**{**asdict(summary), "job_id": job_id})

    def refresh_job(self, job_id: str) -> PlanSummary:
        job = self.database.get_job(job_id)
        if job["config_hash"] != self.config.fingerprint():
            raise ConfigDriftError("current configuration does not match the saved job")
        if (job["source_adapter"] or job["photos_adapter"]) != self.source.adapter_name:
            raise IntegrityError("archive source does not match the saved job")
        assets = list(self._assets())
        self._persist_assets(job_id, assets)
        summary = self._summary(assets)
        return PlanSummary(**{**asdict(summary), "job_id": job_id})

    def _persist_assets(self, job_id: str, assets: list[DiscoveredAsset]) -> None:
        run_id = str(uuid.uuid4())
        for asset in assets:
            parent_id = self.database.persist_asset(job_id, asset, run_id)
            if asset.batch_id:
                warning = ";".join(asset.metadata_warnings) or None
                for item in asset.resources:
                    self.database.record_batch_file(
                        asset.batch_id,
                        resource_id(parent_id, item.resource_key),
                        item.source_relative_path or item.original_name,
                        device=int(item.expected_device or 0),
                        inode=int(item.expected_inode or 0),
                        size=int(item.expected_size or 0),
                        mtime_ns=int(item.expected_mtime_ns or 0),
                        metadata_warning=warning,
                    )


class ArchiveRunner:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        source: ArchiveSource,
        target: ArchiveTarget,
        *,
        sleeper: Callable[[float], None] | None = None,
        fail_after_state: AssetState | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.source = source
        self.target = target
        self.sleeper = sleeper or time.sleep
        self.fail_after_state = fail_after_state
        self.progress = progress
        self.audit = AuditLogger(
            config.storage.database_path.parent / "logs" / "photoarchive.jsonl"
        )
        self.run_id = str(uuid.uuid4())

    def _progress(self, event: str, **details: Any) -> None:
        if self.progress is not None:
            self.progress(event, details)

    def _emit(
        self,
        event_code: str,
        *,
        level: str = "INFO",
        job_id: str,
        asset_key: str | None = None,
        details: dict[str, Any] | None = None,
        retry: int = 0,
    ) -> None:
        self.database.event(
            self.run_id,
            level,
            event_code,
            job_id=job_id,
            asset_key=asset_key,
            details=details,
            retry=retry,
        )
        self.audit.write(
            level=level,
            event_code=event_code,
            run_id=self.run_id,
            job_id=job_id,
            asset_key=asset_key,
            details=details,
            retry=retry,
        )

    def _with_retry(
        self,
        operation: Callable[[], T],
        *,
        job_id: str,
        asset_key: str,
    ) -> T:
        for attempt in range(1, self.config.retry.max_attempts + 1):
            try:
                return operation()
            except (OSError, TimeoutError) as exc:
                if attempt >= self.config.retry.max_attempts:
                    raise IntegrityError("external I/O exhausted configured retries") from exc
                retry_after = getattr(exc, "retry_after", None)
                delay = min(
                    self.config.retry.max_delay_sec,
                    retry_after
                    if isinstance(retry_after, (int, float))
                    else self.config.retry.base_delay_sec * (2 ** (attempt - 1)),
                )
                self._emit(
                    "EXTERNAL_IO_RETRY",
                    level="WARNING",
                    job_id=job_id,
                    asset_key=asset_key,
                    retry=attempt,
                )
                self.sleeper(delay)
        raise AssertionError("retry loop terminated unexpectedly")

    def _after_transition(self, state: AssetState) -> None:
        if self.fail_after_state is state:
            raise InjectedCrash(f"injected crash after {state}")

    def _stable_poll_interval(self) -> float:
        target_interval = getattr(self.target, "stable_poll_interval_sec", None)
        if isinstance(target_interval, (int, float)) and target_interval >= 0:
            return float(target_interval)
        return self.config.verification.stable_poll_interval_sec

    def run_job(self, job_id: str, *, resume: bool = False) -> dict[str, int]:
        job = self.database.get_job(job_id)
        if job["config_hash"] != self.config.fingerprint():
            raise ConfigDriftError("current configuration does not match the saved job")
        self.database.set_job_status(job_id, "RUNNING")
        self._emit("RUN_STARTED", job_id=job_id, details={"resume": resume})
        assets = list(self.database.list_assets(job_id))
        self._progress("ARCHIVE_RUN_STARTED", total=len(assets), resume=resume)
        try:
            for index, asset in enumerate(assets, start=1):
                self._progress(
                    "ARCHIVE_ASSET_STARTED",
                    current=index,
                    total=len(assets),
                    asset_key=str(asset["id"])[:8],
                )
                self._process_asset(
                    job_id,
                    asset,
                    resume=resume,
                    archive_current=index,
                    archive_total=len(assets),
                )
                self._progress(
                    "ARCHIVE_ASSET_COMPLETED",
                    current=index,
                    total=len(assets),
                    asset_key=str(asset["id"])[:8],
                    state=str(self.database.get_asset(asset["id"])["state"]),
                )
        except InjectedCrash:
            self._emit("RUN_INTERRUPTED", level="WARNING", job_id=job_id)
            raise
        counts = self.database.state_counts(job_id)
        has_errors = (
            counts.get(AssetState.FAILED.value, 0) > 0 or self.database.review_count(job_id) > 0
        )
        self.database.set_job_status(
            job_id,
            "COMPLETED_WITH_ERRORS" if has_errors else "COMPLETED",
        )
        self._emit("RUN_COMPLETED", job_id=job_id, details={"counts": counts})
        self._progress("ARCHIVE_RUN_COMPLETED", counts=counts)
        return counts

    def _process_asset(
        self,
        job_id: str,
        asset: sqlite3.Row,
        *,
        resume: bool,
        archive_current: int | None = None,
        archive_total: int | None = None,
    ) -> None:
        short_key = str(asset["id"])[:8]
        try:
            current = AssetState(asset["state"])
            if current is AssetState.FAILED:
                if not resume:
                    return
                self._recover_asset(job_id, asset)
                asset = self.database.get_asset(asset["id"])
                current = AssetState(asset["state"])
            if current is AssetState.DISCOVERED:
                if int(asset["unresolved_warning_count"]) > 0:
                    raise IntegrityError("required resource set is incomplete")
                self._progress(
                    "ARCHIVE_STAGE",
                    asset_key=short_key,
                    stage="export",
                    archive_current=archive_current,
                    archive_total=archive_total,
                )
                self._export_asset(job_id, asset)
                current = AssetState.EXPORTED
            if current is AssetState.EXPORTED:
                self._progress(
                    "ARCHIVE_STAGE",
                    asset_key=short_key,
                    stage="copy",
                    archive_current=archive_current,
                    archive_total=archive_total,
                )
                self._upload_asset(job_id, self.database.get_asset(asset["id"]))
                current = AssetState.UPLOADED
            if current is AssetState.UPLOADED or current is AssetState.VERIFIED:
                self._progress(
                    "ARCHIVE_STAGE",
                    asset_key=short_key,
                    stage="verify",
                    archive_current=archive_current,
                    archive_total=archive_total,
                )
                self._verify_asset(
                    job_id,
                    self.database.get_asset(asset["id"]),
                    archive_current=archive_current,
                    archive_total=archive_total,
                )
            if (
                AssetState(self.database.get_asset(asset["id"])["state"])
                is AssetState.SAFE_TO_DELETE
            ):
                self.database.set_job_asset_result(job_id, asset["id"], "SAFE_TO_DELETE")
                if bool(getattr(self.source, "remove_staged_after_verify", False)):
                    self._remove_verified_stage(job_id, str(asset["id"]))
        except InjectedCrash:
            raise
        except PhotoArchiveError as exc:
            current_row = self.database.get_asset(asset["id"])
            current_state = AssetState(current_row["state"])
            if current_state in {
                AssetState.DISCOVERED,
                AssetState.EXPORTED,
                AssetState.UPLOADED,
                AssetState.VERIFIED,
            }:
                self.database.transition(
                    asset["id"],
                    AssetState.FAILED,
                    exc.code,
                    self.run_id,
                    error_code=exc.code,
                )
            else:
                self.database.set_review_required(asset["id"], True, exc.code)
            self.database.set_job_asset_result(job_id, asset["id"], exc.code)
            self._emit(
                exc.code,
                level="ERROR",
                job_id=job_id,
                asset_key=short_key,
            )
        except Exception:
            current_row = self.database.get_asset(asset["id"])
            current_state = AssetState(current_row["state"])
            if current_state in {
                AssetState.DISCOVERED,
                AssetState.EXPORTED,
                AssetState.UPLOADED,
                AssetState.VERIFIED,
            }:
                self.database.transition(
                    asset["id"],
                    AssetState.FAILED,
                    "INTERNAL_ERROR",
                    self.run_id,
                    error_code="INTERNAL_ERROR",
                )
            else:
                self.database.set_review_required(asset["id"], True, "INTERNAL_ERROR")
            self.database.set_job_asset_result(job_id, asset["id"], "INTERNAL_ERROR")
            self._emit(
                "INTERNAL_ERROR",
                level="ERROR",
                job_id=job_id,
                asset_key=short_key,
            )

    def _asset_relative_directory(self, asset: sqlite3.Row) -> PurePosixPath:
        created = str(asset["creation_at_utc"])
        creation_at = time_from_iso(created)
        return archive_asset_directory(
            creation_at,
            self.config.timezone,
            str(asset["id"])[:8],
        )

    def _stage_directory(self, job_id: str, asset_id: str) -> Path:
        relative = Path(job_id, asset_id)
        directory = safe_path(self.config.storage.staging_root, relative)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return safe_path(self.config.storage.staging_root, relative)

    def _remove_verified_stage(self, job_id: str, parent_asset_id: str) -> None:
        stage = safe_path(self.config.storage.staging_root, Path(job_id, parent_asset_id))
        for resource in self.database.list_resources(parent_asset_id):
            safe_path(stage, str(resource["archive_name"])).unlink(missing_ok=True)
        safe_path(stage, "manifest.json").unlink(missing_ok=True)
        if stage.exists():
            stage.rmdir()
        job_stage = safe_path(self.config.storage.staging_root, job_id)
        if job_stage.exists() and not any(job_stage.iterdir()):
            job_stage.rmdir()

    @staticmethod
    def _resource_from_row(resource: sqlite3.Row) -> DiscoveredResource:
        return DiscoveredResource(
            resource_key=resource["resource_key"],
            resource_type=resource["resource_type"],
            uti=resource["uti"],
            original_name=resource["original_name"],
            required=bool(resource["required"]),
            source_path=Path(resource["source_ref"]),
            expected_size=int(resource["expected_size"]),
            expected_mtime_ns=resource["expected_mtime_ns"],
            expected_device=resource["expected_device"],
            expected_inode=resource["expected_inode"],
            source_relative_path=resource["source_relative_path"],
            source_sha256=resource["source_sha256"],
            source_quickxor=resource["source_quickxor"],
        )

    def _export_asset(self, job_id: str, asset: sqlite3.Row) -> None:
        stage = self._stage_directory(job_id, asset["id"])
        resources = self.database.list_resources(asset["id"])
        for resource in resources:
            final_path = safe_path(stage, resource["archive_name"])
            partial_path = safe_path(stage, resource["archive_name"] + ".partial")
            operation_key = f"export:{job_id}:{resource['id']}"
            self.database.begin_operation(
                self.run_id,
                asset["id"],
                resource["id"],
                "EXPORT",
                operation_key,
                {"resource_key": resource["resource_key"]},
            )
            if final_path.exists():
                size, sha256, quickxor = hash_file(final_path)
                if resource["sha256"] and (
                    size != resource["actual_size"] or sha256 != resource["sha256"]
                ):
                    raise IntegrityError("existing staged resource differs from database evidence")
            else:
                receipt = self._with_retry(
                    partial(
                        self.source.export,
                        self._resource_from_row(resource),
                        partial_path,
                    ),
                    job_id=job_id,
                    asset_key=str(asset["id"])[:8],
                )
                if int(resource["expected_size"]) > 0 and receipt.size != resource["expected_size"]:
                    self.database.finish_operation(
                        operation_key,
                        "FAILED",
                        {"error_code": "EXPORT_SIZE_MISMATCH"},
                    )
                    raise IntegrityError("exported resource size differs from discovery evidence")
                os.replace(partial_path, final_path)
                directory_descriptor = os.open(stage, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
                size, sha256, quickxor = receipt.size, receipt.sha256, receipt.quickxor
            self.database.update_resource_export(resource["id"], size, sha256, quickxor)
            self.database.finish_operation(
                operation_key,
                "SUCCEEDED",
                {"size": size, "sha256": sha256, "quickxor": quickxor},
            )
        refreshed = self.database.list_resources(asset["id"])
        fingerprint_source = [
            f"{row['resource_key']}:{row['sha256']}:{row['actual_size']}"
            for row in refreshed
            if row["required"]
        ]
        fingerprint = hashlib.sha256("\n".join(fingerprint_source).encode()).hexdigest()
        created = time_from_iso(asset["creation_at_utc"])
        local_created = created.astimezone(ZoneInfo(self.config.timezone))
        job = self.database.get_job(job_id)
        manifest = {
            "schema_version": 2 if job["source_adapter"] == "folder" else 1,
            "asset": {
                "asset_key": str(asset["id"])[:8],
                "library_id": asset["library_id"],
                "photos_local_id": asset["photos_local_id"],
                "creation_at_utc": created.isoformat().replace("+00:00", "Z"),
                "creation_at_local": local_created.isoformat(),
                "media_type": asset["media_type"],
                "fingerprint_sha256": fingerprint,
                "profile_id": job["profile_id"],
                "batch_id": job["batch_id"],
                "metadata_warnings": self.database.asset_metadata_warnings(asset["id"])
                if job["source_adapter"] == "folder"
                else [],
            },
            "resources": [
                {
                    "resource_key": row["resource_key"],
                    "resource_type": row["resource_type"],
                    "uti": row["uti"],
                    "original_name": row["original_name"],
                    "archive_name": row["archive_name"],
                    "required": bool(row["required"]),
                    "size": row["actual_size"],
                    "sha256": row["sha256"],
                    "quickxor": row["quickxor"],
                    "source_relative_path": row["source_relative_path"],
                }
                for row in refreshed
            ],
            "exporter": {
                "name": "photoarchive",
                "version": "0.5.0",
                "exported_at_utc": asset["created_at"],
            },
        }
        _atomic_write(safe_path(stage, "manifest.json"), _canonical_json_bytes(manifest))
        self.database.set_asset_fingerprint(asset["id"], fingerprint)
        self.database.transition(asset["id"], AssetState.EXPORTED, "EXPORT_COMPLETE", self.run_id)
        self._after_transition(AssetState.EXPORTED)

    def _upload_asset(self, job_id: str, asset: sqlite3.Row) -> None:
        stage = self._stage_directory(job_id, asset["id"])
        archive_prefix = getattr(self.target, "archive_prefix", "")
        job = self.database.get_job(job_id)
        profile_prefix = str(job["profile_id"] or "")
        remote_directory = (
            PurePosixPath(archive_prefix) / profile_prefix / self._asset_relative_directory(asset)
        )
        resources = self.database.list_resources(asset["id"])
        for resource in resources:
            local_path = safe_path(stage, resource["archive_name"])
            remote_path = str(remote_directory / resource["archive_name"])
            operation_key = f"upload:{job_id}:{resource['id']}"
            self.database.begin_operation(
                self.run_id,
                asset["id"],
                resource["id"],
                "UPLOAD",
                operation_key,
                {"remote_path": remote_path},
            )
            upload_request = UploadRequest(
                local_path=local_path,
                remote_path=remote_path,
                expected_size=int(resource["actual_size"]),
                expected_sha256=str(resource["sha256"]),
                expected_quickxor=str(resource["quickxor"]),
                resource_id=str(resource["id"]),
            )
            remote = self._with_retry(
                partial(self.target.put, upload_request),
                job_id=job_id,
                asset_key=str(asset["id"])[:8],
            )
            self.database.upsert_archive_file(resource["id"], local_path, remote)
            self.database.finish_operation(
                operation_key,
                "SUCCEEDED",
                {
                    "drive_item_id": remote.drive_item_id,
                    "size": remote.size,
                    "etag": remote.etag,
                    "quickxor": remote.quickxor,
                },
            )
        manifest_path = safe_path(stage, "manifest.json")
        size, sha256, quickxor = hash_file(manifest_path)
        manifest_remote = str(remote_directory / "manifest.json")
        manifest_key = f"upload:{job_id}:{asset['id']}:manifest"
        self.database.begin_operation(
            self.run_id,
            asset["id"],
            None,
            "UPLOAD_MANIFEST",
            manifest_key,
            {"remote_path": manifest_remote},
        )
        manifest_request = UploadRequest(
            manifest_path,
            manifest_remote,
            size,
            sha256,
            quickxor,
            f"manifest:{asset['id']}",
        )
        remote_manifest = self._with_retry(
            partial(self.target.put, manifest_request),
            job_id=job_id,
            asset_key=str(asset["id"])[:8],
        )
        self.database.finish_operation(
            manifest_key,
            "SUCCEEDED",
            {"drive_item_id": remote_manifest.drive_item_id, "etag": remote_manifest.etag},
        )
        self.database.transition(asset["id"], AssetState.UPLOADED, "REMOTE_COMMITTED", self.run_id)
        self._after_transition(AssetState.UPLOADED)

    def _verify_asset(
        self,
        job_id: str,
        asset: sqlite3.Row,
        *,
        archive_current: int | None = None,
        archive_total: int | None = None,
    ) -> bool:
        resources = [row for row in self.database.list_resources(asset["id"]) if row["required"]]
        all_passed = bool(resources)
        stable_poll_interval = self._stable_poll_interval()
        for resource in resources:
            archived = self.database.get_archive_file(resource["id"])
            if archived is None:
                all_passed = False
                continue
            observations = []
            for poll_index in range(self.config.verification.stable_poll_count):
                self._progress(
                    "ARCHIVE_VERIFY_POLL",
                    asset_key=str(asset["id"])[:8],
                    current=poll_index + 1,
                    total=self.config.verification.stable_poll_count,
                    archive_current=archive_current,
                    archive_total=archive_total,
                    wait_seconds=(
                        stable_poll_interval
                        if poll_index + 1 < self.config.verification.stable_poll_count
                        else 0
                    ),
                )
                remote = self._with_retry(
                    partial(self.target.stat, str(archived["remote_path"])),
                    job_id=job_id,
                    asset_key=str(asset["id"])[:8],
                )
                observations.append(
                    (
                        remote.drive_item_id,
                        remote.remote_path,
                        remote.size,
                        remote.etag,
                        remote.quickxor,
                    )
                )
                if poll_index + 1 < self.config.verification.stable_poll_count:
                    self.sleeper(stable_poll_interval)
            latest = observations[-1]
            checks = {
                "REMOTE_PATH": (str(archived["remote_path"]), str(latest[1])),
                "REMOTE_SIZE": (str(resource["actual_size"]), str(latest[2])),
                "REMOTE_SHA256": (str(resource["sha256"]), str(latest[3])),
                "REMOTE_QUICKXOR": (str(resource["quickxor"]), str(latest[4])),
                "REMOTE_STABLE": ("stable", "stable" if len(set(observations)) == 1 else "changed"),
            }
            for check_type, (expected, actual) in checks.items():
                passed = expected == actual
                all_passed = all_passed and passed
                self.database.record_verification(
                    resource["id"], check_type, expected, actual, passed, self.run_id
                )
        current = AssetState(self.database.get_asset(asset["id"])["state"])
        if not all_passed or int(asset["unresolved_warning_count"]) > 0:
            if current is AssetState.SAFE_TO_DELETE:
                self.database.set_review_required(asset["id"], True, "REMOTE_MISMATCH")
            elif current in {AssetState.UPLOADED, AssetState.VERIFIED}:
                self.database.transition(
                    asset["id"],
                    AssetState.FAILED,
                    "REMOTE_MISMATCH",
                    self.run_id,
                    error_code="REMOTE_MISMATCH",
                )
            return False
        self.database.set_review_required(asset["id"], False, None)
        if current is AssetState.UPLOADED:
            self.database.transition(
                asset["id"], AssetState.VERIFIED, "ALL_REQUIRED_CHECKS_PASS", self.run_id
            )
            self._after_transition(AssetState.VERIFIED)
            current = AssetState.VERIFIED
        if current is AssetState.VERIFIED:
            self.database.transition(
                asset["id"], AssetState.SAFE_TO_DELETE, "ASSET_GATE_PASS", self.run_id
            )
            self._after_transition(AssetState.SAFE_TO_DELETE)
        self._emit("ASSET_VERIFIED", job_id=job_id, asset_key=str(asset["id"])[:8])
        return True

    def verify_job(self, job_id: str) -> dict[str, int]:
        job = self.database.get_job(job_id)
        if job["config_hash"] != self.config.fingerprint():
            raise ConfigDriftError("current configuration does not match the saved job")
        assets = list(self.database.list_assets(job_id))
        for index, asset in enumerate(assets, start=1):
            state = AssetState(asset["state"])
            if state in {AssetState.UPLOADED, AssetState.VERIFIED, AssetState.SAFE_TO_DELETE}:
                try:
                    self._verify_asset(
                        job_id,
                        asset,
                        archive_current=index,
                        archive_total=len(assets),
                    )
                except PhotoArchiveError as exc:
                    if state is AssetState.SAFE_TO_DELETE:
                        self.database.set_review_required(asset["id"], True, exc.code)
                    else:
                        self.database.transition(
                            asset["id"],
                            AssetState.FAILED,
                            exc.code,
                            self.run_id,
                            error_code=exc.code,
                        )
        return self.database.state_counts(job_id)

    def _recover_asset(self, job_id: str, asset: sqlite3.Row) -> None:
        stable_value = asset["last_stable_state"]
        if stable_value is None:
            raise IntegrityError("failed asset has no stable recovery state")
        stable = AssetState(stable_value)
        stage = self._stage_directory(job_id, asset["id"])
        resources = self.database.list_resources(asset["id"])
        if stable in {
            AssetState.EXPORTED,
            AssetState.UPLOADED,
            AssetState.VERIFIED,
            AssetState.SAFE_TO_DELETE,
        }:
            for resource in resources:
                local_path = safe_path(stage, resource["archive_name"])
                if not local_path.is_file():
                    raise IntegrityError("staged resource is missing during recovery")
                size, sha256, quickxor = hash_file(local_path)
                if (
                    size != resource["actual_size"]
                    or sha256 != resource["sha256"]
                    or quickxor != resource["quickxor"]
                ):
                    raise IntegrityError("staged resource changed during recovery")
        if stable in {
            AssetState.UPLOADED,
            AssetState.VERIFIED,
            AssetState.SAFE_TO_DELETE,
        }:
            for resource in resources:
                archived = self.database.get_archive_file(resource["id"])
                if archived is None:
                    raise IntegrityError("remote mapping is missing during recovery")
                remote = self._with_retry(
                    partial(self.target.stat, str(archived["remote_path"])),
                    job_id=job_id,
                    asset_key=str(asset["id"])[:8],
                )
                if (
                    remote.size != resource["actual_size"]
                    or remote.quickxor != resource["quickxor"]
                ):
                    raise IntegrityError("remote resource changed during recovery")
        self.database.transition(asset["id"], stable, "RESUME_RECONCILED", self.run_id)


def time_from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)
