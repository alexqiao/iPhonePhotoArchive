from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from photoarchive.domain import DiscoveredResource, PathSafetyError
from photoarchive.paths import archive_names, safe_path, sanitize_filename


def test_filename_is_nfc_and_sanitized() -> None:
    decomposed = "Cafe\u0301/unsafe. "
    result = sanitize_filename(decomposed, "fallback")
    assert unicodedata.is_normalized("NFC", result)
    assert "/" not in result
    assert not result.endswith((".", " "))


def test_duplicate_names_get_deterministic_suffix(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"fixture")
    resources = [
        DiscoveredResource("a", "photo", None, "same.jpg", True, source),
        DiscoveredResource("b", "paired_video", None, "SAME.JPG", True, source),
    ]
    names = archive_names(resources)
    assert names["a"] == "same.jpg"
    assert names["b"] == "SAME__paired_video_1.JPG"


@pytest.mark.parametrize("relative", ["../escape", "/tmp/escape"])
def test_root_escape_is_rejected(tmp_path: Path, relative: str) -> None:
    with pytest.raises(PathSafetyError):
        safe_path(tmp_path / "root", relative)


def test_symbolic_link_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathSafetyError):
        safe_path(root, "link/file")
