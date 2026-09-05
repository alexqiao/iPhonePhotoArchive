from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class PhotoArchiveError(Exception):
    """Base error with a stable machine-readable code."""

    code = "PHOTOARCHIVE_ERROR"


class ConflictError(PhotoArchiveError):
    code = "REMOTE_CONFLICT"


class IntegrityError(PhotoArchiveError):
    code = "INTEGRITY_ERROR"


class PathSafetyError(PhotoArchiveError):
    code = "PATH_SAFETY_ERROR"


class ConfigDriftError(PhotoArchiveError):
    code = "CONFIG_DRIFT"


class AuthenticationError(PhotoArchiveError):
    code = "AUTHENTICATION_REQUIRED"


class AdapterError(PhotoArchiveError):
    code = "ADAPTER_ERROR"


class SourceChangedError(PhotoArchiveError):
    code = "SOURCE_CHANGED"


class BatchNotReadyError(PhotoArchiveError):
    code = "BATCH_NOT_READY"


class PhoneSafetyError(PhotoArchiveError):
    code = "PHONE_SAFETY_ERROR"


class DeviceMismatchError(PhotoArchiveError):
    code = "DEVICE_MISMATCH"


class PhoneCleanupComplete(PhotoArchiveError):
    code = "PHONE_CLEANUP_COMPLETE"


class ICloudCleanupComplete(PhotoArchiveError):
    code = "ICLOUD_CLEANUP_COMPLETE"


class StateTransitionError(PhotoArchiveError):
    code = "ILLEGAL_STATE_TRANSITION"


class AssetState(StrEnum):
    DISCOVERED = "DISCOVERED"
    EXPORTED = "EXPORTED"
    UPLOADED = "UPLOADED"
    VERIFIED = "VERIFIED"
    SAFE_TO_DELETE = "SAFE_TO_DELETE"
    FAILED = "FAILED"


SUCCESSOR: dict[AssetState, AssetState] = {
    AssetState.DISCOVERED: AssetState.EXPORTED,
    AssetState.EXPORTED: AssetState.UPLOADED,
    AssetState.UPLOADED: AssetState.VERIFIED,
    AssetState.VERIFIED: AssetState.SAFE_TO_DELETE,
}


def validate_transition(
    current: AssetState,
    target: AssetState,
    *,
    last_stable_state: AssetState | None = None,
) -> None:
    if target is AssetState.FAILED and current in SUCCESSOR:
        return
    if current is AssetState.FAILED and target is last_stable_state:
        return
    if SUCCESSOR.get(current) is target:
        return
    raise StateTransitionError(f"illegal state transition: {current} -> {target}")


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    cutoff_at_utc: datetime
    media_types: frozenset[str]
    batch_size: int


@dataclass(frozen=True, slots=True)
class DiscoveredResource:
    resource_key: str
    resource_type: str
    uti: str | None
    original_name: str
    required: bool
    source_path: Path
    expected_size: int | None = None
    expected_mtime_ns: int | None = None
    expected_device: int | None = None
    expected_inode: int | None = None
    source_relative_path: str | None = None
    source_sha256: str | None = None
    source_quickxor: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredAsset:
    library_id: str
    photos_local_id: str
    creation_at_utc: datetime
    media_type: str
    required_resource_types: tuple[str, ...]
    resources: tuple[DiscoveredResource, ...] = field(default_factory=tuple)
    profile_id: str | None = None
    batch_id: str | None = None
    metadata_warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ExportReceipt:
    size: int
    sha256: str
    quickxor: str


@dataclass(frozen=True, slots=True)
class UploadRequest:
    local_path: Path
    remote_path: str
    expected_size: int
    expected_sha256: str
    expected_quickxor: str
    resource_id: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteObject:
    drive_item_id: str
    remote_path: str
    size: int
    etag: str
    quickxor: str


@dataclass(frozen=True, slots=True)
class PhoneDevice:
    device_key: str
    name: str
    product_kind: str
    locked: bool
    trusted: bool
    icloud_photos_enabled: bool | None
    can_delete: bool
    total_capacity_bytes: int | None = None
    available_capacity_bytes: int | None = None
    delete_capability_declared: bool = False
    can_accept_ptp_commands: bool = False

    @property
    def used_capacity_bytes(self) -> int | None:
        if self.total_capacity_bytes is None or self.available_capacity_bytes is None:
            return None
        return max(0, self.total_capacity_bytes - self.available_capacity_bytes)


@dataclass(frozen=True, slots=True)
class PhoneResource:
    token: str
    item_fingerprint: str
    ptp_object_handle: int
    original_name: str
    size: int
    creation_at_utc: datetime | None
    modification_at_utc: datetime | None
    uti: str | None
    asset_key: str
    media_type: str
    required: bool = True
    downloadable: bool = True
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class PhoneScan:
    device: PhoneDevice
    resources: tuple[PhoneResource, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    total_media_assets: int | None = None
    total_media_resources: int | None = None
    total_media_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class PhoneDeleteResult:
    deleted_tokens: tuple[str, ...]
    failed_tokens: tuple[str, ...]
    failure_reasons: dict[str, str] = field(default_factory=dict)
    completion_error: str | None = None
    delete_method: str = "requestDeleteFiles"


@dataclass(frozen=True, slots=True)
class PhotoLibraryResource:
    resource_key: str
    resource_type: str
    resource_type_code: int
    original_name: str
    uti: str | None
    ordinal: int


@dataclass(frozen=True, slots=True)
class PhotoLibraryAsset:
    local_identifier: str
    creation_at_utc: datetime
    media_type: str
    resources: tuple[PhotoLibraryResource, ...]


@dataclass(frozen=True, slots=True)
class PhotoLibraryScan:
    authorization: str
    assets: tuple[PhotoLibraryAsset, ...]
    total_assets: int
    total_resources: int
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PhotoLibraryRevalidation:
    valid_local_identifiers: tuple[str, ...]
    missing_local_identifiers: tuple[str, ...]
    mismatched_local_identifiers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PhotoLibraryDeleteResult:
    deleted_local_identifiers: tuple[str, ...]
    failed_local_identifiers: tuple[str, ...]
    failure_reason: str | None = None
