from __future__ import annotations

from pathlib import Path

import pytest

from photoarchive.domain import ConflictError, UploadRequest
from photoarchive.fake_archive import FakeArchiveTarget
from photoarchive.hashing import hash_file


def request_for(path: Path, remote_path: str) -> UploadRequest:
    size, sha256, quickxor = hash_file(path)
    return UploadRequest(path, remote_path, size, sha256, quickxor)


def test_same_content_is_idempotent_and_different_content_never_overwrites(tmp_path: Path) -> None:
    remote = FakeArchiveTarget(tmp_path / "remote")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    initial = remote.put(request_for(first, "archive/file.bin"))
    repeated = remote.put(request_for(first, "archive/file.bin"))
    assert repeated == initial
    with pytest.raises(ConflictError):
        remote.put(request_for(second, "archive/file.bin"))
    assert (tmp_path / "remote/archive/file.bin").read_bytes() == b"first"
