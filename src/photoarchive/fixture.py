from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from photoarchive.domain import (
    DiscoveredAsset,
    DiscoveredResource,
    DiscoveryRequest,
    ExportReceipt,
    IntegrityError,
)
from photoarchive.hashing import QuickXorHash
from photoarchive.paths import safe_existing_path


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise IntegrityError("fixture creation_at must include a timezone")
    return parsed.astimezone(UTC)


class FixturePhotosClient:
    def __init__(self, fixture_path: Path) -> None:
        self.fixture_path = fixture_path.resolve(strict=True)
        self.fixture_root = self.fixture_path.parent
        self._assets = self._load()

    @property
    def adapter_name(self) -> str:
        return "fixture"

    @property
    def source_reference(self) -> str:
        return str(self.fixture_path)

    def _load(self) -> tuple[DiscoveredAsset, ...]:
        assets: dict[str, dict[str, Any]] = {}
        resources: dict[str, list[DiscoveredResource]] = {}
        with self.fixture_path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    envelope = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise IntegrityError(f"invalid JSONL at line {line_number}") from exc
                if envelope.get("schema_version") != 1:
                    raise IntegrityError(f"unsupported fixture schema at line {line_number}")
                record_type = envelope.get("type")
                payload = envelope.get("payload")
                if not isinstance(payload, dict):
                    raise IntegrityError(f"fixture payload must be an object at line {line_number}")
                photos_id = payload.get("photos_local_id")
                if not isinstance(photos_id, str) or not photos_id:
                    raise IntegrityError(f"missing photos_local_id at line {line_number}")
                if record_type == "asset":
                    if photos_id in assets:
                        raise IntegrityError(f"duplicate asset in fixture: {photos_id}")
                    assets[photos_id] = payload
                elif record_type == "resource":
                    source_ref = payload.get("source_path")
                    if not isinstance(source_ref, str):
                        raise IntegrityError(f"missing source_path at line {line_number}")
                    source_path = safe_existing_path(self.fixture_root, source_ref)
                    resource = DiscoveredResource(
                        resource_key=str(payload["resource_key"]),
                        resource_type=str(payload["resource_type"]),
                        uti=str(payload["uti"]) if payload.get("uti") is not None else None,
                        original_name=str(payload["original_name"]),
                        required=bool(payload.get("required", True)),
                        source_path=source_path,
                        expected_size=source_path.stat().st_size,
                    )
                    resources.setdefault(photos_id, []).append(resource)
                else:
                    raise IntegrityError(f"unsupported fixture record type at line {line_number}")
        result: list[DiscoveredAsset] = []
        for photos_id, payload in assets.items():
            creation_value = payload.get("creation_at")
            if not isinstance(creation_value, str):
                continue
            creation_at = _parse_datetime(creation_value)
            if creation_at.year < 1900:
                continue
            required_types = payload.get("required_resource_types", [])
            if not isinstance(required_types, list) or not all(
                isinstance(item, str) for item in required_types
            ):
                raise IntegrityError(f"invalid required_resource_types for {photos_id}")
            result.append(
                DiscoveredAsset(
                    library_id=str(payload["library_id"]),
                    photos_local_id=photos_id,
                    creation_at_utc=creation_at,
                    media_type=str(payload["media_type"]),
                    required_resource_types=tuple(required_types),
                    resources=tuple(resources.get(photos_id, [])),
                )
            )
        return tuple(result)

    def discover(self, request: DiscoveryRequest) -> Iterable[DiscoveredAsset]:
        count = 0
        for asset in sorted(self._assets, key=lambda item: item.creation_at_utc):
            if asset.media_type not in request.media_types:
                continue
            if asset.creation_at_utc >= request.cutoff_at_utc:
                continue
            yield asset
            count += 1
            if count % request.batch_size == 0:
                continue

    def export(self, resource: DiscoveredResource, partial_path: Path) -> ExportReceipt:
        partial_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if partial_path.exists() and partial_path.is_symlink():
            raise IntegrityError("export target is a symbolic link")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(partial_path, flags, 0o600)
        sha256 = hashlib.sha256()
        quickxor = QuickXorHash()
        size = 0
        try:
            with resource.source_path.open("rb") as source, os.fdopen(descriptor, "wb") as target:
                descriptor = -1
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                    sha256.update(chunk)
                    quickxor.update(chunk)
                    size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return ExportReceipt(size, sha256.hexdigest(), quickxor.base64digest())
