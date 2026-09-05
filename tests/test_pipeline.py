from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from photoarchive.config import AppConfig
from photoarchive.database import Database
from photoarchive.domain import (
    AssetState,
    ConfigDriftError,
    ConflictError,
    RemoteObject,
    UploadRequest,
)
from photoarchive.fake_archive import FakeArchiveTarget
from photoarchive.fixture import FixturePhotosClient
from photoarchive.pipeline import ArchiveRunner, InjectedCrash, Planner
from photoarchive.reporting import generate_report


def build(app_config: AppConfig, demo_fixture: Path):
    database = Database(app_config.storage.database_path)
    source = FixturePhotosClient(demo_fixture)
    planner = Planner(app_config, database, source)
    return database, source, planner


def test_vertical_slice_and_report_gate(
    app_config: AppConfig, demo_fixture: Path, tmp_path: Path
) -> None:
    database, source, planner = build(app_config, demo_fixture)
    preview = planner.preview()
    assert preview.candidate_assets == 6
    assert preview.required_resources == 9
    summary = planner.create_job()
    assert summary.job_id is not None
    runner = ArchiveRunner(
        app_config,
        database,
        source,
        FakeArchiveTarget(app_config.storage.fake_archive_root),
    )
    counts = runner.run_job(summary.job_id)
    assert counts == {"FAILED": 1, "SAFE_TO_DELETE": 5}
    audit_path = app_config.storage.database_path.parent / "logs" / "photoarchive.jsonl"
    audit_record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert set(audit_record) == {
        "asset_key",
        "duration_ms",
        "event_code",
        "job_id",
        "retry",
        "run_id",
    }

    report_json, report_csv = generate_report(database, summary.job_id, tmp_path / "report")
    report = json.loads(report_json.read_text(encoding="utf-8"))
    assert len(report["candidates"]) == 5
    assert report["states"]["SAFE_TO_DELETE"]["asset_count"] == 5
    assert report["states"]["SAFE_TO_DELETE"]["bytes"] > 0
    assert report["verification_evidence"]
    assert {item["error_code"] for item in report["failures"]} == {"INTEGRITY_ERROR"}
    with report_csv.open(newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 5
    assert report_json.stat().st_mode & 0o777 == 0o600
    assert report_csv.stat().st_mode & 0o777 == 0o600

    with database.connect() as connection:
        incomplete = connection.execute(
            "SELECT state FROM assets WHERE photos_local_id = 'live-incomplete'"
        ).fetchone()
        operation_counts = connection.execute(
            "SELECT status, COUNT(*) AS count FROM operations GROUP BY status"
        ).fetchall()
    assert incomplete["state"] == "FAILED"
    assert {row["status"] for row in operation_counts} == {"SUCCEEDED"}


def test_second_run_is_idempotent(app_config: AppConfig, demo_fixture: Path) -> None:
    database, source, planner = build(app_config, demo_fixture)
    job_id = planner.create_job().job_id
    assert job_id is not None
    remote = FakeArchiveTarget(app_config.storage.fake_archive_root)
    ArchiveRunner(app_config, database, source, remote).run_job(job_id)
    with database.connect() as connection:
        before = {
            "operations": connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0],
            "files": connection.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0],
            "transitions": connection.execute("SELECT COUNT(*) FROM state_transitions").fetchone()[
                0
            ],
        }
    ArchiveRunner(app_config, database, source, remote).run_job(job_id)
    with database.connect() as connection:
        after = {
            "operations": connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0],
            "files": connection.execute("SELECT COUNT(*) FROM archive_files").fetchone()[0],
            "transitions": connection.execute("SELECT COUNT(*) FROM state_transitions").fetchone()[
                0
            ],
        }
    assert after == before


@pytest.mark.parametrize(
    "fail_state",
    [
        AssetState.EXPORTED,
        AssetState.UPLOADED,
        AssetState.VERIFIED,
        AssetState.SAFE_TO_DELETE,
    ],
)
def test_resume_after_each_committed_state(
    app_config: AppConfig,
    demo_fixture: Path,
    fail_state: AssetState,
) -> None:
    database, source, planner = build(app_config, demo_fixture)
    job_id = planner.create_job().job_id
    assert job_id is not None
    remote = FakeArchiveTarget(app_config.storage.fake_archive_root)
    with pytest.raises(InjectedCrash):
        ArchiveRunner(
            app_config,
            database,
            source,
            remote,
            fail_after_state=fail_state,
        ).run_job(job_id)
    ArchiveRunner(app_config, database, source, remote).run_job(job_id, resume=True)
    counts = database.state_counts(job_id)
    assert counts["SAFE_TO_DELETE"] == 5
    assert counts["FAILED"] == 1


def test_tampered_remote_disappears_from_new_report(
    app_config: AppConfig, demo_fixture: Path, tmp_path: Path
) -> None:
    database, source, planner = build(app_config, demo_fixture)
    job_id = planner.create_job().job_id
    assert job_id is not None
    remote = FakeArchiveTarget(app_config.storage.fake_archive_root)
    runner = ArchiveRunner(app_config, database, source, remote)
    runner.run_job(job_id)
    with database.connect() as connection:
        archived = connection.execute(
            """
            SELECT archive_files.remote_path
            FROM archive_files
            JOIN asset_resources ON asset_resources.id = archive_files.resource_id
            JOIN assets ON assets.id = asset_resources.asset_id
            WHERE assets.photos_local_id = 'basic-image'
            LIMIT 1
            """
        ).fetchone()
    remote_file = app_config.storage.fake_archive_root / archived["remote_path"]
    remote_file.write_bytes(b"tampered")
    ArchiveRunner(app_config, database, source, remote).verify_job(job_id)
    _, report_csv = generate_report(database, job_id, tmp_path / "tampered-report")
    with report_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    assert "basic-image" not in {row["photos_local_id"] for row in rows}
    assert database.review_count(job_id) == 1


class FailFirstUpload:
    def __init__(self, delegate: FakeArchiveTarget) -> None:
        self.delegate = delegate
        self.failed = False

    def put(self, request: UploadRequest) -> RemoteObject:
        if not self.failed:
            self.failed = True
            raise ConflictError("injected upload conflict")
        return self.delegate.put(request)

    def stat(self, remote_path: str) -> RemoteObject:
        return self.delegate.stat(remote_path)


def test_failed_upload_keeps_intent_and_resume_reconciles(
    app_config: AppConfig, demo_fixture: Path
) -> None:
    database, source, planner = build(app_config, demo_fixture)
    job_id = planner.create_job().job_id
    assert job_id is not None
    remote = FakeArchiveTarget(app_config.storage.fake_archive_root)
    ArchiveRunner(app_config, database, source, FailFirstUpload(remote)).run_job(job_id)
    with database.connect() as connection:
        intent = connection.execute(
            "SELECT status FROM operations WHERE kind = 'UPLOAD' ORDER BY created_at LIMIT 1"
        ).fetchone()
    assert intent["status"] == "INTENT"

    counts = ArchiveRunner(app_config, database, source, remote).run_job(job_id, resume=True)
    assert counts == {"FAILED": 1, "SAFE_TO_DELETE": 5}


class UnstableFirstRemote:
    def __init__(self, delegate: FakeArchiveTarget) -> None:
        self.delegate = delegate
        self.target: str | None = None
        self.target_calls = 0

    def put(self, request: UploadRequest) -> RemoteObject:
        return self.delegate.put(request)

    def stat(self, remote_path: str) -> RemoteObject:
        remote = self.delegate.stat(remote_path)
        if self.target is None:
            self.target = remote_path
        if remote_path != self.target:
            return remote
        self.target_calls += 1
        return RemoteObject(
            remote.drive_item_id,
            remote.remote_path,
            remote.size,
            f"{remote.etag}-{self.target_calls}",
            remote.quickxor,
        )


def test_stability_gate_rejects_changing_remote_evidence(
    app_config: AppConfig, demo_fixture: Path
) -> None:
    database, source, planner = build(app_config, demo_fixture)
    job_id = planner.create_job().job_id
    assert job_id is not None
    remote = UnstableFirstRemote(FakeArchiveTarget(app_config.storage.fake_archive_root))
    counts = ArchiveRunner(app_config, database, source, remote).run_job(job_id)
    assert counts == {"FAILED": 2, "SAFE_TO_DELETE": 4}


class TimeoutFirstStat:
    def __init__(self, delegate: FakeArchiveTarget) -> None:
        self.delegate = delegate
        self.timed_out = False

    def put(self, request: UploadRequest) -> RemoteObject:
        return self.delegate.put(request)

    def stat(self, remote_path: str) -> RemoteObject:
        if not self.timed_out:
            self.timed_out = True
            raise TimeoutError("injected timeout")
        return self.delegate.stat(remote_path)


def test_transient_io_uses_configured_retry(app_config: AppConfig, demo_fixture: Path) -> None:
    database, source, planner = build(app_config, demo_fixture)
    job_id = planner.create_job().job_id
    assert job_id is not None
    delays: list[float] = []
    remote = TimeoutFirstStat(FakeArchiveTarget(app_config.storage.fake_archive_root))
    counts = ArchiveRunner(
        app_config,
        database,
        source,
        remote,
        sleeper=delays.append,
    ).run_job(job_id)
    assert counts == {"FAILED": 1, "SAFE_TO_DELETE": 5}
    with database.connect() as connection:
        retry = connection.execute(
            "SELECT retry FROM run_events WHERE event_code = 'EXTERNAL_IO_RETRY'"
        ).fetchone()
    assert retry["retry"] == 1
    assert delays


def test_configuration_drift_stops_run(
    app_config: AppConfig, demo_fixture: Path, tmp_path: Path
) -> None:
    database, source, planner = build(app_config, demo_fixture)
    job_id = planner.create_job().job_id
    assert job_id is not None
    changed = app_config.model_copy(
        update={"archive_policy": app_config.archive_policy.model_copy(update={"batch_size": 99})}
    )
    with pytest.raises(ConfigDriftError):
        ArchiveRunner(
            changed,
            database,
            source,
            FakeArchiveTarget(tmp_path / "changed-remote"),
        ).run_job(job_id)


def test_database_enables_wal_and_foreign_keys(app_config: AppConfig) -> None:
    database = Database(app_config.storage.database_path)
    database.migrate()
    with database.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO job_assets(job_id, asset_id, action) "
                "VALUES ('none', 'none', 'ARCHIVE')"
            )
