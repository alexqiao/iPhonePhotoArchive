from __future__ import annotations

from pathlib import Path

import pytest

from photoarchive.config import ExternalDriveConfig
from photoarchive.domain import AdapterError, ConflictError, UploadRequest
from photoarchive.external_drive import ExternalDriveClient, ExternalDriveStatus
from photoarchive.hashing import hash_file


def client_for(volume: Path) -> ExternalDriveClient:
    return ExternalDriveClient(
        ExternalDriveConfig(
            volume_path=volume,
            expected_volume_name=volume.name,
            minimum_free_bytes=0,
        ),
        require_mount=False,
    )


def test_external_drive_copy_is_atomic_and_idempotent(tmp_path: Path) -> None:
    volume = tmp_path / "drive"
    volume.mkdir()
    source = tmp_path / "photo.bin"
    source.write_bytes(b"original photo")
    size, sha256, quickxor = hash_file(source)
    client = client_for(volume)
    assert client.stable_poll_interval_sec == 1.0
    request = UploadRequest(source, "2020/01/photo.bin", size, sha256, quickxor)

    first = client.put(request)
    second = client.put(request)

    assert first == second
    assert (volume / "PhotoArchive/2020/01/photo.bin").read_bytes() == b"original photo"
    assert not list(volume.rglob("*.partial"))


def test_external_drive_never_overwrites_different_content(tmp_path: Path) -> None:
    volume = tmp_path / "drive"
    destination = volume / "PhotoArchive/2020/01/photo.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")
    source = tmp_path / "photo.bin"
    source.write_bytes(b"new")
    size, sha256, quickxor = hash_file(source)

    with pytest.raises(ConflictError):
        client_for(volume).put(UploadRequest(source, "2020/01/photo.bin", size, sha256, quickxor))
    assert destination.read_bytes() == b"existing"


def test_external_drive_rejects_read_only_volume(tmp_path: Path) -> None:
    volume = tmp_path / "drive"
    volume.mkdir()
    client = client_for(volume)
    client.inspect = lambda: ExternalDriveStatus(  # type: ignore[method-assign]
        volume_path=str(volume),
        volume_name=volume.name,
        volume_uuid=None,
        filesystem="NTFS",
        mounted=True,
        writable=False,
        free_bytes=1_000_000,
        identity_matches=True,
    )
    with pytest.raises(AdapterError, match=r"read-only.*NTFS"):
        client.assert_ready()


def test_external_drive_rejects_missing_mount(tmp_path: Path) -> None:
    client = ExternalDriveClient(
        ExternalDriveConfig(
            volume_path=tmp_path / "missing",
            expected_volume_name=None,
            minimum_free_bytes=0,
        )
    )
    with pytest.raises(AdapterError, match="not mounted"):
        client.assert_ready()
