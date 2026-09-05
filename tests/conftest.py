from __future__ import annotations

from pathlib import Path

import pytest

from photoarchive.config import AppConfig


@pytest.fixture
def demo_fixture() -> Path:
    return Path("fixtures/demo/library.jsonl").resolve(strict=True)


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "version": 1,
            "timezone": "Asia/Shanghai",
            "archive_policy": {
                "archive_after_years": 2,
                "cutoff_date": "2024-01-01",
                "media_types": ["image", "video", "live_photo"],
                "batch_size": 2,
            },
            "storage": {
                "database_path": tmp_path / "photoarchive.sqlite3",
                "staging_root": tmp_path / "staging",
                "fake_archive_root": tmp_path / "remote",
                "reports_root": tmp_path / "reports",
            },
            "verification": {
                "stable_poll_count": 2,
                "stable_poll_interval_sec": 0,
            },
            "folder_import": {
                "inbox_root": tmp_path / "inbox",
                "stable_poll_count": 2,
                "stable_poll_interval_sec": 0,
                "open_image_capture": False,
            },
            "external_drive": {
                "volume_path": tmp_path / "external",
                "archive_directory": "PhotoArchive",
                "expected_volume_name": None,
                "expected_volume_uuid": None,
                "minimum_free_bytes": 0,
            },
            "retry": {"max_attempts": 3, "base_delay_sec": 0, "max_delay_sec": 0},
            "safety": {
                "automatic_photos_delete": False,
                "automatic_source_delete": False,
                "allow_remote_overwrite": False,
                "source_finalize_mode": "move_to_completed",
            },
        }
    )
