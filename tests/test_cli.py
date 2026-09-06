from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from typer.testing import CliRunner

import photoarchive.cli as cli_module
from photoarchive.cli import app
from photoarchive.config import AppConfig
from photoarchive.domain import PhotoLibraryAsset, PhotoLibraryScan
from photoarchive.external_drive import ExternalDriveClient
from photoarchive.icloud_workflow import ICloudPipelineResult, ICloudPipelineScope


def test_phone_progress_is_written_to_stderr(capsys) -> None:
    cli_module._phone_progress(
        "PHONE_IMPORT_RESOURCE_STARTED",
        {"current": 2, "total": 5, "asset_key": "1234abcd", "bytes": 2048},
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[导入 2/5]" in captured.err
    assert "资产 1234abcd" in captured.err
    assert "2.0 KiB" in captured.err

    cli_module._phone_progress(
        "PHONE_SCAN_COMPLETED",
        {
            "assets": 4,
            "resources": 5,
            "bytes": 2 * 1024**3,
            "total_capacity_bytes": 256 * 1024**3,
            "used_capacity_bytes": 200 * 1024**3,
            "available_capacity_bytes": 56 * 1024**3,
            "total_media_assets": 1200,
            "total_media_resources": 1250,
            "total_media_bytes": 80 * 1024**3,
            "new_archive_assets": 3,
            "new_archive_resources": 4,
            "new_archive_bytes": 1536 * 1024**2,
            "reusable_assets": 1,
            "reusable_resources": 1,
        },
    )
    scan_log = capsys.readouterr().err
    assert "手机照片/视频总量 1200 项 (1250 个文件, 80.0 GiB)" in scan_log
    assert "符合两年条件 4 项 (5 个文件, 2.0 GiB)" in scan_log
    assert "本次新增归档 3 项 (4 个文件, 1.5 GiB)" in scan_log
    assert "已归档待手机清理 1 项 (1 个文件)" in scan_log
    assert "手机总容量 256.0 GiB" in scan_log

    cli_module._phone_progress(
        "PHONE_CLEANUP_READY",
        {"assets": 41, "resources": 42, "bytes": 92_069_457},
    )
    cleanup_log = capsys.readouterr().err
    assert "删除计划就绪: 41 个资产, 42 个文件" in cleanup_log

    cli_module._phone_progress(
        "PHONE_DELETE_CHUNK_COMPLETED",
        {
            "current": 1,
            "total": 1,
            "deleted": 0,
            "failed": 42,
            "method": "requestDeleteFiles",
        },
    )
    cleanup_log = capsys.readouterr().err
    assert "删除 0 个文件, 保留 42 个文件" in cleanup_log


def test_archive_progress_shows_queue_position(capsys) -> None:
    cli_module._phone_progress(
        "ARCHIVE_VERIFY_POLL",
        {
            "current": 1,
            "total": 2,
            "archive_current": 29,
            "archive_total": 42,
            "asset_key": "ba9e86bc",
            "wait_seconds": 1,
        },
    )

    log = capsys.readouterr().err
    assert "[归档 29/42] 稳定性验证 1/2" in log
    assert "资产 ba9e86bc" not in log


def test_cli_registers_and_runs_dry_plan(tmp_path: Path, demo_fixture: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "timezone": "UTC",
                "archive_policy": {"cutoff_date": "2024-01-01"},
                "storage": {
                    "database_path": str(tmp_path / "db.sqlite3"),
                    "staging_root": str(tmp_path / "staging"),
                    "fake_archive_root": str(tmp_path / "remote"),
                    "reports_root": str(tmp_path / "reports"),
                },
                "verification": {
                    "stable_poll_count": 2,
                    "stable_poll_interval_sec": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "--cutoff-date",
            "2024-01-01",
            "plan",
            "--fixture",
            str(demo_fixture),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"candidate_assets": 6' in result.output
    assert not (tmp_path / "db.sqlite3").exists()


def test_sync_all_cli_confirms_the_frozen_scope_once(
    app_config: AppConfig, monkeypatch
) -> None:
    config = app_config.model_copy(
        update={
            "icloud_cleanup": app_config.icloud_cleanup.model_copy(update={"enabled": True})
        }
    )
    asset = PhotoLibraryAsset(
        "asset-local-id",
        datetime(2020, 1, 1, tzinfo=UTC),
        "image",
        (),
    )
    scan = PhotoLibraryScan("authorized", (asset,), 1, 0)
    scope = ICloudPipelineScope(
        profile_id="wife",
        selection_mode="age_cutoff",
        cutoff_at_utc="2024-01-01T00:00:00Z",
        selection_threshold_bytes=None,
        batch_size=1000,
        cleanup_chunks=(),
        resume_batches=(),
        new_scan=scan,
        authorization_sha256="a" * 64,
    )
    calls = {"confirm": 0, "execute": 0}

    class DummyDrive:
        def assert_ready(self) -> None:
            return None

    class DummyDatabase:
        def migrate(self) -> None:
            return None

    class DummySession:
        def close(self) -> None:
            return None

    class DummyClient:
        def open_session(self) -> DummySession:
            return DummySession()

    class DummyWorkflow:
        client = DummyClient()

        def scan(self, profile, session):
            del profile, session
            return scan, object()

        def build_pipeline_scope(self, profile, selected, **kwargs):
            del profile, selected, kwargs
            return scope

        def execute_pipeline(self, selected_scope, session, *, batch_completed):
            del selected_scope, session, batch_completed
            calls["execute"] += 1
            return ICloudPipelineResult((), 0, 1, 1, 0)

    def confirm_once(prompt: str) -> bool:
        assert "Authorization SHA-256" in prompt
        calls["confirm"] += 1
        return True

    monkeypatch.setattr(cli_module, "_config", lambda _ctx: config)
    monkeypatch.setattr(cli_module, "_database", lambda _config: DummyDatabase())
    monkeypatch.setattr(cli_module, "ExternalDriveClient", lambda _config: DummyDrive())
    monkeypatch.setattr(
        cli_module, "_icloud_workflow", lambda _config, _database: DummyWorkflow()
    )
    monkeypatch.setattr(cli_module.typer, "confirm", confirm_once)

    result = CliRunner().invoke(
        app,
        ["icloud", "sync-all", "--profile", "wife"],
    )

    assert result.exit_code == 0, result.output
    assert calls == {"confirm": 1, "execute": 1}
    payload = json.loads(result.output)
    assert payload["status"] == "completed"
    assert payload["authorization_sha256"] == "a" * 64

    monkeypatch.setattr(cli_module.typer, "confirm", lambda _prompt: False)
    cancelled = CliRunner().invoke(
        app,
        ["icloud", "sync-all", "--profile", "wife"],
    )
    assert cancelled.exit_code == 0, cancelled.output
    assert calls["execute"] == 1
    assert json.loads(cancelled.output)["status"] == "cancelled"


def folder_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "folder.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "timezone": "UTC",
                "storage": {
                    "database_path": str(tmp_path / "folder.sqlite3"),
                    "staging_root": str(tmp_path / "staging"),
                    "fake_archive_root": str(tmp_path / "fake"),
                    "reports_root": str(tmp_path / "reports"),
                },
                "folder_import": {
                    "inbox_root": str(tmp_path / "inbox"),
                    "stable_poll_count": 2,
                    "stable_poll_interval_sec": 0,
                    "open_image_capture": False,
                },
                "media_helper": {"helper_path": str(tmp_path / "missing-helper")},
                "external_drive": {
                    "volume_path": str(tmp_path / "external"),
                    "archive_directory": "PhotoArchive",
                    "expected_volume_name": None,
                    "expected_volume_uuid": None,
                    "minimum_free_bytes": 0,
                },
                "verification": {
                    "stable_poll_count": 2,
                    "stable_poll_interval_sec": 0,
                },
                "retry": {"max_attempts": 1, "base_delay_sec": 0, "max_delay_sec": 0},
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_guided_profile_and_batch_commands_complete_archive(tmp_path: Path, monkeypatch) -> None:
    config_path = folder_config(tmp_path)
    runner = CliRunner()
    add = runner.invoke(
        app,
        ["--config", str(config_path), "profile", "add", "--id", "wife", "--name", "妻子"],
    )
    assert add.exit_code == 0, add.output
    prepare = runner.invoke(
        app,
        ["--config", str(config_path), "batch", "prepare", "--profile", "wife", "--no-open"],
    )
    assert prepare.exit_code == 0, prepare.output
    prepared = json.loads(prepare.output)
    batch_id = prepared["batch_id"]
    Path(prepared["destination"], "IMG_1000.HEIC").write_bytes(b"iphone-original")
    (tmp_path / "external").mkdir()
    monkeypatch.setattr(
        cli_module,
        "ExternalDriveClient",
        lambda config: ExternalDriveClient(config, require_mount=False),
    )
    archived = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "batch",
            "archive",
            "--batch-id",
            batch_id,
            "--yes",
        ],
    )
    assert archived.exit_code == 0, archived.output
    result = json.loads(archived.output)
    assert result["status"] == "completed"
    assert Path(result["completed"]).is_dir()
    assert Path(result["report_csv"]).name == "archived_files.csv"
