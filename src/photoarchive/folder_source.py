from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from photoarchive.config import FolderImportConfig
from photoarchive.domain import (
    BatchNotReadyError,
    DiscoveredAsset,
    DiscoveredResource,
    DiscoveryRequest,
    ExportReceipt,
    IntegrityError,
    PathSafetyError,
    SourceChangedError,
)
from photoarchive.hashing import hash_file
from photoarchive.paths import safe_existing_path

_IMAGE_EXTENSIONS = {
    ".heic",
    ".heif",
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".gif",
    ".dng",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".raf",
    ".orf",
    ".rw2",
}
_RAW_EXTENSIONS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2"}
_VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}
_SIDECAR_EXTENSIONS = {".aae", ".xmp"}
_IGNORED_NAMES = {".DS_Store", ".photoarchive-batch.json"}


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    kind: str
    uti: str | None
    creation_at_utc: datetime | None
    content_identifier: str | None = None


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    device: int
    inode: int
    size: int
    mtime_ns: int


MetadataReader = Callable[[Path], MediaMetadata]


def _fallback_metadata(path: Path) -> MediaMetadata:
    suffix = path.suffix.casefold()
    if suffix in _RAW_EXTENSIONS:
        kind = "raw"
    elif suffix in _IMAGE_EXTENSIONS:
        kind = "photo"
    elif suffix in _VIDEO_EXTENSIONS:
        kind = "video"
    elif suffix in _SIDECAR_EXTENSIONS:
        kind = "sidecar"
    else:
        raise BatchNotReadyError(f"unsupported file in import batch: {path.name}")
    return MediaMetadata(kind, None, None)


class SwiftMetadataReader:
    def __init__(self, helper_path: Path) -> None:
        self.helper_path = helper_path

    def __call__(self, path: Path) -> MediaMetadata:
        if not self.helper_path.is_file():
            return _fallback_metadata(path)
        request_id = str(uuid.uuid4())
        envelope = {
            "schema_version": 1,
            "type": "resource",
            "request_id": request_id,
            "payload": {"path": str(path)},
        }
        completed = subprocess.run(
            [str(self.helper_path), "inspect-file", "--request-id", request_id],
            input=json.dumps(envelope) + "\n",
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            return _fallback_metadata(path)
        try:
            response = json.loads(completed.stdout.splitlines()[-1])
            payload = response["payload"]
            value = payload.get("creation_at_utc")
            created = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
            return MediaMetadata(
                str(payload["kind"]),
                payload.get("uti"),
                created.astimezone(UTC) if created else None,
                payload.get("content_identifier"),
            )
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return _fallback_metadata(path)


class FolderArchiveSource:
    def __init__(
        self,
        batch_root: Path,
        *,
        profile_id: str,
        batch_id: str,
        config: FolderImportConfig,
        metadata_reader: MetadataReader,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.batch_root = batch_root
        self.profile_id = profile_id
        self.batch_id = batch_id
        self.config = config
        self.metadata_reader = metadata_reader
        self.sleeper = sleeper

    @property
    def adapter_name(self) -> str:
        return "folder"

    @property
    def source_reference(self) -> str:
        return str(self.batch_root)

    def _files(self) -> list[Path]:
        if self.batch_root.is_symlink() or not self.batch_root.is_dir():
            raise PathSafetyError("import batch is missing or is a symbolic link")
        files: list[Path] = []
        for path in sorted(self.batch_root.rglob("*")):
            if path.name in _IGNORED_NAMES:
                continue
            if path.is_symlink():
                raise PathSafetyError(f"symbolic link in import batch: {path.name}")
            if path.is_dir():
                continue
            relative = path.relative_to(self.batch_root)
            files.append(safe_existing_path(self.batch_root, relative))
        if not files:
            raise BatchNotReadyError("import batch is empty")
        return files

    @staticmethod
    def _snapshot(path: Path) -> SourceSnapshot:
        stat = path.stat(follow_symlinks=False)
        if not path.is_file() or os.path.islink(path):
            raise PathSafetyError(f"source is not a regular file: {path.name}")
        return SourceSnapshot(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _stable_snapshots(self, files: list[Path]) -> dict[Path, SourceSnapshot]:
        first = {path: self._snapshot(path) for path in files}
        for poll in range(1, self.config.stable_poll_count):
            self.sleeper(self.config.stable_poll_interval_sec)
            current_files = self._files()
            if current_files != files:
                raise SourceChangedError(
                    "the import batch gained or lost files; wait for import to finish"
                )
            current = {path: self._snapshot(path) for path in current_files}
            if current != first:
                raise SourceChangedError(
                    "import files are still changing; wait for import to finish"
                )
            if poll + 1 == self.config.stable_poll_count:
                return current
        return first

    def discover(self, request: DiscoveryRequest) -> Iterable[DiscoveredAsset]:
        del request  # The selected folder is the complete, explicit archive set.
        files = self._files()
        snapshots = self._stable_snapshots(files)
        records: list[tuple[Path, MediaMetadata, str]] = []
        for path in files:
            metadata = self.metadata_reader(path)
            relative = path.relative_to(self.batch_root)
            fallback_key = str(relative.parent / unicodedata.normalize("NFC", path.stem).casefold())
            records.append((path, metadata, fallback_key))
        identifier_counts: dict[str, int] = defaultdict(int)
        for _, metadata, _ in records:
            if metadata.content_identifier:
                identifier_counts[metadata.content_identifier] += 1
        shared_identifier_by_stem: dict[str, str] = {}
        for _, metadata, fallback_key in records:
            identifier = metadata.content_identifier
            if identifier and identifier_counts[identifier] > 1:
                shared_identifier_by_stem[fallback_key] = identifier
        grouped: dict[str, list[tuple[Path, MediaMetadata]]] = defaultdict(list)
        for path, metadata, fallback_key in records:
            identifier = metadata.content_identifier
            shared_identifier = shared_identifier_by_stem.get(fallback_key)
            group_key = (
                f"id:{identifier or shared_identifier}"
                if (identifier and identifier_counts[identifier] > 1) or shared_identifier
                else fallback_key
            )
            grouped[group_key].append((path, metadata))

        for group_key, members in sorted(grouped.items()):
            kinds = {metadata.kind for _, metadata in members}
            has_image = bool(kinds & {"photo", "raw"})
            if "sidecar" in kinds and not has_image:
                raise BatchNotReadyError("orphan sidecar has no matching image")
            if sum(metadata.kind == "video" for _, metadata in members) > 1:
                raise BatchNotReadyError("ambiguous media group contains more than one video")
            media_type = (
                "live_photo"
                if has_image and "video" in kinds
                else ("video" if kinds == {"video"} else "image")
            )
            candidates = [
                metadata.creation_at_utc
                for _, metadata in members
                if metadata.creation_at_utc is not None
            ]
            fallback_time = min(
                datetime.fromtimestamp(snapshots[path].mtime_ns / 1_000_000_000, tz=UTC)
                for path, _ in members
            )
            creation_at = min(candidates) if candidates else fallback_time
            warnings = () if candidates else ("CREATION_TIME_FROM_FILE_MTIME",)
            resources: list[DiscoveredResource] = []
            for path, metadata in sorted(members, key=lambda item: str(item[0])):
                relative = path.relative_to(self.batch_root)
                snapshot = snapshots[path]
                size, source_sha256, source_quickxor = hash_file(path)
                if size != snapshot.size or self._snapshot(path) != snapshot:
                    raise SourceChangedError("source changed while its archive plan was created")
                resources.append(
                    DiscoveredResource(
                        resource_key=str(relative),
                        resource_type=metadata.kind,
                        uti=metadata.uti,
                        original_name=path.name,
                        required=True,
                        source_path=path,
                        expected_size=snapshot.size,
                        expected_mtime_ns=snapshot.mtime_ns,
                        expected_device=snapshot.device,
                        expected_inode=snapshot.inode,
                        source_relative_path=str(relative),
                        source_sha256=source_sha256,
                        source_quickxor=source_quickxor,
                    )
                )
            local_id = hashlib.sha256(f"{self.batch_id}\0{group_key}".encode()).hexdigest()
            yield DiscoveredAsset(
                library_id=f"folder:{self.profile_id}",
                photos_local_id=local_id,
                creation_at_utc=creation_at,
                media_type=media_type,
                required_resource_types=tuple(sorted({item.resource_type for item in resources})),
                resources=tuple(resources),
                profile_id=self.profile_id,
                batch_id=self.batch_id,
                metadata_warnings=warnings,
            )

    def export(self, resource: DiscoveredResource, partial_path: Path) -> ExportReceipt:
        source = safe_existing_path(self.batch_root, resource.source_relative_path or "")
        before = self._snapshot(source)
        expected = SourceSnapshot(
            int(resource.expected_device or before.device),
            int(resource.expected_inode or before.inode),
            int(resource.expected_size if resource.expected_size is not None else before.size),
            int(resource.expected_mtime_ns or before.mtime_ns),
        )
        if before != expected:
            raise SourceChangedError("source changed after the archive plan was created")
        partial_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(partial_path, flags, 0o600)
        try:
            with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target:
                descriptor = -1
                shutil.copyfileobj(source_handle, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if self._snapshot(source) != before:
            partial_path.unlink(missing_ok=True)
            raise SourceChangedError("source changed while it was being copied")
        size, sha256, quickxor = hash_file(partial_path)
        if size != expected.size:
            partial_path.unlink(missing_ok=True)
            raise IntegrityError("copied source size differs from the planned size")
        if resource.source_sha256 and sha256 != resource.source_sha256:
            partial_path.unlink(missing_ok=True)
            raise SourceChangedError("copied source hash differs from the archive plan")
        if resource.source_quickxor and quickxor != resource.source_quickxor:
            partial_path.unlink(missing_ok=True)
            raise SourceChangedError("copied source hash differs from the archive plan")
        return ExportReceipt(size, sha256, quickxor)
