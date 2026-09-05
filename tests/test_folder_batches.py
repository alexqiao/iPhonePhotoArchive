from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest

from photoarchive.batches import BatchManager
from photoarchive.config import AppConfig
from photoarchive.database import Database
from photoarchive.domain import BatchNotReadyError, DiscoveryRequest, SourceChangedError
from photoarchive.external_drive import ExternalDriveClient
from photoarchive.folder_source import FolderArchiveSource, MediaMetadata
from photoarchive.pipeline import ArchiveRunner, Planner
from photoarchive.reporting import generate_report


def source_for(manager: BatchManager, batch_id: str) -> FolderArchiveSource:
    batch = manager.database.get_batch(batch_id)
    return FolderArchiveSource(
        manager.batch_path(batch_id),
        profile_id=str(batch["profile_id"]),
        batch_id=batch_id,
        config=manager.config.folder_import,
        metadata_reader=lambda path: MediaMetadata(
            "video"
            if path.suffix.casefold() == ".mov"
            else ("sidecar" if path.suffix.casefold() == ".aae" else "photo"),
            None,
            None,
        ),
        sleeper=lambda _: None,
    )


def request() -> DiscoveryRequest:
    from datetime import UTC, datetime

    return DiscoveryRequest(datetime(2000, 1, 1, tzinfo=UTC), frozenset(), 100)


def test_live_photo_batch_archives_under_profile_and_moves_whole_batch(
    app_config: AppConfig, tmp_path: Path
) -> None:
    database = Database(app_config.storage.database_path)
    manager = BatchManager(app_config, database)
    manager.add_profile("wife", "妻子")
    batch_id, pending = manager.prepare("wife", open_image_capture=False)
    (pending / "IMG_0001.HEIC").write_bytes(b"still-original")
    (pending / "IMG_0001.MOV").write_bytes(b"motion-original")
    (pending / "IMG_0001.AAE").write_bytes(b"adjustment")
    source = source_for(manager, batch_id)
    assets = list(source.discover(request()))
    assert len(assets) == 1
    assert assets[0].media_type == "live_photo"
    assert {item.resource_type for item in assets[0].resources} == {
        "photo",
        "video",
        "sidecar",
    }

    summary = Planner(app_config, database, source, target_adapter="external").create_job()
    assert summary.job_id
    database.attach_batch_job(batch_id, summary.job_id)
    app_config.external_drive.volume_path.mkdir()
    target = ExternalDriveClient(app_config.external_drive, require_mount=False)
    counts = ArchiveRunner(app_config, database, source, target).run_job(summary.job_id)
    assert counts == {"SAFE_TO_DELETE": 1}

    completed = manager.finalize(batch_id)
    assert completed.is_dir()
    assert not pending.exists()
    assert manager.finalize(batch_id) == completed
    archived = list((app_config.external_drive.volume_path / "PhotoArchive" / "wife").rglob("*"))
    assert any(path.name == "IMG_0001.HEIC" for path in archived)
    assert any(path.name == "IMG_0001.MOV" for path in archived)

    report_json, report_csv = generate_report(database, summary.job_id, tmp_path / "batch-report")
    report = json.loads(report_json.read_text(encoding="utf-8"))
    assert report["report_schema_version"] == 2
    assert report["profile_id"] == "wife"
    assert report_csv.name == "archived_files.csv"
    with report_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["source_relative_paths"]


def test_profiles_with_same_filename_use_separate_archive_roots(
    app_config: AppConfig,
) -> None:
    database = Database(app_config.storage.database_path)
    manager = BatchManager(app_config, database)
    manager.add_profile("alex", "Alex")
    manager.add_profile("wife", "妻子")
    assets = []
    for profile in ("alex", "wife"):
        batch_id, pending = manager.prepare(profile, open_image_capture=False)
        (pending / "IMG_0001.HEIC").write_bytes(profile.encode())
        assets.append(next(iter(source_for(manager, batch_id).discover(request()))))
    assert assets[0].library_id != assets[1].library_id
    assert assets[0].photos_local_id != assets[1].photos_local_id


def test_live_photo_identifier_groups_different_names_and_sidecar(
    app_config: AppConfig,
) -> None:
    database = Database(app_config.storage.database_path)
    manager = BatchManager(app_config, database)
    manager.add_profile("alex", "Alex")
    batch_id, pending = manager.prepare("alex", open_image_capture=False)
    for name in ("IMG_A.HEIC", "IMG_B.MOV", "IMG_A.AAE"):
        (pending / name).write_bytes(name.encode())

    def metadata(path: Path) -> MediaMetadata:
        if path.suffix.casefold() == ".mov":
            return MediaMetadata("video", None, None, "live-id")
        if path.suffix.casefold() == ".heic":
            return MediaMetadata("photo", None, None, "live-id")
        return MediaMetadata("sidecar", None, None)

    source = FolderArchiveSource(
        pending,
        profile_id="alex",
        batch_id=batch_id,
        config=app_config.folder_import,
        metadata_reader=metadata,
        sleeper=lambda _: None,
    )
    assets = list(source.discover(request()))
    assert len(assets) == 1
    assert assets[0].media_type == "live_photo"
    assert len(assets[0].resources) == 3


def test_profile_id_rejects_path_syntax(app_config: AppConfig) -> None:
    manager = BatchManager(app_config, Database(app_config.storage.database_path))
    with pytest.raises(ValueError):
        manager.add_profile("../wife", "妻子")


def test_changed_source_is_rejected_before_export(app_config: AppConfig) -> None:
    database = Database(app_config.storage.database_path)
    manager = BatchManager(app_config, database)
    manager.add_profile("alex", "Alex")
    batch_id, pending = manager.prepare("alex", open_image_capture=False)
    media = pending / "IMG_0002.HEIC"
    media.write_bytes(b"first")
    source = source_for(manager, batch_id)
    resource = next(iter(source.discover(request()))).resources[0]
    media.write_bytes(b"changed-size")
    with pytest.raises(SourceChangedError):
        source.export(resource, pending.parent / "copy.partial")


def test_batch_with_failed_asset_never_moves(app_config: AppConfig) -> None:
    database = Database(app_config.storage.database_path)
    manager = BatchManager(app_config, database)
    manager.add_profile("alex", "Alex")
    batch_id, pending = manager.prepare("alex", open_image_capture=False)
    (pending / "IMG_0003.HEIC").write_bytes(b"photo")
    source = source_for(manager, batch_id)
    summary = Planner(app_config, database, source, target_adapter="external").create_job()
    assert summary.job_id
    database.attach_batch_job(batch_id, summary.job_id)
    with pytest.raises(BatchNotReadyError):
        manager.finalize(batch_id)
    assert pending.is_dir()


def test_finalize_reconciles_directory_move_after_process_crash(app_config: AppConfig) -> None:
    database = Database(app_config.storage.database_path)
    manager = BatchManager(app_config, database)
    manager.add_profile("alex", "Alex")
    batch_id, pending = manager.prepare("alex", open_image_capture=False)
    (pending / "IMG_0007.HEIC").write_bytes(b"photo")
    source = source_for(manager, batch_id)
    summary = Planner(app_config, database, source, target_adapter="external").create_job()
    assert summary.job_id
    database.attach_batch_job(batch_id, summary.job_id)
    app_config.external_drive.volume_path.mkdir()
    ArchiveRunner(
        app_config,
        database,
        source,
        ExternalDriveClient(app_config.external_drive, require_mount=False),
    ).run_job(summary.job_id)
    batch = database.get_batch(batch_id)
    completed = app_config.folder_import.inbox_root / batch["completed_relative_path"]
    completed.parent.mkdir(parents=True, exist_ok=True)
    database.begin_batch_operation(batch_id, "MOVE_TO_COMPLETED", f"finalize:{batch_id}")
    os.replace(pending, completed)

    assert manager.finalize(batch_id) == completed
    with database.connect() as connection:
        operation = connection.execute(
            "SELECT status FROM batch_operations WHERE batch_id = ?", (batch_id,)
        ).fetchone()
    assert operation["status"] == "SUCCEEDED"


def test_file_added_during_stability_poll_blocks_discovery(app_config: AppConfig) -> None:
    database = Database(app_config.storage.database_path)
    manager = BatchManager(app_config, database)
    manager.add_profile("alex", "Alex")
    batch_id, pending = manager.prepare("alex", open_image_capture=False)
    (pending / "IMG_0004.HEIC").write_bytes(b"first")

    def add_file(_: float) -> None:
        (pending / "IMG_0005.HEIC").write_bytes(b"arrived-late")

    batch = database.get_batch(batch_id)
    source = FolderArchiveSource(
        pending,
        profile_id="alex",
        batch_id=batch_id,
        config=app_config.folder_import,
        metadata_reader=lambda _: MediaMetadata("photo", None, None),
        sleeper=add_file,
    )
    assert batch["state"] == "PREPARED"
    with pytest.raises(SourceChangedError):
        list(source.discover(request()))


def test_orphan_sidecar_blocks_batch(app_config: AppConfig) -> None:
    database = Database(app_config.storage.database_path)
    manager = BatchManager(app_config, database)
    manager.add_profile("alex", "Alex")
    batch_id, pending = manager.prepare("alex", open_image_capture=False)
    (pending / "IMG_0006.AAE").write_bytes(b"adjustment-without-photo")
    with pytest.raises(BatchNotReadyError):
        list(source_for(manager, batch_id).discover(request()))
