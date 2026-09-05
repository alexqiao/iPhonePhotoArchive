from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from photoarchive.config import AppConfig
from photoarchive.database import Database
from photoarchive.domain import (
    DeviceMismatchError,
    PhoneCleanupComplete,
    PhoneDeleteResult,
    PhoneDevice,
    PhoneResource,
    PhoneSafetyError,
    PhoneScan,
)
from photoarchive.fake_archive import FakeArchiveTarget
from photoarchive.phone_workflow import PhoneWorkflow
from photoarchive.reporting import generate_report


class FakePhoneSession:
    def __init__(self, scan: PhoneScan, content: dict[str, bytes]) -> None:
        self.current_scan = scan
        self.content = content
        self.deleted: list[str] = []
        self.downloaded: list[str] = []
        self.closed = False
        self.valid_tokens: set[str] | None = None
        self.delete_result: PhoneDeleteResult | None = None

    def scan(self, cutoff_at_utc: str) -> PhoneScan:
        del cutoff_at_utc
        return self.current_scan

    def download(self, resource: PhoneResource, partial_path: Path) -> None:
        self.downloaded.append(resource.token)
        partial_path.write_bytes(self.content[resource.token])

    def revalidate(self, resources: tuple[PhoneResource, ...]) -> tuple[str, ...]:
        return tuple(
            item.token
            for item in resources
            if self.valid_tokens is None or item.token in self.valid_tokens
        )

    def delete(
        self,
        resources: tuple[PhoneResource, ...],
        *,
        batch_id: str,
        cutoff_at_utc: str,
        plan_sha256: str,
    ) -> PhoneDeleteResult:
        assert batch_id
        assert cutoff_at_utc
        assert len(plan_sha256) == 64
        if self.delete_result is not None:
            return self.delete_result
        self.deleted.extend(item.token for item in resources)
        return PhoneDeleteResult(tuple(self.deleted), ())

    def close(self) -> None:
        self.closed = True


class FakePhoneClient:
    def __init__(self, session: FakePhoneSession) -> None:
        self.session = session

    def open_session(self) -> FakePhoneSession:
        return self.session

    def list_devices(self) -> tuple[PhoneDevice, ...]:
        return (self.session.current_scan.device,)


def phone_config(app_config: AppConfig) -> AppConfig:
    return app_config.model_copy(
        update={"phone_cleanup": app_config.phone_cleanup.model_copy(update={"enabled": True})}
    )


def scan_for(
    *, icloud: bool | None = False, resources: tuple[PhoneResource, ...] | None = None
) -> PhoneScan:
    device = PhoneDevice("device-a", "Test iPhone", "iPhone", False, True, icloud, True)
    default = PhoneResource(
        token="token-photo",
        item_fingerprint="fingerprint-photo",
        ptp_object_handle=12,
        original_name="IMG_0001.JPG",
        size=12,
        creation_at_utc=datetime(2020, 1, 2, tzinfo=UTC),
        modification_at_utc=datetime(2020, 1, 2, tzinfo=UTC),
        uti="public.jpeg",
        asset_key="asset-photo",
        media_type="image",
    )
    selected = resources or (default,)
    return PhoneScan(
        device,
        selected,
        total_media_assets=10,
        total_media_resources=12,
        total_media_bytes=2048,
    )


def test_read_only_scan_accepts_stale_locked_device_flag(app_config: AppConfig) -> None:
    device = PhoneDevice("device-a", "Test iPhone", "iPhone", True, True, True, False)
    scan = PhoneScan(device, scan_for().resources)
    session = FakePhoneSession(scan, {})
    workflow = PhoneWorkflow(
        phone_config(app_config),
        Database(app_config.storage.database_path),
        FakePhoneClient(session),
    )

    _, summary = workflow.scan(session)

    assert summary.device.locked is True
    assert summary.candidate_resources == 1
    assert summary.total_media_assets is None
    with pytest.raises(PhoneSafetyError, match="does not allow per-item deletion"):
        workflow._validate_device(device, deletion=True)

    workflow._validate_device(
        PhoneDevice("device-a", "Test iPhone", "iPhone", True, True, True, True),
        deletion=True,
    )


def test_scan_summary_includes_all_phone_media_counts(app_config: AppConfig) -> None:
    scan = scan_for()
    workflow = PhoneWorkflow(
        phone_config(app_config),
        Database(app_config.storage.database_path),
        FakePhoneClient(FakePhoneSession(scan, {})),
    )

    _, summary = workflow.scan()

    assert summary.total_media_assets == 10
    assert summary.total_media_resources == 12
    assert summary.total_media_bytes == 2048
    assert summary.candidate_assets == 1


def test_verified_phone_batch_deletes_only_after_plan_confirmation(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = phone_config(app_config)
    config.external_drive.volume_path.mkdir()
    remote = FakeArchiveTarget(config.external_drive.volume_path / "archive")
    monkeypatch.setattr("photoarchive.phone_workflow.ExternalDriveClient", lambda _: remote)
    scan = scan_for()
    session = FakePhoneSession(scan, {"token-photo": b"hello world!"})
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    progress: list[tuple[str, dict[str, object]]] = []
    workflow = PhoneWorkflow(
        config,
        database,
        FakePhoneClient(session),
        progress=lambda event, details: progress.append((event, details)),
    )
    workflow.bind("wife", scan.device)

    archived = workflow.archive_scan("wife", session, scan)
    assert database.get_phone_batch(archived.batch_id)["state"] == "READY_FOR_PHONE_CLEANUP"
    assert session.deleted == []

    plan = workflow.prepare_cleanup(archived.batch_id, session)
    assert plan.profile_id == "wife"
    assert len(plan.plan_sha256) == 64
    result = workflow.execute_cleanup(plan, session)

    assert result == {"deleted": 1, "failed": 0}
    assert session.deleted == ["token-photo"]
    assert database.get_phone_batch(archived.batch_id)["state"] == "COMPLETED"
    events = {event for event, _ in progress}
    assert "PHONE_IMPORT_RESOURCE_COMPLETED" in events
    assert "ARCHIVE_VERIFY_POLL" in events
    assert "PHONE_CLEANUP_COMPLETED" in events
    json_path, csv_path = generate_report(
        database, archived.job_id, config.storage.reports_root / archived.batch_id
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["report_schema_version"] == 3
    assert report["phone_cleanup"]["counts"] == {"DELETED": 1}
    assert csv_path.name == "phone_cleanup.csv"


def test_second_batch_reuses_verified_archive_without_downloading(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = phone_config(app_config)
    config.external_drive.volume_path.mkdir()
    monkeypatch.setattr(
        "photoarchive.phone_workflow.ExternalDriveClient",
        lambda _: FakeArchiveTarget(config.external_drive.volume_path / "archive"),
    )
    scan = scan_for()
    session = FakePhoneSession(scan, {"token-photo": b"hello world!"})
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    progress: list[tuple[str, dict[str, object]]] = []
    workflow = PhoneWorkflow(
        config,
        database,
        FakePhoneClient(session),
        progress=lambda event, details: progress.append((event, details)),
    )
    workflow.bind("wife", scan.device)

    first = workflow.archive_scan("wife", session, scan)
    _, second_summary = workflow.scan(session)
    second = workflow.archive_scan("wife", session, scan)

    assert first.batch_id != second.batch_id
    assert second_summary.new_archive_assets == 0
    assert second_summary.new_archive_resources == 0
    assert second_summary.reusable_assets == 1
    assert session.downloaded == ["token-photo"]
    assert "PHONE_IMPORT_RESOURCE_REUSED" in {event for event, _ in progress}
    first_item = database.list_phone_items(first.batch_id)[0]
    second_item = database.list_phone_items(second.batch_id)[0]
    assert second_item["resource_id"] == first_item["resource_id"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0] == 1


def test_delete_failure_detail_is_logged_and_reported(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = phone_config(app_config)
    config.external_drive.volume_path.mkdir()
    monkeypatch.setattr(
        "photoarchive.phone_workflow.ExternalDriveClient",
        lambda _: FakeArchiveTarget(config.external_drive.volume_path / "archive"),
    )
    scan = scan_for()
    session = FakePhoneSession(scan, {"token-photo": b"hello world!"})
    session.delete_result = PhoneDeleteResult(
        (),
        ("token-photo",),
        {"token-photo": "ICDeleteErrorReadOnly"},
        "com.apple.ImageCaptureCore code -9958: Delete failed",
    )
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    progress: list[tuple[str, dict[str, object]]] = []
    workflow = PhoneWorkflow(
        config,
        database,
        FakePhoneClient(session),
        progress=lambda event, details: progress.append((event, details)),
    )
    workflow.bind("wife", scan.device)
    archived = workflow.archive_scan("wife", session, scan)
    plan = workflow.prepare_cleanup(archived.batch_id, session)

    assert workflow.execute_cleanup(plan, session) == {"deleted": 0, "failed": 1}

    item = database.list_phone_items(archived.batch_id)[0]
    assert item["delete_error_detail"] == "ICDeleteErrorReadOnly"
    completed = [details for event, details in progress if event == "PHONE_DELETE_CHUNK_COMPLETED"]
    assert completed[-1]["reasons"] == ["ICDeleteErrorReadOnly"]
    report_json, _ = generate_report(
        database, archived.job_id, config.storage.reports_root / archived.batch_id
    )
    report = json.loads(report_json.read_text(encoding="utf-8"))
    assert report["phone_cleanup"]["assets"][0]["remaining_reason"] == (
        "ICDeleteErrorReadOnly"
    )


def test_resume_repairs_legacy_singleton_relationship_warning(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = phone_config(app_config)
    config.external_drive.volume_path.mkdir()
    monkeypatch.setattr(
        "photoarchive.phone_workflow.ExternalDriveClient",
        lambda _: FakeArchiveTarget(config.external_drive.volume_path / "archive"),
    )
    current = scan_for().resources[0]
    legacy = replace(current, warning="INCOMPLETE_RELATED_ASSET")
    session = FakePhoneSession(scan_for(resources=(legacy,)), {"token-photo": b"hello world!"})
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("wife", "妻子")
    workflow = PhoneWorkflow(config, database, FakePhoneClient(session))
    workflow.bind("wife", session.current_scan.device)

    with pytest.raises(PhoneSafetyError, match="no phone resources"):
        workflow.archive_scan("wife", session, session.current_scan)
    with database.connect() as connection:
        row = connection.execute(
            "SELECT batch_id FROM phone_batches ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    batch_id = str(row["batch_id"])

    session.current_scan = scan_for()
    archived = workflow.resume_archive(batch_id, session)

    assert archived.batch_id == batch_id
    items = database.list_phone_items(batch_id)
    assert items[0]["warning"] is None
    assert items[0]["status"] == "VERIFIED"


def test_profile_binding_rejects_another_phone(app_config: AppConfig) -> None:
    config = phone_config(app_config)
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("alex", "Alex")
    database.create_profile("wife", "妻子")
    first = scan_for().device
    workflow = PhoneWorkflow(
        config,
        database,
        FakePhoneClient(FakePhoneSession(scan_for(), {"token-photo": b"hello world!"})),
    )
    workflow.bind("alex", first)

    with pytest.raises(DeviceMismatchError):
        workflow.archive_scan("wife", FakePhoneSession(scan_for(), {}), scan_for())


def test_unknown_icloud_status_blocks_cleanup(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = phone_config(app_config)
    config.external_drive.volume_path.mkdir()
    monkeypatch.setattr(
        "photoarchive.phone_workflow.ExternalDriveClient",
        lambda _: FakeArchiveTarget(config.external_drive.volume_path / "archive"),
    )
    scan = scan_for(icloud=None)
    session = FakePhoneSession(scan, {"token-photo": b"hello world!"})
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("alex", "Alex")
    workflow = PhoneWorkflow(config, database, FakePhoneClient(session))
    workflow.bind("alex", scan.device)
    archived = workflow.archive_scan("alex", session, scan)

    with pytest.raises(PhoneSafetyError, match="iCloud Photos status is unknown"):
        workflow.prepare_cleanup(archived.batch_id, session)


def test_incomplete_logical_asset_never_enters_delete_plan(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = phone_config(app_config)
    config.external_drive.volume_path.mkdir()
    monkeypatch.setattr(
        "photoarchive.phone_workflow.ExternalDriveClient",
        lambda _: FakeArchiveTarget(config.external_drive.volume_path / "archive"),
    )
    image = scan_for().resources[0]
    missing_video = PhoneResource(
        token="token-video",
        item_fingerprint="fingerprint-video",
        ptp_object_handle=13,
        original_name="IMG_0001.MOV",
        size=20,
        creation_at_utc=image.creation_at_utc,
        modification_at_utc=image.modification_at_utc,
        uti="com.apple.quicktime-movie",
        asset_key=image.asset_key,
        media_type="live_photo",
        downloadable=False,
        warning="ORIGINAL_NOT_AVAILABLE",
    )
    scan = scan_for(resources=(image, missing_video))
    session = FakePhoneSession(scan, {"token-photo": b"hello world!"})
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("alex", "Alex")
    workflow = PhoneWorkflow(config, database, FakePhoneClient(session))
    workflow.bind("alex", scan.device)
    archived = workflow.archive_scan("alex", session, scan)

    with pytest.raises(PhoneSafetyError, match="no fully verified"):
        workflow.prepare_cleanup(archived.batch_id, session)
    assert session.deleted == []


def test_resume_reconciles_item_deleted_after_committed_intent(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = phone_config(app_config)
    config.external_drive.volume_path.mkdir()
    monkeypatch.setattr(
        "photoarchive.phone_workflow.ExternalDriveClient",
        lambda _: FakeArchiveTarget(config.external_drive.volume_path / "archive"),
    )
    scan = scan_for()
    session = FakePhoneSession(scan, {"token-photo": b"hello world!"})
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("alex", "Alex")
    workflow = PhoneWorkflow(config, database, FakePhoneClient(session))
    binding = workflow.bind("alex", scan.device)
    assert binding["device_key"] == "device-a"
    archived = workflow.archive_scan("alex", session, scan)
    plan = workflow.prepare_cleanup(archived.batch_id, session)
    database.set_phone_batch_state(
        archived.batch_id,
        "CLEANING_PHONE",
        plan_sha256=plan.plan_sha256,
        confirmed=True,
    )
    for asset_id in plan.asset_ids:
        key = f"phone-delete:{archived.batch_id}:{asset_id}:{plan.plan_sha256}"
        database.begin_batch_operation(archived.batch_id, "DELETE_PHONE_ASSET", key)
        database.set_phone_asset_state(asset_id, "DELETE_INTENT")
    session.current_scan = PhoneScan(scan.device, ())

    with pytest.raises(PhoneCleanupComplete):
        workflow.prepare_cleanup(archived.batch_id, session)

    assert database.get_phone_batch(archived.batch_id)["state"] == "COMPLETED"
    assert database.list_phone_items(archived.batch_id)[0]["status"] == "DELETED"


def test_partial_live_photo_revalidation_deletes_nothing(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = phone_config(app_config)
    config.external_drive.volume_path.mkdir()
    monkeypatch.setattr(
        "photoarchive.phone_workflow.ExternalDriveClient",
        lambda _: FakeArchiveTarget(config.external_drive.volume_path / "archive"),
    )
    image = scan_for().resources[0]
    video = PhoneResource(
        token="token-video",
        item_fingerprint="fingerprint-video",
        ptp_object_handle=13,
        original_name="IMG_0001.MOV",
        size=20,
        creation_at_utc=image.creation_at_utc,
        modification_at_utc=image.modification_at_utc,
        uti="com.apple.quicktime-movie",
        asset_key=image.asset_key,
        media_type="live_photo",
    )
    scan = scan_for(resources=(image, video))
    session = FakePhoneSession(
        scan,
        {"token-photo": b"hello world!", "token-video": b"x" * 20},
    )
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("alex", "Alex")
    workflow = PhoneWorkflow(config, database, FakePhoneClient(session))
    workflow.bind("alex", scan.device)
    archived = workflow.archive_scan("alex", session, scan)
    session.valid_tokens = {"token-photo"}

    with pytest.raises(PhoneSafetyError, match="revalidation failed"):
        workflow.prepare_cleanup(archived.batch_id, session)

    assert session.deleted == []


def test_newer_phone_item_is_rejected_before_download(app_config: AppConfig) -> None:
    config = phone_config(app_config)
    newer = PhoneResource(
        token="newer",
        item_fingerprint="newer-fingerprint",
        ptp_object_handle=99,
        original_name="NEW.JPG",
        size=3,
        creation_at_utc=datetime(2025, 1, 1, tzinfo=UTC),
        modification_at_utc=datetime(2025, 1, 1, tzinfo=UTC),
        uti="public.jpeg",
        asset_key="newer-asset",
        media_type="image",
    )
    scan = scan_for(resources=(newer,))
    session = FakePhoneSession(scan, {"newer": b"new"})
    database = Database(config.storage.database_path)
    database.migrate()
    database.create_profile("alex", "Alex")
    workflow = PhoneWorkflow(config, database, FakePhoneClient(session))
    workflow.bind("alex", scan.device)

    with pytest.raises(PhoneSafetyError, match="safe pre-cutoff date"):
        workflow.archive_scan("alex", session, scan)

    assert session.deleted == []
