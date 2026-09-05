from __future__ import annotations

import hashlib
import os
import plistlib
import subprocess
from dataclasses import asdict, dataclass
from typing import Any, cast

from photoarchive.config import ExternalDriveConfig
from photoarchive.domain import (
    AdapterError,
    ConflictError,
    IntegrityError,
    RemoteObject,
    UploadRequest,
)
from photoarchive.hashing import hash_file
from photoarchive.paths import safe_path


@dataclass(frozen=True, slots=True)
class ExternalDriveStatus:
    volume_path: str
    volume_name: str | None
    volume_uuid: str | None
    filesystem: str | None
    mounted: bool
    writable: bool
    free_bytes: int | None
    identity_matches: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExternalDriveClient:
    def __init__(
        self,
        config: ExternalDriveConfig,
        *,
        require_mount: bool = True,
    ) -> None:
        self.config = config
        self.root = config.volume_path / config.archive_directory
        self.require_mount = require_mount

    @property
    def adapter_name(self) -> str:
        return "external"

    @property
    def archive_prefix(self) -> str:
        return ""

    @property
    def stable_poll_interval_sec(self) -> float:
        return 1.0

    def _disk_info(self) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                ["diskutil", "info", "-plist", str(self.config.volume_path)],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        if completed.returncode != 0:
            return {}
        try:
            return cast(dict[str, Any], plistlib.loads(completed.stdout))
        except plistlib.InvalidFileException:
            return {}

    def inspect(self) -> ExternalDriveStatus:
        volume = self.config.volume_path
        exists = volume.is_dir() and not volume.is_symlink()
        mounted = exists and (os.path.ismount(volume) or not self.require_mount)
        info = self._disk_info() if exists else {}
        writable = False
        free_bytes: int | None = None
        if mounted:
            statistics = os.statvfs(volume)
            writable = not bool(statistics.f_flag & os.ST_RDONLY)
            if info.get("Writable") is not None:
                writable = writable and bool(info["Writable"])
            free_bytes = statistics.f_bavail * statistics.f_frsize
        volume_name = cast(str | None, info.get("VolumeName")) or (volume.name if exists else None)
        volume_uuid = cast(str | None, info.get("VolumeUUID"))
        filesystem = cast(str | None, info.get("FilesystemName") or info.get("FilesystemType"))
        name_matches = (
            self.config.expected_volume_name is None
            or volume_name == self.config.expected_volume_name
        )
        uuid_matches = (
            self.config.expected_volume_uuid is None
            or volume_uuid == self.config.expected_volume_uuid
        )
        return ExternalDriveStatus(
            volume_path=str(volume),
            volume_name=volume_name,
            volume_uuid=volume_uuid,
            filesystem=filesystem,
            mounted=mounted,
            writable=writable,
            free_bytes=free_bytes,
            identity_matches=name_matches and uuid_matches,
        )

    def assert_ready(self, required_bytes: int = 0) -> ExternalDriveStatus:
        status = self.inspect()
        if not status.mounted:
            raise AdapterError("configured external drive is not mounted")
        if not status.identity_matches:
            raise AdapterError("mounted external drive does not match configured identity")
        if not status.writable:
            raise AdapterError(
                f"external drive is read-only ({status.filesystem or 'unknown filesystem'})"
            )
        required_free = required_bytes + self.config.minimum_free_bytes
        if status.free_bytes is None or status.free_bytes < required_free:
            raise AdapterError("external drive does not have enough free space")
        return status

    def put(self, request: UploadRequest) -> RemoteObject:
        self.assert_ready(request.expected_size)
        destination = safe_path(self.root, request.remote_path)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = safe_path(self.root, request.remote_path)
        if destination.exists():
            size, sha256, quickxor = hash_file(destination)
            if (
                size == request.expected_size
                and sha256 == request.expected_sha256
                and quickxor == request.expected_quickxor
            ):
                return self.stat(request.remote_path)
            raise ConflictError(
                f"external drive path contains different content: {request.remote_path}"
            )
        partial = destination.with_name(destination.name + ".partial")
        if partial.exists() and partial.is_symlink():
            raise IntegrityError("external drive partial target is a symbolic link")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(partial, flags, 0o600)
        try:
            with request.local_path.open("rb") as source, os.fdopen(descriptor, "wb") as target:
                descriptor = -1
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        size, sha256, quickxor = hash_file(partial)
        if (
            size != request.expected_size
            or sha256 != request.expected_sha256
            or quickxor != request.expected_quickxor
        ):
            raise IntegrityError("external drive copy failed integrity validation")
        os.replace(partial, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return self.stat(request.remote_path)

    def stat(self, remote_path: str) -> RemoteObject:
        self.assert_ready()
        path = safe_path(self.root, remote_path)
        if not path.is_file():
            raise IntegrityError(f"external archive object is missing: {remote_path}")
        size, sha256, quickxor = hash_file(path)
        item_digest = hashlib.sha256(remote_path.encode()).hexdigest()
        return RemoteObject(
            drive_item_id=f"external_{item_digest[:24]}",
            remote_path=remote_path,
            size=size,
            etag=sha256,
            quickxor=quickxor,
        )
