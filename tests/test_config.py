from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from photoarchive.config import AppConfig, load_config


def test_explicit_cutoff_wins(app_config: AppConfig) -> None:
    cutoff = app_config.cutoff_at_utc(datetime(2030, 1, 1, tzinfo=UTC))
    assert cutoff.isoformat() == "2023-12-31T16:00:00+00:00"


def test_calendar_year_handles_leap_day(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "timezone": "UTC",
            "archive_policy": {"archive_after_years": 1},
            "storage": {
                "database_path": tmp_path / "db.sqlite3",
                "staging_root": tmp_path / "stage",
                "fake_archive_root": tmp_path / "remote",
                "reports_root": tmp_path / "reports",
            },
        }
    )
    cutoff = config.cutoff_at_utc(datetime(2024, 2, 29, 12, tzinfo=UTC))
    assert cutoff.isoformat() == "2023-02-28T00:00:00+00:00"


def test_precedence_cli_then_environment_then_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"timezone": "Asia/Shanghai"}), encoding="utf-8")
    monkeypatch.setenv("PHOTOARCHIVE__TIMEZONE", "UTC")
    assert load_config(config_path).timezone == "UTC"
    assert load_config(config_path, {"timezone": "Europe/London"}).timezone == "Europe/London"


@pytest.mark.parametrize(
    "field", ["automatic_photos_delete", "automatic_source_delete", "allow_remote_overwrite"]
)
def test_safety_flags_cannot_be_enabled(field: str) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"safety": {field: True}})


def test_distinct_roots_are_required(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "storage": {
                    "staging_root": tmp_path / "same",
                    "fake_archive_root": tmp_path / "same",
                    "reports_root": tmp_path / "reports",
                }
            }
        )


def test_config_fingerprint_is_stable(app_config: AppConfig) -> None:
    assert app_config.fingerprint() == app_config.model_copy(deep=True).fingerprint()


def test_icloud_batch_size_does_not_change_archive_evidence_fingerprint(
    app_config: AppConfig,
) -> None:
    changed = app_config.model_copy(
        update={
            "icloud_cleanup": app_config.icloud_cleanup.model_copy(
                update={"batch_size": 1000}
            )
        }
    )
    assert changed.fingerprint() == app_config.fingerprint()


def test_large_video_threshold_is_100_mib_and_is_frozen_outside_job_fingerprint(
    app_config: AppConfig,
) -> None:
    assert app_config.archive_policy.large_video_threshold_bytes == 100 * 1024 * 1024
    changed = app_config.model_copy(
        update={
            "archive_policy": app_config.archive_policy.model_copy(
                update={"large_video_threshold_bytes": 200 * 1024 * 1024}
            )
        }
    )
    assert changed.fingerprint() == app_config.fingerprint()
