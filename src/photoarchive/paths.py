from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from photoarchive.domain import DiscoveredResource, PathSafetyError

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_SEPARATORS = re.compile(r"[/\\:]")


def ensure_private_directory(path: Path) -> Path:
    if path.exists() and path.is_symlink():
        raise PathSafetyError(f"root directory is a symbolic link: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path.resolve(strict=True)


def safe_path(root: Path, relative: str | Path) -> Path:
    root_path = ensure_private_directory(root)
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise PathSafetyError(f"unsafe relative path: {relative}")
    candidate = root_path.joinpath(relative_path)
    current = root_path
    for part in relative_path.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise PathSafetyError(f"symbolic link in target path: {current}")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root_path):
        raise PathSafetyError(f"path escapes configured root: {relative}")
    if candidate.exists() and candidate.is_symlink():
        raise PathSafetyError(f"target is a symbolic link: {candidate}")
    return candidate


def safe_existing_path(root: Path, relative: str | Path) -> Path:
    if root.is_symlink():
        raise PathSafetyError(f"root directory is a symbolic link: {root}")
    root_path = root.resolve(strict=True)
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise PathSafetyError(f"unsafe relative path: {relative}")
    candidate = root_path.joinpath(relative_path)
    current = root_path
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise PathSafetyError(f"symbolic link in source path: {current}")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root_path):
        raise PathSafetyError(f"path escapes configured root: {relative}")
    if not resolved.is_file():
        raise PathSafetyError(f"source is not a regular file: {relative}")
    return resolved


def sanitize_filename(name: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFC", name)
    normalized = _CONTROL_CHARS.sub("_", normalized)
    normalized = _SEPARATORS.sub("_", normalized).strip().rstrip(". ")
    if normalized in {"", ".", ".."}:
        return fallback
    return normalized


def archive_names(resources: Iterable[DiscoveredResource]) -> dict[str, str]:
    result: dict[str, str] = {}
    used: set[str] = set()
    for index, resource in enumerate(sorted(resources, key=lambda item: item.resource_key)):
        base = sanitize_filename(resource.original_name, f"resource-{index}")
        candidate = base
        stem = Path(base).stem
        suffix = Path(base).suffix
        if candidate.casefold() in used:
            candidate = f"{stem}__{resource.resource_type}_{index}{suffix}"
        used.add(candidate.casefold())
        result[resource.resource_key] = candidate
    return result


def asset_id(library_id: str, photos_local_id: str) -> str:
    return hashlib.sha256(f"{library_id}\0{photos_local_id}".encode()).hexdigest()[:32]


def resource_id(parent_asset_id: str, resource_key: str) -> str:
    return hashlib.sha256(f"{parent_asset_id}\0{resource_key}".encode()).hexdigest()[:32]


def asset_short_key(library_id: str, photos_local_id: str) -> str:
    return hashlib.sha256(f"{library_id}\0{photos_local_id}".encode()).hexdigest()[:8]


def archive_asset_directory(
    creation_at_utc: datetime,
    timezone_name: str,
    short_key: str,
) -> PurePosixPath:
    aware = creation_at_utc.astimezone(UTC)
    local = aware.astimezone(ZoneInfo(timezone_name))
    prefix = aware.strftime("%Y%m%dT%H%M%SZ")
    return PurePosixPath(f"{local.year:04d}", f"{local.month:02d}", f"{prefix}_{short_key}")
