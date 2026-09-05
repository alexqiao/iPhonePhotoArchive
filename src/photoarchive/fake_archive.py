from __future__ import annotations

import hashlib
import os
from pathlib import Path

from photoarchive.domain import ConflictError, IntegrityError, RemoteObject, UploadRequest
from photoarchive.hashing import hash_file
from photoarchive.paths import safe_path


class FakeArchiveTarget:
    """Local deterministic target used by fixture tests."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def adapter_name(self) -> str:
        return "fake"

    @property
    def archive_prefix(self) -> str:
        return "FixtureArchive"

    def put(self, request: UploadRequest) -> RemoteObject:
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
            raise ConflictError(f"archive path contains different content: {request.remote_path}")
        partial = destination.with_name(destination.name + ".partial")
        if partial.exists() and partial.is_symlink():
            raise IntegrityError("fake archive partial target is a symbolic link")
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
        copied_size, copied_sha256, copied_quickxor = hash_file(partial)
        if (
            copied_size != request.expected_size
            or copied_sha256 != request.expected_sha256
            or copied_quickxor != request.expected_quickxor
        ):
            raise IntegrityError("fake archive copy failed integrity validation")
        os.replace(partial, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return self.stat(request.remote_path)

    def stat(self, remote_path: str) -> RemoteObject:
        path = safe_path(self.root, remote_path)
        if not path.is_file():
            raise IntegrityError(f"archive object is missing: {remote_path}")
        size, sha256, quickxor = hash_file(path)
        item_digest = hashlib.sha256(remote_path.encode()).hexdigest()
        return RemoteObject(
            drive_item_id=f"fake_{item_digest[:24]}",
            remote_path=remote_path,
            size=size,
            etag=sha256,
            quickxor=quickxor,
        )
