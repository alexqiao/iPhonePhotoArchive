from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from photoarchive.config import AppConfig
from photoarchive.database import Database
from photoarchive.domain import (
    PhoneSafetyError,
    PhotoLibraryAsset,
    PhotoLibraryDeleteResult,
    PhotoLibraryResource,
    PhotoLibraryRevalidation,
    PhotoLibraryScan,
    PhotoLibrarySizeProbe,
)
from photoarchive.fake_archive import FakeArchiveTarget
from photoarchive.icloud_workflow import ArchivedICloudBatch, ICloudWorkflow
from photoarchive.reporting import generate_report


class FakePhotoLibrarySession:
    def __init__(self, scan: PhotoLibraryScan, content: dict[str, bytes]) -> None:
        self.current = {asset.local_identifier: asset for asset in scan.assets}
        self.scan_result = scan
        self.content = content
        self.downloaded: list[str] = []
        self.probed: list[str] = []
        self.deleted: list[str] = []
        self.events: list[tuple[str, tuple[str, ...]]] = []
        self.closed = False

    def scan(self, cutoff_at_utc: str, media_types: frozenset[str]) -> PhotoLibraryScan:
        assert cutoff_at_utc
        assert media_types
        return self.scan_result

    def scan_large_videos(self) -> PhotoLibraryScan:
        return self.scan_result

    def probe_resource_size(
        self,
        asset: PhotoLibraryAsset,
        resource_key: str,
        threshold_bytes: int,
    ) -> PhotoLibrarySizeProbe:
        assert asset.local_identifier in self.current
        self.probed.append(resource_key)
        size = len(self.content[resource_key])
        exceeds = size > threshold_bytes
        return PhotoLibrarySizeProbe(
            observed_bytes=min(size, threshold_bytes + 1),
            complete=not exceeds,
            exceeds_threshold=exceeds,
        )

    def download(
        self, asset: PhotoLibraryAsset, resource_key: str, partial_path: Path
    ) -> int:
        assert asset.local_identifier in self.current
        self.downloaded.append(resource_key)
        self.events.append(("archive", (asset.local_identifier,)))
        payload = self.content[resource_key]
        partial_path.write_bytes(payload)
        return len(payload)

    def revalidate(
        self,
        assets: tuple[PhotoLibraryAsset, ...],
        *,
        cutoff_at_utc: str,
        selection_mode: str = "age_cutoff",
    ) -> PhotoLibraryRevalidation:
        cutoff = datetime.fromisoformat(cutoff_at_utc.replace("Z", "+00:00"))
        valid: list[str] = []
        missing: list[str] = []
        mismatched: list[str] = []
        for expected in assets:
            current = self.current.get(expected.local_identifier)
            if current is None:
                missing.append(expected.local_identifier)
            elif (
                (selection_mode == "age_cutoff" and current.creation_at_utc >= cutoff)
                or (selection_mode == "large_video" and current.media_type != "video")
                or {item.resource_key for item in current.resources}
                != {item.resource_key for item in expected.resources}
            ):
                mismatched.append(expected.local_identifier)
            else:
                valid.append(expected.local_identifier)
        return PhotoLibraryRevalidation(tuple(valid), tuple(missing), tuple(mismatched))

    def delete(
        self,
        assets: tuple[PhotoLibraryAsset, ...],
        *,
        batch_id: str,
        cutoff_at_utc: str,
        plan_sha256: str,
        selection_mode: str = "age_cutoff",
    ) -> PhotoLibraryDeleteResult:
        assert batch_id.startswith("icloud-")
        assert cutoff_at_utc
        assert len(plan_sha256) == 64
        assert selection_mode in {"age_cutoff", "large_video"}
        identifiers = tuple(asset.local_identifier for asset in assets)
        self.events.append(("delete", identifiers))
        for identifier in identifiers:
            self.current.pop(identifier)
            self.deleted.append(identifier)
        return PhotoLibraryDeleteResult(identifiers, ())

    def close(self) -> None:
        self.closed = True


class FakePhotoLibraryClient:
    def __init__(self, session: FakePhotoLibrarySession) -> None:
        self.session = session

    def open_session(self) -> FakePhotoLibrarySession:
        return self.session


class ReadyFakeArchive(FakeArchiveTarget):
    def assert_ready(self, required_bytes: int = 0) -> None:
        del required_bytes


class CountingReadyFakeArchive(ReadyFakeArchive):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.stat_calls = 0

    def stat(self, remote_path: str):
        self.stat_calls += 1
        return super().stat(remote_path)


def icloud_config(app_config: AppConfig) -> AppConfig:
    return app_config.model_copy(
        update={
            "icloud_cleanup": app_config.icloud_cleanup.model_copy(update={"enabled": True})
        }
    )


def library_scan() -> PhotoLibraryScan:
    photo = PhotoLibraryResource("photo-key", "photo", 1, "IMG_1000.HEIC", "public.heic", 0)
    video = PhotoLibraryResource(
        "video-key", "paired_video", 9, "IMG_1000.MOV", "com.apple.quicktime-movie", 0
    )
    asset = PhotoLibraryAsset(
        "asset-local-id",
        datetime(2020, 1, 2, tzinfo=UTC),
        "live_photo",
        (photo, video),
    )
    return PhotoLibraryScan("authorized", (asset,), 12, 15)


def multi_asset_scan(count: int) -> tuple[PhotoLibraryScan, dict[str, bytes]]:
    assets: list[PhotoLibraryAsset] = []
    content: dict[str, bytes] = {}
    for index in range(count):
        key = f"photo-key-{index}"
        content[key] = f"photo-{index}".encode()
        assets.append(
            PhotoLibraryAsset(
                f"asset-local-id-{index}",
                datetime(2020, 1, index + 1, tzinfo=UTC),
                "image",
                (
                    PhotoLibraryResource(
                        key,
                        "photo",
                        1,
                        f"IMG_{index:04d}.HEIC",
                        "public.heic",
                        0,
                    ),
                ),
            )
        )
    return PhotoLibraryScan("authorized", tuple(assets), count, count), content


def large_video_scan() -> tuple[PhotoLibraryScan, dict[str, bytes]]:
    threshold = 5
    equal = PhotoLibraryAsset(
        "video-equal",
        datetime(2030, 1, 1, tzinfo=UTC),
        "video",
        (
            PhotoLibraryResource(
                "equal-original", "video", 2, "EQUAL.MOV", "com.apple.quicktime-movie", 0
            ),
        ),
    )
    large = PhotoLibraryAsset(
        "video-large",
        datetime(2031, 1, 1, tzinfo=UTC),
        "video",
        (
            PhotoLibraryResource(
                "large-original", "video", 2, "LARGE.MOV", "com.apple.quicktime-movie", 0
            ),
            PhotoLibraryResource(
                "large-adjusted",
                "full_size_video",
                6,
                "LARGE-EDIT.MOV",
                "com.apple.quicktime-movie",
                0,
            ),
        ),
    )
    live = PhotoLibraryAsset(
        "live-photo",
        datetime(2032, 1, 1, tzinfo=UTC),
        "live_photo",
        (
            PhotoLibraryResource(
                "live-paired",
                "paired_video",
                9,
                "LIVE.MOV",
                "com.apple.quicktime-movie",
                0,
            ),
        ),
    )
    missing_original = PhotoLibraryAsset(
        "video-missing-original",
        datetime(2033, 1, 1, tzinfo=UTC),
        "video",
        (
            PhotoLibraryResource(
                "video-adjustment-only",
                "full_size_video",
                6,
                "EDIT.MOV",
                "com.apple.quicktime-movie",
                0,
            ),
        ),
    )
    content = {
        "equal-original": b"e" * threshold,
        "large-original": b"l" * (threshold + 1),
        "large-adjusted": b"edited",
        "live-paired": b"live-video",
        "video-adjustment-only": b"edited-video",
    }
    return PhotoLibraryScan(
        "authorized", (equal, large, live, missing_original), 4, 5
    ), content


def test_icloud_archive_uses_bounded_batches_and_skips_verified_assets(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = icloud_config(app_config)
    config = base.model_copy(
        update={
            "icloud_cleanup": base.icloud_cleanup.model_copy(update={"batch_size": 2})
        }
    )
    remote = ReadyFakeArchive(config.storage.fake_archive_root)
    monkeypatch.setattr("photoarchive.icloud_workflow.ExternalDriveClient", lambda _: remote)
    scan, content = multi_asset_scan(5)
    session = FakePhotoLibrarySession(scan, content)
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))

    first = workflow.archive_scan("wife", session, scan)
    assert first.counts == {"SAFE_TO_DELETE": 2}
    assert len(workflow.unarchived_scan("wife", scan).assets) == 3

    rest = workflow.archive_all_batches("wife", session, scan)
    assert [sum(batch.counts.values()) for batch in rest] == [2, 1]
    assert len(database.verified_icloud_local_ids("wife")) == 5


def test_sync_all_pipeline_archives_deletes_and_continues_in_bounded_order(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = icloud_config(app_config)
    config = base.model_copy(
        update={
            "icloud_cleanup": base.icloud_cleanup.model_copy(update={"batch_size": 2})
        }
    )
    remote = ReadyFakeArchive(config.storage.fake_archive_root)
    monkeypatch.setattr("photoarchive.icloud_workflow.ExternalDriveClient", lambda _: remote)
    scan, content = multi_asset_scan(5)
    session = FakePhotoLibrarySession(scan, content)
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))

    scope = workflow.build_pipeline_scope("wife", scan)
    late_asset = PhotoLibraryAsset(
        "late-asset",
        datetime(2020, 2, 1, tzinfo=UTC),
        "image",
        (),
    )
    session.current[late_asset.local_identifier] = late_asset
    result = workflow.execute_pipeline(scope, session)

    assert scope.authorized_assets == 5
    assert len(scope.authorization_sha256) == 64
    assert result.archived_assets == 5
    assert result.deleted == 5
    assert "late-asset" in session.current
    assert result.completed_chunks == 3
    assert session.events == [
        ("archive", ("asset-local-id-0",)),
        ("archive", ("asset-local-id-1",)),
        ("delete", ("asset-local-id-0", "asset-local-id-1")),
        ("archive", ("asset-local-id-2",)),
        ("archive", ("asset-local-id-3",)),
        ("delete", ("asset-local-id-2", "asset-local-id-3")),
        ("archive", ("asset-local-id-4",)),
        ("delete", ("asset-local-id-4",)),
    ]


def test_sync_all_pipeline_drains_verified_backlog_before_new_assets(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = icloud_config(app_config)
    config = base.model_copy(
        update={
            "icloud_cleanup": base.icloud_cleanup.model_copy(update={"batch_size": 2})
        }
    )
    remote = ReadyFakeArchive(config.storage.fake_archive_root)
    monkeypatch.setattr("photoarchive.icloud_workflow.ExternalDriveClient", lambda _: remote)
    scan, content = multi_asset_scan(5)
    session = FakePhotoLibrarySession(scan, content)
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))
    workflow.archive_scan("wife", session, scan)
    session.events.clear()

    scope = workflow.build_pipeline_scope("wife", scan)
    result = workflow.execute_pipeline(scope, session)

    assert scope.ready_assets == 2
    assert scope.new_assets == 3
    assert result.deleted == 5
    assert session.events[0] == (
        "delete",
        ("asset-local-id-0", "asset-local-id-1"),
    )
    assert session.events[1:4] == [
        ("archive", ("asset-local-id-2",)),
        ("archive", ("asset-local-id-3",)),
        ("delete", ("asset-local-id-2", "asset-local-id-3")),
    ]


def test_sync_all_pipeline_resumes_partial_bounded_batch_without_duplicate_archive(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = icloud_config(app_config)
    config = base.model_copy(
        update={
            "icloud_cleanup": base.icloud_cleanup.model_copy(update={"batch_size": 2})
        }
    )
    remote = ReadyFakeArchive(config.storage.fake_archive_root)
    monkeypatch.setattr("photoarchive.icloud_workflow.ExternalDriveClient", lambda _: remote)
    scan, content = multi_asset_scan(2)
    session = FakePhotoLibrarySession(scan, content)
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))
    archived = workflow.archive_scan("wife", session, scan)
    rows = database.list_icloud_batch_assets(archived.batch_id)
    with database.connect() as connection:
        connection.execute(
            "UPDATE assets SET state = 'DISCOVERED' WHERE id = ?",
            (str(rows[1]["id"]),),
        )
        connection.execute(
            "UPDATE icloud_batch_assets SET cleanup_state = 'PENDING' WHERE id = ?",
            (str(rows[1]["batch_asset_id"]),),
        )
        connection.execute(
            "UPDATE icloud_batches SET state = 'NEEDS_ATTENTION' WHERE batch_id = ?",
            (archived.batch_id,),
        )
    session.events.clear()

    def resume(batch_id, active_session):
        assert batch_id == archived.batch_id
        assert active_session is session
        with database.connect() as connection:
            connection.execute(
                "UPDATE assets SET state = 'SAFE_TO_DELETE', review_required = 0 WHERE id = ?",
                (str(rows[1]["id"]),),
            )
            connection.execute(
                "UPDATE icloud_batches SET state = 'READY_FOR_ICLOUD_CLEANUP' "
                "WHERE batch_id = ?",
                (batch_id,),
            )
        return ArchivedICloudBatch(batch_id, archived.job_id, {"SAFE_TO_DELETE": 2})

    monkeypatch.setattr(workflow, "resume_archive", resume)
    scope = workflow.build_pipeline_scope("wife", scan)

    assert scope.resume_assets == 2
    assert scope.new_assets == 0
    result = workflow.execute_pipeline(scope, session)

    assert result.archived_assets == 1
    assert result.deleted == 2
    assert session.events == [
        ("delete", ("asset-local-id-0", "asset-local-id-1")),
    ]


def test_sync_all_pipeline_uses_exact_1000_1000_501_chunks_without_heavy_io(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = icloud_config(app_config)
    config = base.model_copy(
        update={
            "icloud_cleanup": base.icloud_cleanup.model_copy(update={"batch_size": 1000})
        }
    )
    assets = tuple(
        PhotoLibraryAsset(
            f"asset-{index:04d}",
            datetime(2020, 1, 1, tzinfo=UTC),
            "image",
            (),
        )
        for index in range(2501)
    )
    scan = PhotoLibraryScan("authorized", assets, len(assets), 0)
    session = FakePhotoLibrarySession(scan, {})
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))
    events: list[tuple[str, int]] = []
    batch_number = 0

    def archive_scan(*args, **kwargs):
        nonlocal batch_number
        subset = args[2]
        batch_number += 1
        events.append(("archive", len(subset.assets)))
        return ArchivedICloudBatch(
            f"batch-{batch_number}",
            f"job-{batch_number}",
            {"SAFE_TO_DELETE": len(subset.assets)},
        )

    def cleanup(batch_id, expected, authorized, active_session):
        del batch_id, authorized, active_session
        events.append(("delete", len(expected)))
        return {"deleted": len(expected), "failed": 0}

    monkeypatch.setattr(workflow, "archive_scan", archive_scan)
    monkeypatch.setattr(workflow, "_execute_pipeline_cleanup", cleanup)

    scope = workflow.build_pipeline_scope("wife", scan)
    result = workflow.execute_pipeline(scope, session)

    assert events == [
        ("archive", 1000),
        ("delete", 1000),
        ("archive", 1000),
        ("delete", 1000),
        ("archive", 501),
        ("delete", 501),
    ]
    assert result.completed_chunks == 3
    assert result.deleted == 2501


def test_sync_all_pipeline_stops_before_archiving_the_next_batch_on_delete_failure(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = icloud_config(app_config)
    config = base.model_copy(
        update={
            "icloud_cleanup": base.icloud_cleanup.model_copy(update={"batch_size": 2})
        }
    )
    scan, _ = multi_asset_scan(5)
    session = FakePhotoLibrarySession(scan, {})
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))
    events: list[tuple[str, int]] = []

    def archive_scan(*args, **kwargs):
        subset = args[2]
        events.append(("archive", len(subset.assets)))
        return ArchivedICloudBatch("failed-batch", "failed-job", {"SAFE_TO_DELETE": 2})

    def cleanup(*args, **kwargs):
        events.append(("delete", 2))
        return {"deleted": 0, "failed": 2}

    monkeypatch.setattr(workflow, "archive_scan", archive_scan)
    monkeypatch.setattr(workflow, "_execute_pipeline_cleanup", cleanup)
    scope = workflow.build_pipeline_scope("wife", scan)

    with pytest.raises(PhoneSafetyError, match="stopped after cleanup failure"):
        workflow.execute_pipeline(scope, session)

    assert events == [("archive", 2), ("delete", 2)]


def test_large_video_selection_is_any_date_strict_and_cached(
    app_config: AppConfig,
) -> None:
    base = icloud_config(app_config)
    config = base.model_copy(
        update={
            "archive_policy": base.archive_policy.model_copy(
                update={"large_video_threshold_bytes": 5}
            )
        }
    )
    scan, content = large_video_scan()
    session = FakePhotoLibrarySession(scan, content)
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))

    _, before = workflow.scan_large_videos("wife", session)
    assert before.pending_measurement_assets == 2
    assert before.unmeasurable_assets == 1
    assert session.probed == []

    selected = workflow.select_large_videos("wife", session, scan)
    assert [asset.local_identifier for asset in selected.assets] == ["video-large"]
    assert session.probed == ["equal-original", "large-original"]

    selected_again = workflow.select_large_videos("wife", session, scan)
    assert [asset.local_identifier for asset in selected_again.assets] == ["video-large"]
    assert session.probed == ["equal-original", "large-original"]

    _, after = workflow.scan_large_videos("wife", session)
    assert after.measured_large_assets == 1
    assert after.measured_not_large_assets == 1
    assert after.pending_measurement_assets == 0


def test_large_video_sync_all_pipeline_uses_frozen_threshold_and_excludes_live_photo(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = icloud_config(app_config)
    config = base.model_copy(
        update={
            "archive_policy": base.archive_policy.model_copy(
                update={"large_video_threshold_bytes": 5}
            )
        }
    )
    remote = ReadyFakeArchive(config.storage.fake_archive_root)
    monkeypatch.setattr("photoarchive.icloud_workflow.ExternalDriveClient", lambda _: remote)
    scan, content = large_video_scan()
    session = FakePhotoLibrarySession(scan, content)
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))
    selected = workflow.select_large_videos("wife", session, scan)

    scope = workflow.build_pipeline_scope(
        "wife",
        selected,
        selection_mode="large_video",
        selection_threshold_bytes=5,
    )
    result = workflow.execute_pipeline(scope, session)

    assert scope.selection_threshold_bytes == 5
    assert [asset.local_identifier for asset in scope.new_scan.assets] == ["video-large"]
    assert result.deleted == 1
    assert session.deleted == ["video-large"]


def test_large_video_batch_archives_all_resources_and_uses_large_video_gate(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = icloud_config(app_config)
    config = base.model_copy(
        update={
            "archive_policy": base.archive_policy.model_copy(
                update={"large_video_threshold_bytes": 5}
            )
        }
    )
    remote = ReadyFakeArchive(config.storage.fake_archive_root)
    monkeypatch.setattr("photoarchive.icloud_workflow.ExternalDriveClient", lambda _: remote)
    scan, content = large_video_scan()
    session = FakePhotoLibrarySession(scan, content)
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))
    selected = workflow.select_large_videos("wife", session, scan)

    archived = workflow.archive_scan(
        "wife",
        session,
        selected,
        selection_mode="large_video",
        selection_threshold_bytes=5,
    )
    assert set(session.downloaded) == {"large-original", "large-adjusted"}
    batch = database.get_icloud_batch(archived.batch_id)
    assert batch["selection_mode"] == "large_video"
    assert batch["selection_threshold_bytes"] == 5

    plan = workflow.prepare_cleanup(archived.batch_id, session)
    assert plan.selection_mode == "large_video"
    assert plan.selection_threshold_bytes == 5
    json_path, _ = generate_report(
        database, archived.job_id, config.storage.reports_root / archived.batch_id
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["icloud_cleanup"]["selection_mode"] == "large_video"
    assert report["icloud_cleanup"]["selection_threshold_bytes"] == 5
    assert workflow.execute_cleanup(plan, session) == {"deleted": 1, "failed": 0}


def test_cleanup_plan_splits_large_legacy_batch_without_weakening_digests(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = icloud_config(app_config)
    config = base.model_copy(
        update={
            "icloud_cleanup": base.icloud_cleanup.model_copy(update={"batch_size": 2})
        }
    )
    remote = ReadyFakeArchive(config.storage.fake_archive_root)
    monkeypatch.setattr("photoarchive.icloud_workflow.ExternalDriveClient", lambda _: remote)
    scan, content = multi_asset_scan(5)
    session = FakePhotoLibrarySession(scan, content)
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))

    archived = workflow.archive_scan(
        "wife", session, scan, candidate_limit=len(scan.assets)
    )
    plan = workflow.prepare_cleanup(archived.batch_id, session)
    chunks = workflow.split_cleanup_plan(plan)

    assert [len(chunk.assets) for chunk in chunks] == [2, 2, 1]
    assert len({chunk.plan_sha256 for chunk in chunks}) == 3
    assert sum(chunk.total_bytes for chunk in chunks) == plan.total_bytes


def test_icloud_archive_requires_verification_before_explicit_cleanup(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = icloud_config(app_config)
    remote = ReadyFakeArchive(config.storage.fake_archive_root)
    monkeypatch.setattr("photoarchive.icloud_workflow.ExternalDriveClient", lambda _: remote)
    scan = library_scan()
    session = FakePhotoLibrarySession(
        scan,
        {"photo-key": b"photo-original", "video-key": b"video-original"},
    )
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))

    _, summary = workflow.scan("wife", session)
    archived = workflow.archive_scan("wife", session, scan)

    assert summary.total_assets == 12
    assert summary.candidate_bytes is None
    assert session.deleted == []
    assert database.get_icloud_batch(archived.batch_id)["state"] == (
        "READY_FOR_ICLOUD_CLEANUP"
    )
    assert not (config.storage.staging_root / archived.job_id).exists()

    plan = workflow.prepare_cleanup(archived.batch_id, session)
    assert len(plan.plan_sha256) == 64
    assert plan.total_bytes == len(b"photo-original") + len(b"video-original")
    assert workflow.execute_cleanup(plan, session) == {"deleted": 1, "failed": 0}
    assert session.deleted == ["asset-local-id"]
    assert database.get_icloud_batch(archived.batch_id)["state"] == "COMPLETED"

    json_path, csv_path = generate_report(
        database, archived.job_id, config.storage.reports_root / archived.batch_id
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["report_schema_version"] == 4
    assert report["icloud_cleanup"]["counts"] == {"DELETED": 1}
    assert report["icloud_cleanup"]["recently_deleted_action_required"] is True
    assert csv_path.name == "icloud_cleanup.csv"


def test_icloud_cleanup_rehashes_each_remote_resource_once_without_stability_wait(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = icloud_config(app_config)
    remote = CountingReadyFakeArchive(config.storage.fake_archive_root)
    monkeypatch.setattr("photoarchive.icloud_workflow.ExternalDriveClient", lambda _: remote)
    scan = library_scan()
    session = FakePhotoLibrarySession(
        scan,
        {"photo-key": b"photo-original", "video-key": b"video-original"},
    )
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    progress: list[tuple[str, dict[str, object]]] = []
    workflow = ICloudWorkflow(
        config,
        database,
        FakePhotoLibraryClient(session),
        progress=lambda event, details: progress.append((event, details)),
    )
    archived = workflow.archive_scan("wife", session, scan)
    remote.stat_calls = 0
    monkeypatch.setattr(
        "photoarchive.pipeline.time.sleep",
        lambda _seconds: pytest.fail("cleanup verification must not wait between observations"),
    )

    plan = workflow.prepare_cleanup(archived.batch_id, session)

    assert len(plan.assets) == 1
    assert remote.stat_calls == 2
    starts = [details for event, details in progress if event == "DELETE_EVIDENCE_ASSET_STARTED"]
    assert starts[-1]["current"] == 1
    assert starts[-1]["total"] == 1


def test_icloud_cleanup_can_verify_and_delete_one_confirmed_chunk_at_a_time(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = icloud_config(app_config)
    config = base.model_copy(
        update={
            "icloud_cleanup": base.icloud_cleanup.model_copy(update={"batch_size": 2})
        }
    )
    remote = CountingReadyFakeArchive(config.storage.fake_archive_root)
    monkeypatch.setattr("photoarchive.icloud_workflow.ExternalDriveClient", lambda _: remote)
    scan, content = multi_asset_scan(5)
    session = FakePhotoLibrarySession(scan, content)
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))
    archived = workflow.archive_scan(
        "wife", session, scan, candidate_limit=len(scan.assets)
    )

    previews = workflow.preview_cleanup_plans("wife")
    assert [len(plan.assets) for plan in previews] == [2, 2, 1]
    first = previews[0]
    prepared = workflow.prepare_cleanup(
        archived.batch_id,
        session,
        local_identifiers=frozenset(asset.local_identifier for asset in first.assets),
    )
    assert prepared.plan_sha256 == first.plan_sha256
    assert workflow.execute_cleanup(prepared, session) == {"deleted": 2, "failed": 0}
    assert len(session.deleted) == 2
    assert len(workflow.preview_cleanup_plans("wife")) == 2


def test_icloud_cleanup_blocks_asset_whose_resource_set_changed(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = icloud_config(app_config)
    remote = ReadyFakeArchive(config.storage.fake_archive_root)
    monkeypatch.setattr("photoarchive.icloud_workflow.ExternalDriveClient", lambda _: remote)
    scan = library_scan()
    session = FakePhotoLibrarySession(
        scan,
        {"photo-key": b"photo-original", "video-key": b"video-original"},
    )
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = ICloudWorkflow(config, database, FakePhotoLibraryClient(session))
    archived = workflow.archive_scan("wife", session, scan)
    original = scan.assets[0]
    session.current[original.local_identifier] = PhotoLibraryAsset(
        original.local_identifier,
        original.creation_at_utc,
        original.media_type,
        original.resources[:1],
    )

    with pytest.raises(PhoneSafetyError, match="revalidation failed"):
        workflow.prepare_cleanup(archived.batch_id, session)

    row = database.list_icloud_batch_assets(archived.batch_id)[0]
    assert row["cleanup_state"] == "REMAINING"
    assert row["error_code"] == "ICLOUD_ASSET_CHANGED"
    assert session.deleted == []
