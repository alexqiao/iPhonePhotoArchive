from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
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
class LargeVideoScanSummary:
    authorization: str
    total_assets: int
    total_resources: int
    total_video_assets: int
    archived_assets: int
    measured_large_assets: int
    measured_not_large_assets: int
    pending_measurement_assets: int
    unmeasurable_assets: int
    threshold_bytes: int
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
    selection_mode: str = "age_cutoff"
    selection_threshold_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ICloudPipelineCleanupChunk:
    batch_id: str
    local_identifiers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ICloudPipelineResumeBatch:
    batch_id: str
    local_identifiers: tuple[str, ...]
    incomplete_assets: int


@dataclass(frozen=True, slots=True)
class ICloudPipelineScope:
    profile_id: str
    selection_mode: str
    cutoff_at_utc: str
    selection_threshold_bytes: int | None
    batch_size: int
    cleanup_chunks: tuple[ICloudPipelineCleanupChunk, ...]
    resume_batches: tuple[ICloudPipelineResumeBatch, ...]
    new_scan: PhotoLibraryScan
    authorization_sha256: str
    first_batch_size: int = 50
    historical_thresholds: tuple[int, ...] = ()

    @property
    def ready_assets(self) -> int:
        return sum(len(chunk.local_identifiers) for chunk in self.cleanup_chunks)

    @property
    def resume_assets(self) -> int:
        return sum(len(batch.local_identifiers) for batch in self.resume_batches)

    @property
    def new_assets(self) -> int:
        return len(self.new_scan.assets)

    @property
    def authorized_assets(self) -> int:
        return self.ready_assets + self.resume_assets + self.new_assets


@dataclass(frozen=True, slots=True)
class ICloudPipelineResult:
    batch_ids: tuple[str, ...]
    completed_chunks: int
    archived_assets: int
    deleted: int
    failed: int


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

    @staticmethod
    def _short_asset_key(local_identifier: str) -> str:
        return hashlib.sha256(local_identifier.encode()).hexdigest()[:8]

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

    def scan_large_videos(
        self, profile_id: str, session: PhotoLibrarySession | None = None
    ) -> tuple[PhotoLibraryScan, LargeVideoScanSummary]:
        self.database.get_profile(profile_id)
        owns_session = session is None
        active = session or self.client.open_session()
        self._progress("ICLOUD_LARGE_VIDEO_SCAN_STARTED", profile_id=profile_id)
        try:
            scan = active.scan_large_videos()
        finally:
            if owns_session:
                active.close()
        scan = PhotoLibraryScan(
            authorization=scan.authorization,
            assets=tuple(asset for asset in scan.assets if asset.media_type == "video"),
            total_assets=scan.total_assets,
            total_resources=scan.total_resources,
            warnings=scan.warnings,
        )
        threshold = self.config.archive_policy.large_video_threshold_bytes
        measurements = self.database.icloud_video_measurements(profile_id, threshold)
        archived = self.database.verified_icloud_local_ids(profile_id)
        large = 0
        not_large = 0
        pending = 0
        unmeasurable = 0
        for asset in scan.assets:
            if asset.local_identifier in archived:
                continue
            originals = self._original_video_resources(asset)
            if not originals:
                unmeasurable += 1
                continue
            rows = [
                measurements.get((asset.local_identifier, resource.resource_key))
                for resource in originals
            ]
            if any(row is not None and bool(row["exceeds_threshold"]) for row in rows):
                large += 1
            elif all(row is not None for row in rows):
                not_large += 1
            else:
                pending += 1
        summary = LargeVideoScanSummary(
            authorization=scan.authorization,
            total_assets=scan.total_assets,
            total_resources=scan.total_resources,
            total_video_assets=len(scan.assets),
            archived_assets=sum(
                asset.local_identifier in archived for asset in scan.assets
            ),
            measured_large_assets=large,
            measured_not_large_assets=not_large,
            pending_measurement_assets=pending,
            unmeasurable_assets=unmeasurable,
            threshold_bytes=threshold,
            warnings=scan.warnings,
        )
        self._progress(
            "ICLOUD_LARGE_VIDEO_SCAN_COMPLETED",
            videos=summary.total_video_assets,
            large=large,
            pending=pending,
            threshold_bytes=threshold,
        )
        return scan, summary

    def select_large_videos(
        self,
        profile_id: str,
        session: PhotoLibrarySession,
        scan: PhotoLibraryScan,
        *,
        limit: int | None = None,
    ) -> PhotoLibraryScan:
        threshold = self.config.archive_policy.large_video_threshold_bytes
        measurements = self.database.icloud_video_measurements(profile_id, threshold)
        archived = self.database.verified_icloud_local_ids(profile_id)
        selected: list[PhotoLibraryAsset] = []
        candidates = sorted(
            (
                asset
                for asset in scan.assets
                if asset.local_identifier not in archived and asset.media_type == "video"
            ),
            key=lambda asset: (asset.creation_at_utc, asset.local_identifier),
        )
        for position, asset in enumerate(candidates, start=1):
            originals = self._original_video_resources(asset)
            if not originals:
                self._progress(
                    "ICLOUD_LARGE_VIDEO_UNMEASURABLE",
                    asset_key=self._short_asset_key(asset.local_identifier),
                    reason="MISSING_ORIGINAL_VIDEO_RESOURCE",
                )
                continue
            is_large = False
            for resource in originals:
                row = measurements.get((asset.local_identifier, resource.resource_key))
                if row is None:
                    self._progress(
                        "ICLOUD_LARGE_VIDEO_MEASURE_STARTED",
                        current=position,
                        total=len(candidates),
                        asset_key=self._short_asset_key(asset.local_identifier),
                    )
                    probe = session.probe_resource_size(
                        asset,
                        resource.resource_key,
                        threshold,
                    )
                    self.database.record_icloud_video_measurement(
                        profile_id,
                        asset.local_identifier,
                        resource.resource_key,
                        threshold,
                        observed_bytes=probe.observed_bytes,
                        complete=probe.complete,
                        exceeds_threshold=probe.exceeds_threshold,
                    )
                    exceeds = probe.exceeds_threshold
                    self._progress(
                        "ICLOUD_LARGE_VIDEO_MEASURE_COMPLETED",
                        current=position,
                        total=len(candidates),
                        asset_key=self._short_asset_key(asset.local_identifier),
                        exceeds_threshold=exceeds,
                        observed_bytes=probe.observed_bytes,
                    )
                else:
                    exceeds = bool(row["exceeds_threshold"])
                if exceeds:
                    is_large = True
                    break
            if is_large:
                selected.append(asset)
                if limit is not None and len(selected) >= limit:
                    break
        return PhotoLibraryScan(
            authorization=scan.authorization,
            assets=tuple(selected),
            total_assets=scan.total_assets,
            total_resources=scan.total_resources,
            warnings=scan.warnings,
        )

    @staticmethod
    def _original_video_resources(
        asset: PhotoLibraryAsset,
    ) -> tuple[PhotoLibraryResource, ...]:
        return tuple(resource for resource in asset.resources if resource.resource_type_code == 2)

    def archive_scan(
        self,
        profile_id: str,
        session: PhotoLibrarySession,
        scan: PhotoLibraryScan,
        *,
        candidate_limit: int | None = None,
        selection_mode: str = "age_cutoff",
        selection_threshold_bytes: int | None = None,
    ) -> ArchivedICloudBatch:
        if not self.config.icloud_cleanup.enabled:
            raise PhoneSafetyError("iCloud cleanup is disabled in configuration")
        if selection_mode not in {"age_cutoff", "large_video"}:
            raise ValueError(f"unknown iCloud selection mode: {selection_mode}")
        if selection_mode == "large_video" and selection_threshold_bytes is None:
            raise ValueError("large-video archive requires a frozen size threshold")
        if selection_mode == "age_cutoff" and selection_threshold_bytes is not None:
            raise ValueError("age-cutoff archive cannot have a video size threshold")
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
            selection_mode=selection_mode,
            selection_threshold_bytes=selection_threshold_bytes,
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

    def build_pipeline_scope(
        self,
        profile_id: str,
        scan: PhotoLibraryScan,
        *,
        selection_mode: str = "age_cutoff",
        selection_threshold_bytes: int | None = None,
        first_batch_size: int = 50,
    ) -> ICloudPipelineScope:
        """Freeze all work authorized by one interactive sync-all confirmation."""
        if selection_mode not in {"age_cutoff", "large_video"}:
            raise ValueError(f"unknown iCloud selection mode: {selection_mode}")
        if selection_mode == "large_video" and selection_threshold_bytes is None:
            raise ValueError("large-video pipeline requires a frozen size threshold")
        if selection_mode == "age_cutoff" and selection_threshold_bytes is not None:
            raise ValueError("age-cutoff pipeline cannot have a video size threshold")

        batch_size = self.config.icloud_cleanup.batch_size
        if not 1 <= first_batch_size <= batch_size:
            raise ValueError(
                "first iCloud pipeline batch size must be between 1 and the configured batch size"
            )
        cleanup_chunks: list[ICloudPipelineCleanupChunk] = []
        resume_batches: list[ICloudPipelineResumeBatch] = []
        claimed: set[str] = set()
        historical_thresholds: set[int] = set()
        historical_policies: list[dict[str, Any]] = []

        for batch in self.database.list_icloud_pipeline_batches(
            profile_id, selection_mode
        ):
            batch_id = str(batch["batch_id"])
            rows = [
                row
                for row in self.database.list_icloud_batch_assets(batch_id)
                if row["cleanup_state"] != "DELETED"
            ]
            if not rows:
                continue
            incomplete = [
                row
                for row in rows
                if row["state"] != "SAFE_TO_DELETE" or bool(row["review_required"])
            ]
            can_resume = (
                bool(incomplete)
                and len(rows) <= batch_size
                and batch["state"] in {"ARCHIVING", "NEEDS_ATTENTION"}
            )
            threshold = batch["selection_threshold_bytes"]

            if can_resume:
                identifiers = tuple(str(row["photos_local_id"]) for row in rows)
                overlap = claimed.intersection(identifiers)
                if overlap:
                    raise PhoneSafetyError(
                        "the same iCloud asset appears in multiple pending pipeline batches"
                    )
                claimed.update(identifiers)
                resume_batches.append(
                    ICloudPipelineResumeBatch(
                        batch_id=batch_id,
                        local_identifiers=identifiers,
                        incomplete_assets=len(incomplete),
                    )
                )
                historical_policies.append(
                    {
                        "batch_id": batch_id,
                        "cutoff_at_utc": str(batch["cutoff_at_utc"]),
                        "selection_threshold_bytes": threshold,
                    }
                )
                if threshold is not None:
                    historical_thresholds.add(int(threshold))
                continue

            ready = [
                row
                for row in rows
                if row["state"] == "SAFE_TO_DELETE" and not bool(row["review_required"])
            ]
            identifiers = tuple(str(row["photos_local_id"]) for row in ready)
            overlap = claimed.intersection(identifiers)
            if overlap:
                raise PhoneSafetyError(
                    "the same iCloud asset appears in multiple pending cleanup batches"
                )
            claimed.update(identifiers)
            cleanup_chunks.extend(
                ICloudPipelineCleanupChunk(batch_id, tuple(group))
                for group in batched(identifiers, batch_size)
            )
            if identifiers:
                historical_policies.append(
                    {
                        "batch_id": batch_id,
                        "cutoff_at_utc": str(batch["cutoff_at_utc"]),
                        "selection_threshold_bytes": threshold,
                    }
                )
                if threshold is not None:
                    historical_thresholds.add(int(threshold))

        archived = self.database.verified_icloud_local_ids(profile_id)
        fresh_assets = tuple(
            sorted(
                (
                    asset
                    for asset in scan.assets
                    if asset.local_identifier not in archived
                    and asset.local_identifier not in claimed
                ),
                key=lambda asset: (asset.creation_at_utc, asset.local_identifier),
            )
        )
        new_scan = PhotoLibraryScan(
            authorization=scan.authorization,
            assets=fresh_assets,
            total_assets=scan.total_assets,
            total_resources=scan.total_resources,
            warnings=scan.warnings,
        )
        authorization_payload = {
            "version": 1,
            "profile_id": profile_id,
            "selection_mode": selection_mode,
            "cutoff_at_utc": self._cutoff(),
            "selection_threshold_bytes": selection_threshold_bytes,
            "batch_size": batch_size,
            "first_batch_size": first_batch_size,
            "cleanup_chunks": [
                {
                    "batch_id": chunk.batch_id,
                    "local_identifiers": list(chunk.local_identifiers),
                }
                for chunk in cleanup_chunks
            ],
            "resume_batches": [
                {
                    "batch_id": batch.batch_id,
                    "local_identifiers": list(batch.local_identifiers),
                }
                for batch in resume_batches
            ],
            "new_local_identifiers": [
                asset.local_identifier for asset in fresh_assets
            ],
            "historical_policies": historical_policies,
        }
        authorization_sha256 = hashlib.sha256(
            json.dumps(
                authorization_payload, separators=(",", ":"), sort_keys=True
            ).encode()
        ).hexdigest()
        return ICloudPipelineScope(
            profile_id=profile_id,
            selection_mode=selection_mode,
            cutoff_at_utc=self._cutoff(),
            selection_threshold_bytes=selection_threshold_bytes,
            batch_size=batch_size,
            cleanup_chunks=tuple(cleanup_chunks),
            resume_batches=tuple(resume_batches),
            new_scan=new_scan,
            authorization_sha256=authorization_sha256,
            first_batch_size=first_batch_size,
            historical_thresholds=tuple(sorted(historical_thresholds)),
        )

    def execute_pipeline(
        self,
        scope: ICloudPipelineScope,
        session: PhotoLibrarySession,
        *,
        batch_completed: Callable[[str], None] | None = None,
    ) -> ICloudPipelineResult:
        """Execute a previously frozen, interactively authorized pipeline scope."""
        authorized = {
            identifier
            for chunk in scope.cleanup_chunks
            for identifier in chunk.local_identifiers
        }
        authorized.update(
            identifier
            for batch in scope.resume_batches
            for identifier in batch.local_identifiers
        )
        authorized.update(asset.local_identifier for asset in scope.new_scan.assets)
        if len(authorized) != scope.authorized_assets:
            raise PhoneSafetyError("the frozen iCloud pipeline scope contains duplicates")

        cleanup_requests: list[tuple[str, frozenset[str]]] = [
            (chunk.batch_id, frozenset(chunk.local_identifiers))
            for chunk in scope.cleanup_chunks
        ]
        archived_assets = 0
        first_archive_completed = False

        for pending in scope.resume_batches:
            self.resume_archive(pending.batch_id, session)
            rows_by_identifier = {
                str(row["photos_local_id"]): row
                for row in self.database.list_icloud_batch_assets(pending.batch_id)
            }
            if any(
                identifier not in rows_by_identifier
                or rows_by_identifier[identifier]["state"] != "SAFE_TO_DELETE"
                or bool(rows_by_identifier[identifier]["review_required"])
                for identifier in pending.local_identifiers
            ):
                raise PhoneSafetyError(
                    f"iCloud pipeline stopped because {pending.batch_id} is not fully verified"
                )
            archived_assets += pending.incomplete_assets
            first_archive_completed = True
            cleanup_requests.append(
                (pending.batch_id, frozenset(pending.local_identifiers))
            )

        remaining_assets = list(scope.new_scan.assets)
        while remaining_assets:
            size = (
                scope.batch_size
                if first_archive_completed
                else scope.first_batch_size
            )
            assets = tuple(remaining_assets[:size])
            del remaining_assets[:size]
            subset = PhotoLibraryScan(
                authorization=scope.new_scan.authorization,
                assets=assets,
                total_assets=scope.new_scan.total_assets,
                total_resources=scope.new_scan.total_resources,
                warnings=scope.new_scan.warnings,
            )
            archived = self.archive_scan(
                scope.profile_id,
                session,
                subset,
                candidate_limit=len(assets),
                selection_mode=scope.selection_mode,
                selection_threshold_bytes=scope.selection_threshold_bytes,
            )
            if archived.counts.get("SAFE_TO_DELETE", 0) != len(assets):
                raise PhoneSafetyError(
                    f"iCloud pipeline stopped because {archived.batch_id} is not fully verified"
                )
            archived_assets += len(assets)
            first_archive_completed = True
            cleanup_requests.append(
                (
                    archived.batch_id,
                    frozenset(asset.local_identifier for asset in assets),
                )
            )

        plans: list[ICloudCleanupPlan] = []
        for batch_id, expected in cleanup_requests:
            plan = self._prepare_pipeline_cleanup_plan(
                batch_id,
                expected,
                authorized,
                session,
            )
            if plan is not None:
                plans.append(plan)

        result = (
            self.execute_aggregate_cleanup(tuple(plans), session)
            if plans
            else {"deleted": 0, "failed": 0}
        )
        if result["failed"]:
            raise PhoneSafetyError("iCloud pipeline stopped after aggregate cleanup failure")

        batch_ids = tuple(dict.fromkeys(batch_id for batch_id, _ in cleanup_requests))
        if batch_completed is not None:
            for batch_id in batch_ids:
                batch_completed(batch_id)

        return ICloudPipelineResult(
            batch_ids=batch_ids,
            completed_chunks=len(plans),
            archived_assets=archived_assets,
            deleted=result["deleted"],
            failed=result["failed"],
        )

    def _prepare_pipeline_cleanup_plan(
        self,
        batch_id: str,
        expected: frozenset[str],
        authorized: set[str],
        session: PhotoLibrarySession,
    ) -> ICloudCleanupPlan | None:
        if not expected or not expected.issubset(authorized):
            raise PhoneSafetyError("iCloud cleanup escaped the frozen pipeline scope")
        batch = self.database.get_icloud_batch(batch_id)
        if batch["state"] == "CLEANING_ICLOUD":
            self._reconcile_delete_intents(batch_id, session)
            expected = frozenset(
                str(row["photos_local_id"])
                for row in self.database.list_icloud_batch_assets(batch_id)
                if row["cleanup_state"] != "DELETED"
                and str(row["photos_local_id"]) in expected
            )
            if not expected:
                return None
        plan = self.prepare_cleanup(
            batch_id,
            session,
            local_identifiers=expected,
        )
        planned = {asset.local_identifier for asset in plan.assets}
        if planned != expected or not planned.issubset(authorized):
            raise PhoneSafetyError("iCloud deletion plan changed outside the authorized scope")
        return plan

    def execute_aggregate_cleanup(
        self,
        plans: tuple[ICloudCleanupPlan, ...],
        session: PhotoLibrarySession,
    ) -> dict[str, int]:
        """Delete multiple exact batch plans in one PhotoKit change transaction."""
        if not plans:
            raise PhoneSafetyError("aggregate iCloud cleanup requires at least one plan")
        profile_ids = {plan.profile_id for plan in plans}
        selection_modes = {plan.selection_mode for plan in plans}
        cutoffs = {plan.cutoff_at_utc for plan in plans}
        if len(profile_ids) != 1 or len(selection_modes) != 1 or len(cutoffs) != 1:
            raise PhoneSafetyError(
                "single-transaction iCloud cleanup requires one profile, selection mode, and cutoff"
            )

        assets: list[PhotoLibraryAsset] = []
        rows_by_local_id: dict[str, Any] = {}
        payload_plans: list[dict[str, Any]] = []
        for plan in plans:
            batch = self.database.get_icloud_batch(plan.batch_id)
            payload = self._plan_payload(batch, plan.assets)
            digest = hashlib.sha256(
                json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest()
            if digest != plan.plan_sha256:
                raise PhoneSafetyError("iCloud deletion plan changed after confirmation")
            batch_rows = {
                str(row["photos_local_id"]): row
                for row in self.database.list_icloud_batch_assets(plan.batch_id)
            }
            for asset in plan.assets:
                if asset.local_identifier in rows_by_local_id:
                    raise PhoneSafetyError(
                        "the aggregate iCloud deletion plan contains duplicate assets"
                    )
                rows_by_local_id[asset.local_identifier] = batch_rows[
                    asset.local_identifier
                ]
                assets.append(asset)
            payload_plans.append(
                {
                    "batch_id": plan.batch_id,
                    "plan_sha256": plan.plan_sha256,
                    "local_identifiers": [
                        asset.local_identifier for asset in plan.assets
                    ],
                }
            )

        aggregate_sha256 = hashlib.sha256(
            json.dumps(payload_plans, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        for plan in plans:
            self.database.set_icloud_batch_state(
                plan.batch_id,
                "CLEANING_ICLOUD",
                plan_sha256=plan.plan_sha256,
                confirmed=True,
            )
            for asset in plan.assets:
                self.database.set_icloud_asset_state(
                    str(rows_by_local_id[asset.local_identifier]["batch_asset_id"]),
                    "DELETE_INTENT",
                )

        self._progress("ICLOUD_DELETE_STARTED", assets=len(assets))
        selection_mode = plans[0].selection_mode
        result = session.delete(
            tuple(assets),
            batch_id=f"icloud-aggregate-{aggregate_sha256[:16]}",
            cutoff_at_utc=plans[0].cutoff_at_utc,
            plan_sha256=aggregate_sha256,
            **(
                {"selection_mode": selection_mode}
                if selection_mode == "large_video"
                else {}
            ),
        )
        deleted = set(result.deleted_local_identifiers)
        reported_failed = set(result.failed_local_identifiers)
        for asset in assets:
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
                    error_detail=result.failure_reason
                    or "PhotoKit did not confirm aggregate deletion",
                )

        for plan in plans:
            remaining = sum(
                row["cleanup_state"] != "DELETED"
                for row in self.database.list_icloud_batch_assets(plan.batch_id)
            )
            self.database.set_icloud_batch_state(
                plan.batch_id,
                "COMPLETED" if remaining == 0 else "COMPLETED_WITH_ITEMS_REMAINING",
            )
        self._progress(
            "ICLOUD_CLEANUP_COMPLETED",
            deleted=len(deleted),
            failed=len(reported_failed),
        )
        return {"deleted": len(deleted), "failed": len(reported_failed)}

    def archive_all_batches(
        self,
        profile_id: str,
        session: PhotoLibrarySession,
        scan: PhotoLibraryScan,
        *,
        selection_mode: str = "age_cutoff",
        selection_threshold_bytes: int | None = None,
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
                selection_mode=selection_mode,
                selection_threshold_bytes=selection_threshold_bytes,
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
        try:
            counts = ArchiveRunner(
                self.config,
                self.database,
                source,
                ExternalDriveClient(self.config.external_drive),
                progress=self.progress,
            ).run_job(str(batch["job_id"]), resume=True)
        except BaseException:
            self.database.set_icloud_batch_state(
                batch_id, "NEEDS_ATTENTION", error_code="ARCHIVE_INTERRUPTED"
            )
            raise
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
        selection_mode = str(batch["selection_mode"])
        if selection_mode == "large_video":
            validation = session.revalidate(
                candidates,
                cutoff_at_utc=str(batch["cutoff_at_utc"]),
                selection_mode=selection_mode,
            )
        else:
            validation = session.revalidate(
                candidates, cutoff_at_utc=str(batch["cutoff_at_utc"])
            )
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
            **(
                {"selection_mode": plan.selection_mode}
                if plan.selection_mode == "large_video"
                else {}
            ),
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
            if row["selection_mode"] == "large_video":
                ready = (
                    ready
                    and row["media_type"] == "video"
                    and row["selection_threshold_bytes"] is not None
                    and int(row["qualifying_original_video_count"] or 0) > 0
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
        base = {
            "schema_version": 1,
            "batch_id": str(batch["batch_id"]),
            "profile_id": str(batch["profile_id"]),
            "library_id": str(batch["library_id"]),
            "cutoff_at_utc": str(batch["cutoff_at_utc"]),
            "assets": planned_assets,
        }
        if batch["selection_mode"] == "large_video":
            threshold = batch["selection_threshold_bytes"]
            if threshold is None:
                raise PhoneSafetyError("large-video batch is missing its size threshold")
            ordered_assets = sorted(assets, key=lambda item: item.local_identifier)
            for planned, asset in zip(planned_assets, ordered_assets, strict=True):
                qualifying_keys = sorted(
                    resource.resource_key
                    for resource in asset.resources
                    if resource.resource_type_code == 2
                    and any(
                        evidence["resource_key"] == resource.resource_key
                        and int(evidence["size"]) > int(threshold)
                        for evidence in planned["resources"]
                    )
                )
                if not qualifying_keys:
                    raise PhoneSafetyError(
                        "large-video deletion lacks a verified original above the threshold"
                    )
                planned["qualifying_original_video_resource_keys"] = qualifying_keys
            base.update(
                {
                    "schema_version": 2,
                    "selection_mode": "large_video",
                    "selection_threshold_bytes": int(threshold),
                }
            )
        return base

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
            selection_mode=str(batch["selection_mode"]),
            selection_threshold_bytes=(
                int(batch["selection_threshold_bytes"])
                if batch["selection_threshold_bytes"] is not None
                else None
            ),
        )

    def _reconcile_delete_intents(
        self, batch_id: str, session: PhotoLibrarySession
    ) -> None:
        batch = self.database.get_icloud_batch(batch_id)
        intents = self._asset_references(batch_id, {"DELETE_INTENT"})
        if not intents:
            return
        if batch["selection_mode"] == "large_video":
            validation = session.revalidate(
                intents,
                cutoff_at_utc=str(batch["cutoff_at_utc"]),
                selection_mode="large_video",
            )
        else:
            validation = session.revalidate(
                intents, cutoff_at_utc=str(batch["cutoff_at_utc"])
            )
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
