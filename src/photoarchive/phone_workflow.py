from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from photoarchive.batches import BatchManager
from photoarchive.config import AppConfig
from photoarchive.database import Database
from photoarchive.domain import (
    DeviceMismatchError,
    IntegrityError,
    PhoneCleanupComplete,
    PhoneDevice,
    PhoneResource,
    PhoneSafetyError,
    PhoneScan,
)
from photoarchive.external_drive import ExternalDriveClient
from photoarchive.hashing import hash_file
from photoarchive.paths import safe_path, sanitize_filename
from photoarchive.pipeline import ArchiveRunner, Planner
from photoarchive.protocols import PhoneClient, PhoneSession

ProgressCallback = Callable[[str, dict[str, Any]], None]


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class PhoneScanSummary:
    device: PhoneDevice
    cutoff_at_utc: str
    candidate_assets: int
    candidate_resources: int
    candidate_bytes: int
    warnings: tuple[str, ...]
    total_media_assets: int | None = None
    total_media_resources: int | None = None
    total_media_bytes: int | None = None
    new_archive_assets: int = 0
    new_archive_resources: int = 0
    new_archive_bytes: int = 0
    reusable_assets: int = 0
    reusable_resources: int = 0


@dataclass(frozen=True, slots=True)
class ArchivedPhoneBatch:
    batch_id: str
    job_id: str
    completed_path: Path
    candidate_assets: int
    candidate_resources: int
    candidate_bytes: int


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    batch_id: str
    profile_id: str
    device: PhoneDevice
    cutoff_at_utc: str
    plan_sha256: str
    resources: tuple[PhoneResource, ...]
    asset_ids: tuple[str, ...]
    total_bytes: int


class PhoneWorkflow:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        client: PhoneClient,
        *,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.client = client
        self.progress = progress

    def _progress(self, event: str, **details: Any) -> None:
        if self.progress is not None:
            self.progress(event, details)

    def cutoff_text(self) -> str:
        return self.config.cutoff_at_utc().isoformat().replace("+00:00", "Z")

    def scan(self, session: PhoneSession | None = None) -> tuple[PhoneScan, PhoneScanSummary]:
        self.database.migrate()
        owned = session is None
        active = session or self.client.open_session()
        try:
            cutoff = self.cutoff_text()
            self._progress("PHONE_SCAN_STARTED", cutoff_at_utc=cutoff)
            scan = active.scan(cutoff)
            self._validate_device(scan.device, deletion=False)
            asset_keys = {item.asset_key for item in scan.resources}
            reusable_tokens = {
                item.token
                for item in scan.resources
                if self.database.reusable_phone_resource(
                    scan.device.device_key,
                    item.item_fingerprint,
                    item.size,
                )
                is not None
            }
            new_resources = [item for item in scan.resources if item.token not in reusable_tokens]
            new_asset_keys = {item.asset_key for item in new_resources}
            summary = PhoneScanSummary(
                device=scan.device,
                cutoff_at_utc=cutoff,
                candidate_assets=len(asset_keys),
                candidate_resources=len(scan.resources),
                candidate_bytes=sum(item.size for item in scan.resources),
                warnings=scan.warnings,
                total_media_assets=scan.total_media_assets,
                total_media_resources=scan.total_media_resources,
                total_media_bytes=scan.total_media_bytes,
                new_archive_assets=len(new_asset_keys),
                new_archive_resources=len(new_resources),
                new_archive_bytes=sum(item.size for item in new_resources),
                reusable_assets=len(asset_keys - new_asset_keys),
                reusable_resources=len(reusable_tokens),
            )
            self._progress(
                "PHONE_SCAN_COMPLETED",
                assets=summary.candidate_assets,
                resources=summary.candidate_resources,
                bytes=summary.candidate_bytes,
                total_media_assets=summary.total_media_assets,
                total_media_resources=summary.total_media_resources,
                total_media_bytes=summary.total_media_bytes,
                new_archive_assets=summary.new_archive_assets,
                new_archive_resources=summary.new_archive_resources,
                new_archive_bytes=summary.new_archive_bytes,
                reusable_assets=summary.reusable_assets,
                reusable_resources=summary.reusable_resources,
                total_capacity_bytes=scan.device.total_capacity_bytes,
                used_capacity_bytes=scan.device.used_capacity_bytes,
                available_capacity_bytes=scan.device.available_capacity_bytes,
            )
            return scan, summary
        finally:
            if owned:
                active.close()

    def bound_device(self, profile_id: str) -> Any:
        self.database.migrate()
        self.database.get_profile(profile_id)
        return self.database.get_active_profile_device(profile_id)

    def bind(self, profile_id: str, device: PhoneDevice, *, replace: bool = False) -> Any:
        self.database.migrate()
        self.database.get_profile(profile_id)
        return self.database.bind_profile_device(
            profile_id,
            device.device_key,
            device.name,
            device.product_kind,
            replace=replace,
        )

    def archive_scan(
        self, profile_id: str, session: PhoneSession, scan: PhoneScan
    ) -> ArchivedPhoneBatch:
        if not self.config.phone_cleanup.enabled:
            raise PhoneSafetyError("phone cleanup is disabled in configuration")
        binding = self.bound_device(profile_id)
        if binding is None or binding["device_key"] != scan.device.device_key:
            raise DeviceMismatchError("connected phone is not bound to this profile")
        self._validate_device(scan.device, deletion=False)
        cutoff = self.cutoff_text()
        if not scan.resources:
            raise PhoneSafetyError("no phone items are older than the configured cutoff")
        cutoff_value = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        if any(
            item.creation_at_utc is None or item.creation_at_utc >= cutoff_value
            for item in scan.resources
        ):
            raise PhoneSafetyError(
                "phone discovery returned an item without a safe pre-cutoff date"
            )
        manager = BatchManager(self.config, self.database)
        batch_id, batch_path = manager.prepare_phone(
            profile_id,
            device_id=str(binding["id"]),
            cutoff_at_utc=cutoff,
            icloud_photos_enabled=scan.device.icloud_photos_enabled,
        )
        self.database.set_phone_batch_state(batch_id, "IMPORTING")
        names = self._relative_names(scan.resources)
        self._progress(
            "PHONE_IMPORT_STARTED",
            batch_id=batch_id,
            total=len(scan.resources),
            bytes=sum(item.size for item in scan.resources),
        )
        try:
            for index, item in enumerate(scan.resources, start=1):
                self._progress(
                    "PHONE_IMPORT_RESOURCE_STARTED",
                    current=index,
                    total=len(scan.resources),
                    asset_key=item.asset_key[:8],
                    bytes=item.size,
                )
                relative = names[item.token]
                asset_id = _stable_id(batch_id, item.asset_key)
                item_id = _stable_id(asset_id, item.item_fingerprint)
                self.database.record_phone_item(
                    batch_id,
                    asset_id=asset_id,
                    asset_key=item.asset_key,
                    media_type=item.media_type,
                    asset_creation_at_utc=_utc_text(item.creation_at_utc),
                    asset_warning=item.warning,
                    item_id=item_id,
                    session_token=item.token,
                    item_fingerprint=item.item_fingerprint,
                    ptp_object_handle=item.ptp_object_handle,
                    original_name=item.original_name,
                    relative_path=relative,
                    uti=item.uti,
                    expected_size=item.size,
                    creation_at_utc=_utc_text(item.creation_at_utc),
                    modification_at_utc=_utc_text(item.modification_at_utc),
                    required=item.required,
                    downloadable=item.downloadable,
                    warning=item.warning,
                )
                reusable = self.database.reusable_phone_resource(
                    scan.device.device_key,
                    item.item_fingerprint,
                    item.size,
                )
                if reusable is not None:
                    self.database.reuse_phone_item_archive(
                        item_id,
                        str(reusable["resource_id"]),
                    )
                    self._progress(
                        "PHONE_IMPORT_RESOURCE_REUSED",
                        current=index,
                        total=len(scan.resources),
                        asset_key=item.asset_key[:8],
                    )
                    continue
                if not item.downloadable or item.warning:
                    reason = item.warning or "NOT_DOWNLOADABLE"
                    self.database.set_phone_item_status(item_id, "SKIPPED", error_code=reason)
                    self._progress(
                        "PHONE_IMPORT_RESOURCE_SKIPPED",
                        current=index,
                        total=len(scan.resources),
                        asset_key=item.asset_key[:8],
                        reason=reason,
                    )
                    continue
                destination = safe_path(batch_path, relative)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                partial = destination.with_name(destination.name + ".partial")
                if destination.exists():
                    size, _, _ = hash_file(destination)
                    if size != item.size:
                        raise IntegrityError("existing phone import differs from discovery")
                else:
                    partial.unlink(missing_ok=True)
                    try:
                        session.download(item, partial)
                        size, _, _ = hash_file(partial)
                        if size != item.size:
                            raise IntegrityError("phone download size differs from discovery")
                    except Exception as exc:
                        partial.unlink(missing_ok=True)
                        reason = str(getattr(exc, "code", type(exc).__name__.upper()))
                        self.database.set_phone_item_status(
                            item_id,
                            "FAILED",
                            error_code=reason,
                        )
                        self._progress(
                            "PHONE_IMPORT_RESOURCE_FAILED",
                            current=index,
                            total=len(scan.resources),
                            asset_key=item.asset_key[:8],
                            reason=reason,
                        )
                        continue
                    os.replace(partial, destination)
                    directory = os.open(destination.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                self.database.set_phone_item_status(item_id, "DOWNLOADED")
                self._progress(
                    "PHONE_IMPORT_RESOURCE_COMPLETED",
                    current=index,
                    total=len(scan.resources),
                    asset_key=item.asset_key[:8],
                    bytes=item.size,
                )

            self.database.set_phone_batch_state(batch_id, "ARCHIVING")
            phone_items = self.database.list_phone_items(batch_id)
            if not any(row["status"] in {"DOWNLOADED", "VERIFIED"} for row in phone_items):
                raise PhoneSafetyError("no phone resources have a verified archive or download")
            source = manager.source(batch_id)
            if any(row["status"] == "DOWNLOADED" for row in phone_items):
                summary = Planner(
                    self.config, self.database, source, target_adapter="external"
                ).create_job()
                if summary.job_id is None:
                    raise RuntimeError("archive job was not created")
                job_id = summary.job_id
            else:
                job_id = self.database.create_job(
                    cutoff,
                    self.config.fingerprint(),
                    source.source_reference,
                    source_adapter=source.adapter_name,
                    target_adapter="external",
                    profile_id=profile_id,
                    batch_id=batch_id,
                )
            self.database.attach_batch_job(batch_id, job_id)
            self.database.map_phone_items_to_archive_resources(batch_id)
            self.database.attach_phone_archive_assets(job_id, batch_id)
            self._progress(
                "PHONE_ARCHIVE_STARTED",
                assets=len({item.asset_key for item in scan.resources}),
                resources=len(scan.resources),
                bytes=sum(item.size for item in scan.resources),
            )
            runner = ArchiveRunner(
                self.config,
                self.database,
                source,
                ExternalDriveClient(self.config.external_drive),
                progress=self.progress,
            )
            counts = runner.run_job(job_id, resume=True)
            runner.verify_job(job_id)
            if set(counts) != {"SAFE_TO_DELETE"} or self.database.review_count(job_id):
                raise PhoneSafetyError("one or more imported resources failed archive verification")
            self._mark_ready_assets(batch_id)
            completed = manager.finalize(batch_id)
            self.database.set_phone_batch_state(batch_id, "READY_FOR_PHONE_CLEANUP")
            self._progress("PHONE_ARCHIVE_COMPLETED", counts=counts)
            return ArchivedPhoneBatch(
                batch_id,
                job_id,
                completed,
                len({item.asset_key for item in scan.resources}),
                len(scan.resources),
                sum(item.size for item in scan.resources),
            )
        except Exception as exc:
            self.database.set_phone_batch_state(
                batch_id,
                "NEEDS_ATTENTION",
                error_code=str(getattr(exc, "code", type(exc).__name__.upper())),
            )
            raise

    def resume_archive(self, batch_id: str, session: PhoneSession) -> ArchivedPhoneBatch:
        batch = self.database.get_phone_batch(batch_id)
        repaired = self.database.repair_legacy_phone_relationship_warnings(batch_id)
        if repaired:
            self._progress("PHONE_LEGACY_WARNING_REPAIRED", resources=repaired)
        scan = session.scan(str(batch["cutoff_at_utc"]))
        if scan.device.device_key != batch["device_key"]:
            raise DeviceMismatchError("connected phone does not match this batch")
        self._validate_device(scan.device, deletion=False)
        current = {item.item_fingerprint: item for item in scan.resources}
        manager = BatchManager(self.config, self.database)
        batch_path = manager.batch_path(batch_id)
        self.database.set_phone_batch_state(batch_id, "IMPORTING")
        stored_items = list(self.database.list_phone_items(batch_id))
        self._progress("PHONE_IMPORT_RESUMED", batch_id=batch_id, total=len(stored_items))
        for index, row in enumerate(stored_items, start=1):
            if row["status"] in {"DOWNLOADED", "VERIFIED", "DELETED"}:
                continue
            item = current.get(str(row["item_fingerprint"]))
            if item is None or item.warning or not item.downloadable:
                reason = item.warning if item is not None else "DEVICE_ITEM_MISMATCH"
                self.database.set_phone_item_status(
                    str(row["id"]), "SKIPPED", error_code=reason or "NOT_DOWNLOADABLE"
                )
                self._progress(
                    "PHONE_IMPORT_RESOURCE_SKIPPED",
                    current=index,
                    total=len(stored_items),
                    asset_key=str(row["device_asset_key"])[:8],
                    reason=reason or "NOT_DOWNLOADABLE",
                )
                continue
            destination = safe_path(batch_path, str(row["relative_path"]))
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not destination.exists():
                self._progress(
                    "PHONE_IMPORT_RESOURCE_STARTED",
                    current=index,
                    total=len(stored_items),
                    asset_key=item.asset_key[:8],
                    bytes=item.size,
                )
                partial = destination.with_name(destination.name + ".partial")
                partial.unlink(missing_ok=True)
                try:
                    session.download(item, partial)
                    size, _, _ = hash_file(partial)
                    if size != int(row["expected_size"]):
                        raise IntegrityError("resumed phone download size differs from discovery")
                except Exception as exc:
                    partial.unlink(missing_ok=True)
                    reason = str(getattr(exc, "code", type(exc).__name__.upper()))
                    self.database.set_phone_item_status(
                        str(row["id"]),
                        "FAILED",
                        error_code=reason,
                    )
                    self._progress(
                        "PHONE_IMPORT_RESOURCE_FAILED",
                        current=index,
                        total=len(stored_items),
                        asset_key=item.asset_key[:8],
                        reason=reason,
                    )
                    continue
                os.replace(partial, destination)
            self.database.set_phone_item_status(str(row["id"]), "DOWNLOADED")
            self._progress(
                "PHONE_IMPORT_RESOURCE_COMPLETED",
                current=index,
                total=len(stored_items),
                asset_key=item.asset_key[:8],
                bytes=item.size,
            )
        self.database.set_phone_batch_state(batch_id, "ARCHIVING")
        if not any(
            row["status"] in {"DOWNLOADED", "VERIFIED", "DELETED"}
            for row in self.database.list_phone_items(batch_id)
        ):
            raise PhoneSafetyError("no phone resources were downloaded successfully")
        source = manager.source(batch_id)
        job_id = batch["job_id"]
        if job_id:
            Planner(self.config, self.database, source, target_adapter="external").refresh_job(
                str(job_id)
            )
        else:
            summary = Planner(
                self.config, self.database, source, target_adapter="external"
            ).create_job()
            if summary.job_id is None:
                raise RuntimeError("archive job was not created")
            job_id = summary.job_id
            self.database.attach_batch_job(batch_id, str(job_id))
        self.database.map_phone_items_to_archive_resources(batch_id)
        self._progress("PHONE_ARCHIVE_STARTED", batch_id=batch_id)
        counts = ArchiveRunner(
            self.config,
            self.database,
            source,
            ExternalDriveClient(self.config.external_drive),
            progress=self.progress,
        ).run_job(str(job_id), resume=True)
        if set(counts) != {"SAFE_TO_DELETE"} or self.database.review_count(str(job_id)):
            self.database.set_phone_batch_state(
                batch_id, "NEEDS_ATTENTION", error_code="VERIFY_FAILED"
            )
            raise PhoneSafetyError("one or more imported resources failed archive verification")
        self._mark_ready_assets(batch_id)
        completed = manager.finalize(batch_id)
        self.database.set_phone_batch_state(batch_id, "READY_FOR_PHONE_CLEANUP")
        self._progress("PHONE_ARCHIVE_COMPLETED", counts=counts)
        items = self.database.list_phone_items(batch_id)
        return ArchivedPhoneBatch(
            batch_id,
            str(job_id),
            completed,
            len({str(row["phone_asset_id"]) for row in items}),
            len(items),
            sum(int(row["expected_size"]) for row in items),
        )

    def prepare_cleanup(self, batch_id: str, session: PhoneSession) -> CleanupPlan:
        if not self.config.phone_cleanup.enabled:
            raise PhoneSafetyError("phone cleanup is disabled in configuration")
        batch = self.database.get_phone_batch(batch_id)
        if batch["state"] not in {
            "READY_FOR_PHONE_CLEANUP",
            "COMPLETED_WITH_PHONE_ITEMS_REMAINING",
            "NEEDS_ATTENTION",
            "CLEANING_PHONE",
        }:
            raise PhoneSafetyError(f"phone batch is not ready for cleanup: {batch['state']}")
        self._progress("PHONE_CLEANUP_REVALIDATION_STARTED", batch_id=batch_id)
        scan = session.scan(str(batch["cutoff_at_utc"]))
        self._validate_device(scan.device, deletion=True)
        if scan.device.device_key != batch["device_key"]:
            raise DeviceMismatchError("connected phone does not match this batch")
        if scan.device.icloud_photos_enabled is None:
            raise PhoneSafetyError("iCloud Photos status is unknown; deletion is blocked")
        self.database.set_phone_batch_icloud_status(batch_id, scan.device.icloud_photos_enabled)
        batch = self.database.get_phone_batch(batch_id)
        current_by_fingerprint = {item.item_fingerprint: item for item in scan.resources}
        if len(current_by_fingerprint) != len(scan.resources):
            raise PhoneSafetyError("phone contains ambiguous item fingerprints")
        if batch["state"] == "CLEANING_PHONE" and batch["deletion_plan_sha256"]:
            self._reconcile_deleted_items(batch_id, current_by_fingerprint)
            batch = self.database.get_phone_batch(batch_id)
            if batch["state"] == "COMPLETED":
                raise PhoneCleanupComplete("phone cleanup completed during reconciliation")
        gate_rows = self.database.phone_asset_gate_rows(batch_id)
        ready_asset_ids = {
            str(row["id"])
            for row in gate_rows
            if row["warning"] is None
            and int(row["required_count"] or 0) > 0
            and int(row["required_count"] or 0) == int(row["verified_count"] or 0)
            and row["cleanup_state"] != "DELETED"
        }
        candidate_rows = [
            row
            for row in self.database.list_phone_items(batch_id)
            if row["phone_asset_id"] in ready_asset_ids
            and row["status"] not in {"DELETED", "SKIPPED"}
        ]
        rows_by_asset: dict[str, list[Any]] = {}
        for row in candidate_rows:
            rows_by_asset.setdefault(str(row["phone_asset_id"]), []).append(row)
        selected_rows: list[Any] = []
        selected: list[PhoneResource] = []
        selected_by_asset: dict[str, list[PhoneResource]] = {}
        for asset_id, asset_rows in rows_by_asset.items():
            current_items = [
                current_by_fingerprint.get(str(row["item_fingerprint"])) for row in asset_rows
            ]
            matches = all(
                current is not None and current.size == int(row["expected_size"])
                for row, current in zip(asset_rows, current_items, strict=True)
            )
            if not matches:
                self.database.set_phone_asset_state(
                    asset_id, "REMAINING", error_code="DEVICE_ITEM_MISMATCH"
                )
                continue
            concrete_items = [item for item in current_items if item is not None]
            selected_rows.extend(asset_rows)
            selected.extend(concrete_items)
            selected_by_asset[asset_id] = concrete_items
        if not selected:
            raise PhoneSafetyError("no fully verified phone assets remain eligible for deletion")
        valid = set(session.revalidate(tuple(selected)))
        valid_assets = {
            asset_id
            for asset_id, items in selected_by_asset.items()
            if items and all(item.token in valid for item in items)
        }
        for asset_id in set(selected_by_asset) - valid_assets:
            self.database.set_phone_asset_state(
                asset_id, "REMAINING", error_code="DEVICE_REVALIDATION_FAILED"
            )
        selected = [
            item
            for asset_id, items in selected_by_asset.items()
            if asset_id in valid_assets
            for item in items
        ]
        if not selected:
            raise PhoneSafetyError("phone item revalidation failed; deletion is blocked")
        selected_fingerprints = {item.item_fingerprint for item in selected}
        selected_rows = [
            row
            for row in selected_rows
            if row["phone_asset_id"] in valid_assets
            and row["item_fingerprint"] in selected_fingerprints
        ]
        plan_payload = self._plan_payload(batch, selected_rows, scan.device)
        plan_sha256 = hashlib.sha256(
            json.dumps(plan_payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        asset_ids = tuple(sorted({str(row["phone_asset_id"]) for row in selected_rows}))
        self._progress(
            "PHONE_CLEANUP_READY",
            assets=len(asset_ids),
            resources=len(selected),
            bytes=sum(item.size for item in selected),
        )
        return CleanupPlan(
            batch_id=batch_id,
            profile_id=str(batch["profile_id"]),
            device=scan.device,
            cutoff_at_utc=str(batch["cutoff_at_utc"]),
            plan_sha256=plan_sha256,
            resources=tuple(selected),
            asset_ids=asset_ids,
            total_bytes=sum(item.size for item in selected),
        )

    def execute_cleanup(self, plan: CleanupPlan, session: PhoneSession) -> dict[str, int]:
        batch = self.database.get_phone_batch(plan.batch_id)
        current_rows = self.database.list_phone_items(plan.batch_id)
        plan_fingerprints = {item.item_fingerprint for item in plan.resources}
        payload = self._plan_payload(
            batch,
            [
                row
                for row in current_rows
                if row["phone_asset_id"] in plan.asset_ids
                and row["item_fingerprint"] in plan_fingerprints
            ],
            plan.device,
        )
        current_digest = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        if current_digest != plan.plan_sha256:
            raise PhoneSafetyError("deletion plan changed after confirmation")
        self.database.set_phone_batch_state(
            plan.batch_id,
            "CLEANING_PHONE",
            plan_sha256=plan.plan_sha256,
            confirmed=True,
        )
        for asset_id in plan.asset_ids:
            key = f"phone-delete:{plan.batch_id}:{asset_id}:{plan.plan_sha256}"
            self.database.begin_batch_operation(plan.batch_id, "DELETE_PHONE_ASSET", key)
            self.database.set_phone_asset_state(asset_id, "DELETE_INTENT")
        deleted: set[str] = set()
        failed: set[str] = set()
        failure_reasons: dict[str, str] = {}
        completion_errors: list[str] = []
        batch_size = self.config.phone_cleanup.delete_batch_size
        resource_by_fingerprint = {item.item_fingerprint: item for item in plan.resources}
        groups: list[tuple[PhoneResource, ...]] = []
        for asset_id in plan.asset_ids:
            group = tuple(
                resource_by_fingerprint[str(row["item_fingerprint"])]
                for row in current_rows
                if row["phone_asset_id"] == asset_id
                and row["item_fingerprint"] in resource_by_fingerprint
            )
            if group:
                groups.append(group)
        chunks: list[tuple[PhoneResource, ...]] = []
        current_chunk: list[PhoneResource] = []
        for group in groups:
            if current_chunk and len(current_chunk) + len(group) > batch_size:
                chunks.append(tuple(current_chunk))
                current_chunk = []
            current_chunk.extend(group)
        if current_chunk:
            chunks.append(tuple(current_chunk))
        for index, chunk in enumerate(chunks, start=1):
            self._progress(
                "PHONE_DELETE_CHUNK_STARTED",
                current=index,
                total=len(chunks),
                resources=len(chunk),
            )
            result = session.delete(
                chunk,
                batch_id=plan.batch_id,
                cutoff_at_utc=plan.cutoff_at_utc,
                plan_sha256=plan.plan_sha256,
            )
            deleted.update(result.deleted_tokens)
            failed.update(result.failed_tokens)
            failure_reasons.update(result.failure_reasons)
            if result.completion_error:
                completion_errors.append(result.completion_error)
            failure_details = sorted(
                {
                    result.failure_reasons.get(token)
                    or result.completion_error
                    or "ImageCaptureCore 未提供具体原因"
                    for token in result.failed_tokens
                }
            )
            self._progress(
                "PHONE_DELETE_CHUNK_COMPLETED",
                current=index,
                total=len(chunks),
                deleted=len(result.deleted_tokens),
                failed=len(result.failed_tokens),
                reasons=failure_details,
                method=result.delete_method,
            )
        rows = self.database.list_phone_items(plan.batch_id)
        fingerprint_to_row = {str(row["item_fingerprint"]): row for row in rows}
        for item in plan.resources:
            row = fingerprint_to_row.get(item.item_fingerprint)
            if row is None:
                continue
            if item.token in deleted:
                self.database.set_phone_item_status(str(row["id"]), "DELETED")
            else:
                detail = (
                    failure_reasons.get(item.token)
                    or (completion_errors[-1] if completion_errors else None)
                )
                self.database.set_phone_item_status(
                    str(row["id"]),
                    "FAILED",
                    error_code="PHONE_DELETE_FAILED",
                    error_detail=detail or "ImageCaptureCore 未提供具体原因",
                )
                failed.add(item.token)
        for asset_id in plan.asset_ids:
            asset_rows = [
                row
                for row in self.database.list_phone_items(plan.batch_id)
                if row["phone_asset_id"] == asset_id
            ]
            complete = all(row["status"] == "DELETED" for row in asset_rows)
            state = "DELETED" if complete else "FAILED"
            details = sorted(
                {
                    str(row["delete_error_detail"])
                    for row in asset_rows
                    if row["delete_error_detail"]
                }
            )
            self.database.set_phone_asset_state(
                asset_id,
                state,
                error_code=None if complete else "PHONE_DELETE_FAILED",
                error_detail=None if complete else "; ".join(details),
            )
            key = f"phone-delete:{plan.batch_id}:{asset_id}:{plan.plan_sha256}"
            self.database.finish_batch_operation(
                key,
                "SUCCEEDED" if complete else "FAILED",
                {"deleted": complete},
            )
        final_state = "COMPLETED" if not failed else "COMPLETED_WITH_PHONE_ITEMS_REMAINING"
        self.database.set_phone_batch_state(plan.batch_id, final_state)
        self._progress("PHONE_CLEANUP_COMPLETED", deleted=len(deleted), failed=len(failed))
        return {"deleted": len(deleted), "failed": len(failed)}

    def _reconcile_deleted_items(
        self, batch_id: str, current_by_fingerprint: dict[str, PhoneResource]
    ) -> None:
        rows = self.database.list_phone_items(batch_id)
        with self.database.connect() as connection:
            intent_by_asset = {
                str(row["id"]): str(row["idempotency_key"])
                for row in connection.execute(
                    """
                    SELECT phone_assets.id, batch_operations.idempotency_key
                    FROM phone_assets
                    JOIN batch_operations
                      ON batch_operations.batch_id = phone_assets.batch_id
                     AND batch_operations.idempotency_key LIKE
                         'phone-delete:' || phone_assets.batch_id || ':' || phone_assets.id || ':%'
                    WHERE phone_assets.batch_id = ? AND batch_operations.status = 'INTENT'
                    """,
                    (batch_id,),
                ).fetchall()
            }
        intent_assets = set(intent_by_asset)
        for row in rows:
            if (
                row["phone_asset_id"] in intent_assets
                and row["status"] != "DELETED"
                and row["item_fingerprint"] not in current_by_fingerprint
            ):
                self.database.set_phone_item_status(str(row["id"]), "DELETED")
        all_complete = True
        for asset_id in intent_assets:
            asset_rows = [
                row
                for row in self.database.list_phone_items(batch_id)
                if row["phone_asset_id"] == asset_id
            ]
            complete = bool(asset_rows) and all(row["status"] == "DELETED" for row in asset_rows)
            self.database.set_phone_asset_state(
                asset_id,
                "DELETED" if complete else "FAILED",
                error_code=None if complete else "PHONE_ITEMS_REMAIN",
            )
            if complete:
                self.database.finish_batch_operation(
                    intent_by_asset[asset_id],
                    "SUCCEEDED",
                    {"deleted": True, "reconciled_absence": True},
                )
            all_complete = all_complete and complete
        if intent_assets and all_complete:
            self.database.set_phone_batch_state(batch_id, "COMPLETED")

    @staticmethod
    def _validate_device(device: PhoneDevice, *, deletion: bool) -> None:
        if device.product_kind.casefold() != "iphone":
            raise PhoneSafetyError("exactly one connected iPhone is required")
        if not device.trusted:
            raise PhoneSafetyError("trust this Mac on the connected iPhone")
        if deletion and not device.can_delete:
            raise PhoneSafetyError("the connected iPhone does not allow per-item deletion")

    @staticmethod
    def _relative_names(resources: tuple[PhoneResource, ...]) -> dict[str, str]:
        used: set[str] = set()
        result: dict[str, str] = {}
        for index, item in enumerate(sorted(resources, key=lambda value: value.token)):
            base = sanitize_filename(item.original_name, f"resource-{index}")
            relative = str(Path(item.asset_key[:16], base))
            if relative.casefold() in used:
                stem = Path(base).stem
                suffix = Path(base).suffix
                relative = str(Path(item.asset_key[:16], f"{stem}__{index}{suffix}"))
            used.add(relative.casefold())
            result[item.token] = relative
        return result

    def _mark_ready_assets(self, batch_id: str) -> None:
        for row in self.database.phone_asset_gate_rows(batch_id):
            ready = (
                row["warning"] is None
                and int(row["required_count"] or 0) > 0
                and int(row["required_count"] or 0) == int(row["verified_count"] or 0)
            )
            self.database.set_phone_asset_state(
                str(row["id"]),
                "READY" if ready else "REMAINING",
                error_code=None if ready else "ARCHIVE_GATE_FAILED",
            )
        for row in self.database.list_phone_items(batch_id):
            if row["cleanup_state"] == "READY":
                self.database.set_phone_item_status(str(row["id"]), "VERIFIED")

    def _plan_payload(self, batch: Any, rows: list[Any], device: PhoneDevice) -> dict[str, Any]:
        evidence: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            for row in sorted(rows, key=lambda value: str(value["item_fingerprint"])):
                archive = connection.execute(
                    """
                    SELECT archive_files.remote_path, archive_files.size,
                           asset_resources.sha256, asset_resources.quickxor
                    FROM archive_files
                    JOIN asset_resources ON asset_resources.id = archive_files.resource_id
                    WHERE archive_files.resource_id = ?
                    """,
                    (row["resource_id"],),
                ).fetchone()
                if archive is None:
                    raise PhoneSafetyError("archive evidence is missing")
                evidence.append(
                    {
                        "item_fingerprint": row["item_fingerprint"],
                        "size": row["expected_size"],
                        "remote_path": archive["remote_path"],
                        "sha256": archive["sha256"],
                        "quickxor": archive["quickxor"],
                    }
                )
        return {
            "schema_version": 1,
            "batch_id": batch["batch_id"],
            "profile_id": batch["profile_id"],
            "device_key": device.device_key,
            "cutoff_at_utc": batch["cutoff_at_utc"],
            "icloud_photos_enabled": device.icloud_photos_enabled,
            "items": evidence,
        }
