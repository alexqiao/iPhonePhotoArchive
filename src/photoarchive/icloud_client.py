from __future__ import annotations

import json
import select
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from photoarchive.config import ICloudCleanupConfig
from photoarchive.domain import (
    AdapterError,
    PhotoLibraryAsset,
    PhotoLibraryDeleteResult,
    PhotoLibraryResource,
    PhotoLibraryRevalidation,
    PhotoLibraryScan,
)


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise AdapterError("Photos helper omitted an asset creation date")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _resource(payload: dict[str, Any]) -> PhotoLibraryResource:
    uti = payload.get("uti")
    return PhotoLibraryResource(
        resource_key=str(payload["resource_key"]),
        resource_type=str(payload["resource_type"]),
        resource_type_code=int(payload["resource_type_code"]),
        original_name=str(payload["original_name"]),
        uti=str(uti) if uti is not None else None,
        ordinal=int(payload["ordinal"]),
    )


def _asset(payload: dict[str, Any]) -> PhotoLibraryAsset:
    raw_resources = cast(list[dict[str, Any]], payload.get("resources") or [])
    return PhotoLibraryAsset(
        local_identifier=str(payload["local_identifier"]),
        creation_at_utc=_parse_datetime(payload.get("creation_at_utc")),
        media_type=str(payload["media_type"]),
        resources=tuple(_resource(item) for item in raw_resources),
    )


def _asset_reference(asset: PhotoLibraryAsset) -> dict[str, Any]:
    return {
        "local_identifier": asset.local_identifier,
        "resource_keys": sorted(resource.resource_key for resource in asset.resources),
    }


class SwiftPhotoLibrarySession:
    def __init__(self, helper_path: Path, config: ICloudCleanupConfig) -> None:
        if not helper_path.is_file():
            raise AdapterError(f"Photos helper is missing: {helper_path}")
        self.config = config
        self.process = subprocess.Popen(
            [str(helper_path), "photo-library-session"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def _exchange(
        self, command: str, payload: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self.process.stdin is None or self.process.stdout is None:
            raise AdapterError("Photos helper pipes are unavailable")
        request_id = str(uuid.uuid4())
        request = {
            "schema_version": 3,
            "type": "request",
            "request_id": request_id,
            "payload": {"command": command, **(payload or {})},
        }
        self.process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        records: list[dict[str, Any]] = []
        while True:
            ready, _, _ = select.select(
                [self.process.stdout], [], [], self.config.command_timeout_sec
            )
            if not ready:
                self.close()
                raise AdapterError(f"Photos helper timed out during {command}")
            line = self.process.stdout.readline()
            if not line:
                error = ""
                if self.process.stderr is not None:
                    error = self.process.stderr.read().strip()
                raise AdapterError(error or "Photos helper closed unexpectedly")
            try:
                envelope = cast(dict[str, Any], json.loads(line))
            except json.JSONDecodeError as exc:
                raise AdapterError("Photos helper returned invalid JSONL") from exc
            if envelope.get("request_id") != request_id:
                continue
            kind = envelope.get("type")
            body = cast(dict[str, Any], envelope.get("payload") or {})
            if kind == "error":
                raise AdapterError(
                    f"{body.get('error_code', 'PHOTO_LIBRARY_ERROR')}: "
                    f"{body.get('message', 'Photos helper failed')}"
                )
            if kind == "result":
                return records, body
            records.append(envelope)

    def scan(self, cutoff_at_utc: str, media_types: frozenset[str]) -> PhotoLibraryScan:
        records, result = self._exchange(
            "discover",
            {"cutoff_at_utc": cutoff_at_utc, "media_types": sorted(media_types)},
        )
        assets = tuple(
            _asset(cast(dict[str, Any], record["payload"]))
            for record in records
            if record.get("type") == "asset"
        )
        return PhotoLibraryScan(
            authorization=str(result.get("authorization") or "unknown"),
            assets=assets,
            total_assets=int(result.get("total_assets", 0)),
            total_resources=int(result.get("total_resources", 0)),
            warnings=tuple(str(item) for item in result.get("warnings", [])),
        )

    def download(
        self, asset: PhotoLibraryAsset, resource_key: str, partial_path: Path
    ) -> int:
        _, result = self._exchange(
            "download",
            {
                "local_identifier": asset.local_identifier,
                "resource_key": resource_key,
                "path": str(partial_path),
            },
        )
        return int(result["size"])

    def revalidate(
        self,
        assets: tuple[PhotoLibraryAsset, ...],
        *,
        cutoff_at_utc: str,
    ) -> PhotoLibraryRevalidation:
        _, result = self._exchange(
            "revalidate",
            {
                "cutoff_at_utc": cutoff_at_utc,
                "assets": [_asset_reference(asset) for asset in assets],
            },
        )
        return PhotoLibraryRevalidation(
            tuple(str(item) for item in result.get("valid_local_identifiers", [])),
            tuple(str(item) for item in result.get("missing_local_identifiers", [])),
            tuple(str(item) for item in result.get("mismatched_local_identifiers", [])),
        )

    def delete(
        self,
        assets: tuple[PhotoLibraryAsset, ...],
        *,
        batch_id: str,
        cutoff_at_utc: str,
        plan_sha256: str,
    ) -> PhotoLibraryDeleteResult:
        _, result = self._exchange(
            "delete",
            {
                "batch_id": batch_id,
                "cutoff_at_utc": cutoff_at_utc,
                "plan_sha256": plan_sha256,
                "assets": [_asset_reference(asset) for asset in assets],
            },
        )
        reason = result.get("failure_reason")
        return PhotoLibraryDeleteResult(
            tuple(str(item) for item in result.get("deleted_local_identifiers", [])),
            tuple(str(item) for item in result.get("failed_local_identifiers", [])),
            str(reason) if reason is not None else None,
        )

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self._exchange("close")
        except (AdapterError, BrokenPipeError, OSError):
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def __enter__(self) -> SwiftPhotoLibrarySession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class SwiftPhotoLibraryClient:
    def __init__(self, helper_path: Path, config: ICloudCleanupConfig | None = None) -> None:
        self.helper_path = helper_path
        self.config = config or ICloudCleanupConfig()

    def open_session(self) -> SwiftPhotoLibrarySession:
        app_path = self.helper_path.parents[2]
        if app_path.suffix != ".app":
            raise AdapterError(f"Photos helper is not inside an app bundle: {self.helper_path}")
        try:
            completed = subprocess.run(
                [
                    "open",
                    "-W",
                    "-n",
                    str(app_path),
                    "--args",
                    "photo-library-authorize",
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=self.config.command_timeout_sec,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdapterError("could not request Photos access through macOS") from exc
        if completed.returncode != 0:
            raise AdapterError(completed.stderr.strip() or "macOS Photos authorization failed")
        return SwiftPhotoLibrarySession(self.helper_path, self.config)
