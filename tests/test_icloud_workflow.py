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
)
from photoarchive.fake_archive import FakeArchiveTarget
from photoarchive.icloud_workflow import ICloudWorkflow
from photoarchive.reporting import generate_report


class FakePhotoLibrarySession:
    def __init__(self, scan: PhotoLibraryScan, content: dict[str, bytes]) -> None:
        self.current = {asset.local_identifier: asset for asset in scan.assets}
        self.scan_result = scan
        self.content = content
        self.downloaded: list[str] = []
        self.deleted: list[str] = []
        self.closed = False

    def scan(self, cutoff_at_utc: str, media_types: frozenset[str]) -> PhotoLibraryScan:
        assert cutoff_at_utc
        assert media_types
        return self.scan_result

    def download(
        self, asset: PhotoLibraryAsset, resource_key: str, partial_path: Path
    ) -> int:
        assert asset.local_identifier in self.current
        self.downloaded.append(resource_key)
        payload = self.content[resource_key]
        partial_path.write_bytes(payload)
        return len(payload)

    def revalidate(
        self,
        assets: tuple[PhotoLibraryAsset, ...],
        *,
        cutoff_at_utc: str,
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
                current.creation_at_utc >= cutoff
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
    ) -> PhotoLibraryDeleteResult:
        assert batch_id.startswith("icloud-")
        assert cutoff_at_utc
        assert len(plan_sha256) == 64
        identifiers = tuple(asset.local_identifier for asset in assets)
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
