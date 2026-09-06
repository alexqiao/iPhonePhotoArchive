from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

MediaType = Literal["image", "video", "live_photo"]


def _default_media_types() -> list[MediaType]:
    return ["image", "video", "live_photo"]


class ArchivePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archive_after_years: int = Field(default=2, ge=1, le=20)
    cutoff_date: date | None = None
    media_types: list[MediaType] = Field(default_factory=_default_media_types)
    batch_size: int = Field(default=100, ge=1, le=10_000)
    large_video_threshold_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
    )

    @field_validator("media_types")
    @classmethod
    def unique_media_types(cls, value: list[MediaType]) -> list[MediaType]:
        if not value:
            raise ValueError("media_types must contain at least one type")
        return list(dict.fromkeys(value))


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_path: Path = Path("var/photoarchive.sqlite3")
    staging_root: Path = Path("var/staging")
    fake_archive_root: Path = Path("var/fake-archive")
    reports_root: Path = Path("var/reports")

    @field_validator("database_path", "staging_root", "fake_archive_root", "reports_root")
    @classmethod
    def normalize_path(cls, value: Path) -> Path:
        return value.expanduser().absolute()

    @model_validator(mode="after")
    def roots_must_be_distinct(self) -> StorageConfig:
        roots = {
            self.staging_root.resolve(strict=False),
            self.fake_archive_root.resolve(strict=False),
            self.reports_root.resolve(strict=False),
        }
        if len(roots) != 3:
            raise ValueError("staging, fake remote, and reports roots must be distinct")
        return self


class VerificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stable_poll_count: int = Field(default=2, ge=2, le=10)
    stable_poll_interval_sec: float = Field(default=30, ge=0, le=3600)


class RetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=8, ge=1, le=50)
    base_delay_sec: float = Field(default=5, ge=0, le=3600)
    max_delay_sec: float = Field(default=900, ge=0, le=86_400)

    @model_validator(mode="after")
    def delay_order(self) -> RetryConfig:
        if self.max_delay_sec < self.base_delay_sec:
            raise ValueError("max_delay_sec must be greater than or equal to base_delay_sec")
        return self


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    automatic_photos_delete: Literal[False] = False
    automatic_source_delete: Literal[False] = False
    allow_remote_overwrite: Literal[False] = False
    source_finalize_mode: Literal["move_to_completed"] = "move_to_completed"


class MediaHelperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    helper_path: Path = Path(
        "native/photos-helper/build/PhotoArchiveMediaHelper.app/Contents/MacOS/photos-helper"
    )

    @field_validator("helper_path")
    @classmethod
    def normalize_helper_path(cls, value: Path) -> Path:
        return value.expanduser().absolute()


class FolderImportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inbox_root: Path = Path("~/Pictures/PhotoArchiveInbox")
    stable_poll_count: int = Field(default=2, ge=2, le=10)
    stable_poll_interval_sec: float = Field(default=2, ge=0, le=60)
    open_image_capture: bool = True

    @field_validator("inbox_root")
    @classmethod
    def normalize_inbox_root(cls, value: Path) -> Path:
        return value.expanduser().absolute()


class PhoneCleanupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    confirmation_mode: Literal["per_batch"] = "per_batch"
    discovery_timeout_sec: float = Field(default=15, ge=1, le=120)
    command_timeout_sec: float = Field(default=600, ge=10, le=3600)
    delete_batch_size: int = Field(default=50, ge=1, le=500)


class ICloudCleanupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    confirmation_mode: Literal["per_batch"] = "per_batch"
    command_timeout_sec: float = Field(default=3600, ge=30, le=86_400)
    batch_size: int = Field(default=1000, ge=1, le=10_000)


class ExternalDriveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume_path: Path = Path("/Volumes/Elements")
    archive_directory: str = "PhotoArchive"
    expected_volume_name: str | None = "Elements"
    expected_volume_uuid: str | None = None
    minimum_free_bytes: int = Field(default=5 * 1024 * 1024 * 1024, ge=0)

    @field_validator("volume_path")
    @classmethod
    def normalize_volume_path(cls, value: Path) -> Path:
        return value.expanduser().absolute()

    @field_validator("archive_directory")
    @classmethod
    def safe_archive_directory(cls, value: str) -> str:
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
            raise ValueError("archive_directory must be one safe directory name")
        return value


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PHOTOARCHIVE__",
        env_nested_delimiter="__",
        extra="forbid",
    )

    version: Literal[1] = 1
    timezone: str = "Asia/Shanghai"
    archive_policy: ArchivePolicy = Field(default_factory=ArchivePolicy)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    media_helper: MediaHelperConfig = Field(default_factory=MediaHelperConfig)
    folder_import: FolderImportConfig = Field(default_factory=FolderImportConfig)
    phone_cleanup: PhoneCleanupConfig = Field(default_factory=PhoneCleanupConfig)
    icloud_cleanup: ICloudCleanupConfig = Field(default_factory=ICloudCleanupConfig)
    external_drive: ExternalDriveConfig = Field(default_factory=ExternalDriveConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del settings_cls, dotenv_settings, file_secret_settings
        return env_settings, init_settings

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    def cutoff_at_utc(self, now: datetime | None = None) -> datetime:
        zone = ZoneInfo(self.timezone)
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        if self.archive_policy.cutoff_date is not None:
            cutoff_day = self.archive_policy.cutoff_date
        else:
            local_day = current.astimezone(zone).date()
            target_year = local_day.year - self.archive_policy.archive_after_years
            try:
                cutoff_day = local_day.replace(year=target_year)
            except ValueError:
                cutoff_day = local_day.replace(year=target_year, day=28)
        local_midnight = datetime.combine(cutoff_day, time.min, tzinfo=zone)
        return local_midnight.astimezone(UTC)

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        # Operational chunk sizing may change between runs without changing any
        # source selection, archive identity, hash, or verification evidence.
        payload["icloud_cleanup"].pop("batch_size", None)
        # Large-video selection is stored immutably on its dedicated iCloud batch.
        # Excluding the new default also keeps pre-0.6 archive jobs resumable.
        payload["archive_policy"].pop("large_video_threshold_bytes", None)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def _deep_merge(base: dict[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        elif value is not None:
            merged[key] = value
    return merged


def load_config(
    path: Path | None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    raw: dict[str, Any] = {}
    if path is not None:
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError("configuration root must be a mapping")
        raw = loaded
    config = AppConfig(**raw)
    if cli_overrides:
        merged = _deep_merge(config.model_dump(mode="python"), cli_overrides)
        # The environment has already been resolved above. Disable it for this
        # second validation pass so explicit CLI values remain authoritative.
        config = AppConfig(  # type: ignore[call-arg]
            _env_prefix="__PHOTOARCHIVE_CLI_VALIDATION__", **merged
        )
    return config
