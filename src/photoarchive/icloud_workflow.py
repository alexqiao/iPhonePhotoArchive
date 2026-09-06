from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from typing import Any

from photoarchive.config import AppConfig
from photoarchive.database import Database
from photoarchive.domain import (
    ICloudCleanupComplete,
    PhoneSafetyError,
    PhotoLibraryAsset,
    PhotoLibraryResource,
    PhotoLibraryScan,
)
from photoarchive.external_drive import ExternalDriveClient
from photoarchive.icloud_source import PhotoLibraryArchiveSource, decode_source
from photoarchive.pipeline import ArchiveRunner, Planner
from photoarchive.protocols import PhotoLibraryClient, PhotoLibrarySession


@dataclass(frozen=True, slots=True)
class ICloudScanSummary:
    authorization: str
    total_assets: int
    total_resources: int
    candidate_assets: int
    candidate_resources: int
    candidate_bytes: int | None
    cutoff_at_utc: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchivedICloudBatch:
    batch_id: str
    job_id: str
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class ICloudCleanupPlan:
    batch_id: str
    profile_id: str
    cutoff_at_utc: str
    plan_sha256: str
    assets: tuple[PhotoLibraryAsset, ...]
    total_bytes: int


ProgressCallback = Any


class ICloudWorkflow:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        client: PhotoLibraryClient,
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

    def _cutoff(self) -> str:
        return self.config.cutoff_at_utc().isoformat().replace("+00:00", "Z")

    def scan(
        self, profile_id: str, session: PhotoLibrarySession | None = None
    ) -> tuple[PhotoLibraryScan, ICloudScanSummary]:
        self.database.get_profile(profile_id)
        owns_session = session is None
        active = session or self.client.open_session()
        cutoff = self._cutoff()
        self._progress("ICLOUD_SCAN_STARTED", profile_id=profile_id)
        try:
            scan = active.scan(cutoff, frozenset(self.config.archive_policy.media_types))
        finally:
            if owns_session:
                active.close()
        summary = ICloudScanSummary(
            authorization=scan.authorization,
            total_assets=scan.total_assets,
            total_resources=scan.total_resources,
            candidate_assets=len(scan.assets),
            candidate_resources=sum(len(asset.resources) for asset in scan.assets),
            candidate_bytes=None,
            cutoff_at_utc=cutoff,
            warnings=scan.warnings,
        )
        self._progress(
            "ICLOUD_SCAN_COMPLETED",
            total_assets=summary.total_assets,
            total_resources=summary.total_resources,
            assets=summary.candidate_assets,
            resources=summary.candidate_resources,
            cutoff_at_utc=cutoff,
        )
        return scan, summary

    def archive_scan(
        self,
        profile_id: str,
        session: PhotoLibrarySession,
        scan: PhotoLibraryScan,
        *,
        candidate_limit: int | None = None,
    ) -> ArchivedICloudBatch:
        if not self.config.icloud_cleanup.enabled:
            raise PhoneSafetyError("iCloud cleanup is disabled in configuration")
        self.database.get_profile(profile_id)
        ExternalDriveClient(self.config.external_drive).assert_ready()
        scan = self.unarchived_scan(profile_id, scan)
        if not scan.assets:
            raise PhoneSafetyError("no unarchived iCloud assets remain eligible")
        batch_id = self._new_batch_id()
        library_id = f"icloud:{profile_id}:{batch_id}"
        source = PhotoLibraryArchiveSource(
            session,
            profile_id=profile_id,
            library_id=library_id,
            initial_scan=scan,
        )
        planner = Planner(
            self.config,
            self.database,
            source,
            candidate_limit=candidate_limit or self.config.icloud_cleanup.batch_size,
            target_adapter="external",
        )
        summary = planner.create_job()
        if summary.job_id is None:
            raise RuntimeError("iCloud archive job was not created")
        self.database.create_icloud_batch(
            batch_id,
            profile_id,
            summary.job_id,
            library_id,
            summary.cutoff_at_utc,
        )
        self._progress(
            "ICLOUD_ARCHIVE_STARTED",
            batch_id=batch_id,
            assets=summary.candidate_assets,
            resources=summary.required_resources,
        )
        runner = ArchiveRunner(
            self.config,
            self.database,
            source,
            ExternalDriveClient(self.config.external_drive),
            progress=self.progress,
        )
        try:
            counts = runner.run_job(summary.job_id)
            self._mark_ready_assets(batch_id)
            ready = sum(
                row["cleanup_state"] == "READY"
                for row in self.database.list_icloud_batch_assets(batch_id)
            )
            self.database.set_icloud_batch_state(
                batch_id,
                "READY_FOR_ICLOUD_CLEANUP" if ready else "NEEDS_ATTENTION",
                error_code=None if ready else "NO_VERIFIED_ICLOUD_ASSETS",
            )
        except BaseException:
            self.database.set_icloud_batch_state(
                batch_id, "NEEDS_ATTENTION", error_code="ARCHIVE_INTERRUPTED"
            )
            raise
        self._progress("ICLOUD_ARCHIVE_COMPLETED", batch_id=batch_id, counts=counts)
        return ArchivedICloudBatch(batch_id, summary.job_id, counts)

    def unarchived_scan(
        self, profile_id: str, scan: PhotoLibraryScan
    ) -> PhotoLibraryScan:
        archived = self.database.verified_icloud_local_ids(profile_id)
        assets = tuple(
            asset for asset in scan.assets if asset.local_identifier not in archived
        )
        return PhotoLibraryScan(
            authorization=scan.authorization,
            assets=assets,
            total_assets=scan.total_assets,
            total_resources=scan.total_resources,
            warnings=scan.warnings,
        )

    def archive_all_batches(
        self,
        profile_id: str,
        session: PhotoLibrarySession,
        scan: PhotoLibraryScan,
    ) -> tuple[ArchivedICloudBatch, ...]:
        remaining = self.unarchived_scan(profile_id, scan)
        completed: list[ArchivedICloudBatch] = []
        for group in batched(remaining.assets, self.config.icloud_cleanup.batch_size):
            subset = PhotoLibraryScan(
                authorization=remaining.authorization,
                assets=tuple(group),
                total_assets=remaining.total_assets,
                total_resources=remaining.total_resources,
                warnings=remaining.warnings,
            )
            archived = self.archive_scan(
                profile_id,
                session,
                subset,
                candidate_limit=len(subset.assets),
            )
            completed.append(archived)
            if archived.counts.get("SAFE_TO_DELETE", 0) != len(subset.assets):
                raise PhoneSafetyError(
                    f"night archive stopped after batch {archived.batch_id} needs attention"
                )
        return tuple(completed)

    def resume_archive(
        self, batch_id: str, session: PhotoLibrarySession
    ) -> ArchivedICloudBatch:
        batch = self.database.get_icloud_batch(batch_id)
        source = PhotoLibraryArchiveSource(
            session,
            profile_id=str(batch["profile_id"]),
            library_id=str(batch["library_id"]),
        )
        self.database.set_icloud_batch_state(batch_id, "ARCHIVING")
        counts = ArchiveRunner(
            self.config,
            self.database,
            source,
            ExternalDriveClient(self.config.external_drive),
            progress=self.progress,
        ).run_job(str(batch["job_id"]), resume=True)
        self._mark_ready_assets(batch_id)
        ready = any(
            row["cleanup_state"] == "READY"
            for row in self.database.list_icloud_batch_assets(batch_id)
        )
        self.database.set_icloud_batch_state(
            batch_id,
            "READY_FOR_ICLOUD_CLEANUP" if ready else "NEEDS_ATTENTION",
            error_code=None if ready else "NO_VERIFIED_ICLOUD_ASSETS",
        )
        return ArchivedICloudBatch(batch_id, str(batch["job_id"]), counts)

    def prepare_cleanup(
        self,
        batch_id: str,
        session: PhotoLibrarySession,
        *,
        local_identifiers: frozenset[str] | None = None,
    ) -> ICloudCleanupPlan:
        if not self.config.icloud_cleanup.enabled:
            raise PhoneSafetyError("iCloud cleanup is disabled in configuration")
        batch = self.database.get_icloud_batch(batch_id)
        if batch["state"] not in {
            "READY_FOR_ICLOUD_CLEANUP",
            "COMPLETED_WITH_ITEMS_REMAINING",
            "NEEDS_ATTENTION",
            "CLEANING_ICLOUD",
        }:
            raise PhoneSafetyError(f"iCloud batch is not ready for cleanup: {batch['state']}")
        self._progress("ICLOUD_CLEANUP_REVALIDATION_STARTED", batch_id=batch_id)
        if batch["state"] == "CLEANING_ICLOUD" and batch["deletion_plan_sha256"]:
            self._reconcile_delete_intents(batch_id, session)
            batch = self.database.get_icloud_batch(batch_id)
            if batch["state"] == "COMPLETED":
                raise ICloudCleanupComplete("iCloud cleanup completed during reconciliation")
        ExternalDriveClient(self.config.external_drive).assert_ready()
        source = PhotoLibraryArchiveSource(
            session,
            profile_id=str(batch["profile_id"]),
            library_id=str(batch["library_id"]),
        )
        deletion_candidate_ids = {
            str(row["id"])
            for row in self.database.list_icloud_batch_assets(batch_id)
            if row["cleanup_state"] != "DELETED"
            and row["state"] == "SAFE_TO_DELETE"
            and (
                local_identifiers is None
                or str(row["photos_local_id"]) in local_identifiers
            )
        }
        ArchiveRunner(
            self.config,
            self.database,
            source,
            ExternalDriveClient(self.config.external_drive),
            progress=self.progress,
        ).verify_for_deletion(str(batch["job_id"]), deletion_candidate_ids)
        self._mark_ready_assets(batch_id)
        candidates = self._asset_references(batch_id, {"READY"})
        if local_identifiers is not None:
            candidates = tuple(
                asset
                for asset in candidates
                if asset.local_identifier in local_identifiers
            )
            if {asset.local_identifier for asset in candidates} != local_identifiers:
                raise PhoneSafetyError(
                    "one or more confirmed iCloud assets failed deletion-time verification"
                )
        if not candidates:
            raise PhoneSafetyError("no fully verified iCloud assets remain eligible for deletion")
        validation = session.revalidate(candidates, cutoff_at_utc=str(batch["cutoff_at_utc"]))
        valid_ids = set(validation.valid_local_identifiers)
        missing_ids = set(validation.missing_local_identifiers)
        mismatched_ids = set(validation.mismatched_local_identifiers)
        for row in self.database.list_icloud_batch_assets(batch_id):
            local_id = str(row["photos_local_id"])
            if local_id in missing_ids:
                self.database.set_icloud_asset_state(
                    str(row["batch_asset_id"]),
                    "REMAINING",
                    error_code="ICLOUD_ASSET_MISSING",
                )
            elif local_id in mismatched_ids:
                self.database.set_icloud_asset_state(
                    str(row["batch_asset_id"]),
                    "REMAINING",
                    error_code="ICLOUD_ASSET_CHANGED",
                )
        selected = tuple(asset for asset in candidates if asset.local_identifier in valid_ids)
        if not selected:
            raise PhoneSafetyError("iCloud asset revalidation failed; deletion is blocked")
        plan = self._cleanup_plan(batch, selected)
        self._progress(
            "ICLOUD_CLEANUP_READY",
            assets=len(selected),
            resources=sum(len(asset.resources) for asset in selected),
            bytes=plan.total_bytes,
        )
        return plan

    def split_cleanup_plan(
        self, plan: ICloudCleanupPlan
    ) -> tuple[ICloudCleanupPlan, ...]:
        batch = self.database.get_icloud_batch(plan.batch_id)
        return tuple(
            self._cleanup_plan(batch, tuple(group))
            for group in batched(plan.assets, self.config.icloud_cleanup.batch_size)
        )

    def cleanup_batch_ids(self, profile_id: str) -> tuple[str, ...]:
        return tuple(
            str(row["batch_id"])
            for row in self.database.list_icloud_batches_with_verified_assets(profile_id)
        )

    def preview_cleanup_plans(self, profile_id: str) -> tuple[ICloudCleanupPlan, ...]:
        """Build exact bounded plans for one confirmation, without remote I/O."""
        plans: list[ICloudCleanupPlan] = []
        for batch_id in self.cleanup_batch_ids(profile_id):
            batch = self.database.get_icloud_batch(batch_id)
            self._mark_ready_assets(batch_id)
            candidates = self._asset_references(batch_id, {"READY"})
            plans.extend(
                self._cleanup_plan(batch, tuple(group))
                for group in batched(
                    candidates,
                    self.config.icloud_cleanup.batch_size,
                )
            )
        return tuple(plans)

    def execute_cleanup(
        self, plan: ICloudCleanupPlan, session: PhotoLibrarySession
    ) -> dict[str, int]:
        batch = self.database.get_icloud_batch(plan.batch_id)
        payload = self._plan_payload(batch, plan.assets)
        digest = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        if digest != plan.plan_sha256:
            raise PhoneSafetyError("iCloud deletion plan changed after confirmation")
        self.database.set_icloud_batch_state(
            plan.batch_id,
            "CLEANING_ICLOUD",
            plan_sha256=plan.plan_sha256,
            confirmed=True,
        )
        rows_by_local_id = {
            str(row["photos_local_id"]): row
            for row in self.database.list_icloud_batch_assets(plan.batch_id)
        }
        for asset in plan.assets:
            self.database.set_icloud_asset_state(
                str(rows_by_local_id[asset.local_identifier]["batch_asset_id"]),
                "DELETE_INTENT",
            )
        self._progress("ICLOUD_DELETE_STARTED", assets=len(plan.assets))
        result = session.delete(
            plan.assets,
            batch_id=plan.batch_id,
            cutoff_at_utc=plan.cutoff_at_utc,
            plan_sha256=plan.plan_sha256,
        )
        deleted = set(result.deleted_local_identifiers)
        reported_failed = set(result.failed_local_identifiers)
        for asset in plan.assets:
            row = rows_by_local_id[asset.local_identifier]
            if asset.local_identifier in deleted:
                self.database.set_icloud_asset_state(
                    str(row["batch_asset_id"]), "DELETED"
                )
            else:
                reported_failed.add(asset.local_identifier)
                self.database.set_icloud_asset_state(
                    str(row["batch_asset_id"]),
                    "FAILED",
                    error_code="ICLOUD_DELETE_FAILED",
                    error_detail=result.failure_reason or "PhotoKit did not confirm deletion",
                )
        remaining = sum(
            row["cleanup_state"] != "DELETED"
            for row in self.database.list_icloud_batch_assets(plan.batch_id)
        )
        self.database.set_icloud_batch_state(
            plan.batch_id,
            "COMPLETED" if remaining == 0 else "COMPLETED_WITH_ITEMS_REMAINING",
        )
        self._progress(
            "ICLOUD_CLEANUP_COMPLETED", deleted=len(deleted), failed=len(reported_failed)
        )
        return {"deleted": len(deleted), "failed": len(reported_failed)}

    def _mark_ready_assets(self, batch_id: str) -> None:
        for row in self.database.icloud_asset_gate_rows(batch_id):
            if row["cleanup_state"] == "DELETED":
                continue
            required = int(row["required_count"] or 0)
            archived = int(row["archived_count"] or 0)
            ready = (
                row["archive_state"] == "SAFE_TO_DELETE"
                and not bool(row["review_required"])
                and required > 0
                and required == archived
            )
            self.database.set_icloud_asset_state(
                str(row["batch_asset_id"]),
                "READY" if ready else "REMAINING",
                error_code=None if ready else "ARCHIVE_GATE_FAILED",
            )

    def _asset_references(
        self, batch_id: str, states: set[str]
    ) -> tuple[PhotoLibraryAsset, ...]:
        assets: list[PhotoLibraryAsset] = []
        for row in self.database.list_icloud_batch_assets(batch_id):
            if str(row["cleanup_state"]) not in states:
                continue
            resources: list[PhotoLibraryResource] = []
            local_identifier = str(row["photos_local_id"])
            for resource_row in self.database.list_resources(str(row["id"])):
                persisted_asset, resource = decode_source(Path(str(resource_row["source_ref"])))
                if (
                    persisted_asset.local_identifier != local_identifier
                    or resource.resource_key != resource_row["resource_key"]
                ):
                    raise PhoneSafetyError("persisted iCloud resource identity is inconsistent")
                resources.append(resource)
            assets.append(
                PhotoLibraryAsset(
                    local_identifier=local_identifier,
                    creation_at_utc=datetime.fromisoformat(
                        str(row["creation_at_utc"]).replace("Z", "+00:00")
                    ).astimezone(UTC),
                    media_type=str(row["media_type"]),
                    resources=tuple(sorted(resources, key=lambda item: item.resource_key)),
                )
            )
        return tuple(assets)

    def _plan_payload(
        self, batch: Any, assets: tuple[PhotoLibraryAsset, ...]
    ) -> dict[str, Any]:
        planned_assets: list[dict[str, Any]] = []
        rows = {
            str(row["photos_local_id"]): row
            for row in self.database.list_icloud_batch_assets(str(batch["batch_id"]))
        }
        with self.database.connect() as connection:
            for asset in sorted(assets, key=lambda item: item.local_identifier):
                row = rows.get(asset.local_identifier)
                if row is None:
                    raise PhoneSafetyError("iCloud batch asset is missing")
                evidence = connection.execute(
                    """
                    SELECT asset_resources.resource_key, asset_resources.actual_size AS size,
                           asset_resources.sha256, asset_resources.quickxor,
                           archive_files.remote_path
                    FROM asset_resources
                    JOIN archive_files ON archive_files.resource_id = asset_resources.id
                    WHERE asset_resources.asset_id = ? AND asset_resources.required = 1
                    ORDER BY asset_resources.resource_key
                    """,
                    (row["id"],),
                ).fetchall()
                expected_keys = sorted(resource.resource_key for resource in asset.resources)
                evidence_keys = [str(item["resource_key"]) for item in evidence]
                if not evidence or evidence_keys != expected_keys:
                    raise PhoneSafetyError("complete iCloud archive evidence is missing")
                planned_assets.append(
                    {
                        "local_identifier": asset.local_identifier,
                        "creation_at_utc": asset.creation_at_utc.isoformat().replace(
                            "+00:00", "Z"
                        ),
                        "resource_keys": expected_keys,
                        "resources": [
                            {
                                "resource_key": str(item["resource_key"]),
                                "remote_path": str(item["remote_path"]),
                                "size": int(item["size"]),
                                "sha256": str(item["sha256"]),
                                "quickxor": str(item["quickxor"]),
                            }
                            for item in evidence
                        ],
                    }
                )
        return {
            "schema_version": 1,
            "batch_id": str(batch["batch_id"]),
            "profile_id": str(batch["profile_id"]),
            "library_id": str(batch["library_id"]),
            "cutoff_at_utc": str(batch["cutoff_at_utc"]),
            "assets": planned_assets,
        }

    def _cleanup_plan(
        self, batch: Any, assets: tuple[PhotoLibraryAsset, ...]
    ) -> ICloudCleanupPlan:
        payload = self._plan_payload(batch, assets)
        digest = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        total_bytes = sum(
            int(resource["size"])
            for asset in payload["assets"]
            for resource in asset["resources"]
        )
        return ICloudCleanupPlan(
            batch_id=str(batch["batch_id"]),
            profile_id=str(batch["profile_id"]),
            cutoff_at_utc=str(batch["cutoff_at_utc"]),
            plan_sha256=digest,
            assets=assets,
            total_bytes=total_bytes,
        )

    def _reconcile_delete_intents(
        self, batch_id: str, session: PhotoLibrarySession
    ) -> None:
        batch = self.database.get_icloud_batch(batch_id)
        intents = self._asset_references(batch_id, {"DELETE_INTENT"})
        if not intents:
            return
        validation = session.revalidate(intents, cutoff_at_utc=str(batch["cutoff_at_utc"]))
        valid = set(validation.valid_local_identifiers)
        missing = set(validation.missing_local_identifiers)
        rows = {
            str(row["photos_local_id"]): row
            for row in self.database.list_icloud_batch_assets(batch_id)
        }
        for asset in intents:
            row = rows[asset.local_identifier]
            if asset.local_identifier in missing:
                self.database.set_icloud_asset_state(str(row["batch_asset_id"]), "DELETED")
            elif asset.local_identifier in valid:
                self.database.set_icloud_asset_state(str(row["batch_asset_id"]), "READY")
            else:
                self.database.set_icloud_asset_state(
                    str(row["batch_asset_id"]),
                    "FAILED",
                    error_code="ICLOUD_ASSET_CHANGED_AFTER_DELETE_INTENT",
                )
        if all(
            row["cleanup_state"] == "DELETED"
            for row in self.database.list_icloud_batch_assets(batch_id)
        ):
            self.database.set_icloud_batch_state(batch_id, "COMPLETED")

    @staticmethod
    def _new_batch_id() -> str:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"icloud-{timestamp}-{uuid.uuid4().hex[:8]}"
