from __future__ import annotations

import json
import select
import subprocess
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from photoarchive.config import PhoneCleanupConfig
from photoarchive.domain import (
    AdapterError,
    PhoneDeleteResult,
    PhoneDevice,
    PhoneResource,
    PhoneScan,
)


def _device(payload: dict[str, Any]) -> PhoneDevice:
    value = payload.get("icloud_photos_enabled")
    total = payload.get("total_capacity_bytes")
    available = payload.get("available_capacity_bytes")
    return PhoneDevice(
        device_key=str(payload["device_key"]),
        name=str(payload.get("name") or "iPhone"),
        product_kind=str(payload.get("product_kind") or "iPhone"),
        locked=bool(payload.get("locked", False)),
        trusted=bool(payload.get("trusted", False)),
        icloud_photos_enabled=value if isinstance(value, bool) else None,
        can_delete=bool(payload.get("can_delete", False)),
        total_capacity_bytes=(
            total if isinstance(total, int) and not isinstance(total, bool) else None
        ),
        available_capacity_bytes=(
            available if isinstance(available, int) and not isinstance(available, bool) else None
        ),
        delete_capability_declared=bool(payload.get("delete_capability_declared", False)),
        can_accept_ptp_commands=bool(payload.get("can_accept_ptp_commands", False)),
    )


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return str(value) if value is not None else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _resource(payload: dict[str, Any]) -> PhoneResource:
    return PhoneResource(
        token=str(payload["token"]),
        item_fingerprint=str(payload["item_fingerprint"]),
        ptp_object_handle=int(payload.get("ptp_object_handle", 0)),
        original_name=str(payload["original_name"]),
        size=int(payload["size"]),
        creation_at_utc=_parse_datetime(payload.get("creation_at_utc")),
        modification_at_utc=_parse_datetime(payload.get("modification_at_utc")),
        uti=_optional_text(payload, "uti"),
        asset_key=str(payload["asset_key"]),
        media_type=str(payload["media_type"]),
        required=bool(payload.get("required", True)),
        downloadable=bool(payload.get("downloadable", True)),
        warning=_optional_text(payload, "warning"),
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class SwiftPhoneSession:
    def __init__(self, helper_path: Path, config: PhoneCleanupConfig) -> None:
        if not helper_path.is_file():
            raise AdapterError(f"phone helper is missing: {helper_path}")
        self.config = config
        self.process = subprocess.Popen(
            [str(helper_path), "phone-session"],
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
            raise AdapterError("phone helper pipes are unavailable")
        request_id = str(uuid.uuid4())
        request = {
            "schema_version": 2,
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
                raise AdapterError(f"phone helper timed out during {command}")
            line = self.process.stdout.readline()
            if not line:
                error = ""
                if self.process.stderr is not None:
                    error = self.process.stderr.read().strip()
                raise AdapterError(error or "phone helper closed unexpectedly")
            try:
                envelope = cast(dict[str, Any], json.loads(line))
            except json.JSONDecodeError as exc:
                raise AdapterError("phone helper returned invalid JSONL") from exc
            if envelope.get("request_id") != request_id:
                continue
            kind = envelope.get("type")
            body = cast(dict[str, Any], envelope.get("payload") or {})
            if kind == "error":
                raise AdapterError(
                    f"{body.get('error_code', 'PHONE_HELPER_ERROR')}: "
                    f"{body.get('message', 'phone helper failed')}"
                )
            if kind == "result":
                return records, body
            records.append(envelope)

    def scan(self, cutoff_at_utc: str) -> PhoneScan:
        records, result = self._exchange(
            "discover",
            {
                "cutoff_at_utc": cutoff_at_utc,
                "discovery_timeout_sec": self.config.discovery_timeout_sec,
            },
        )
        device = _device(cast(dict[str, Any], result["device"]))
        raw_resources = tuple(
            _resource(cast(dict[str, Any], record["payload"]))
            for record in records
            if record.get("type") == "resource"
        )
        group_types: dict[str, set[str]] = {}
        for item in raw_resources:
            group_types.setdefault(item.asset_key, set()).add(item.media_type)
        resources = tuple(
            replace(item, media_type="live_photo")
            if group_types[item.asset_key] == {"image", "video"}
            else item
            for item in raw_resources
        )
        warnings = tuple(str(item) for item in result.get("warnings", []))
        return PhoneScan(
            device,
            resources,
            warnings,
            total_media_assets=_optional_int(result.get("total_media_assets")),
            total_media_resources=_optional_int(result.get("total_media_resources")),
            total_media_bytes=_optional_int(result.get("total_media_bytes")),
        )

    def download(self, resource: PhoneResource, partial_path: Path) -> None:
        _, result = self._exchange("download", {"token": resource.token, "path": str(partial_path)})
        if int(result.get("size", -1)) != resource.size:
            partial_path.unlink(missing_ok=True)
            raise AdapterError("downloaded phone item size differs from discovery")

    def revalidate(self, resources: tuple[PhoneResource, ...]) -> tuple[str, ...]:
        _, result = self._exchange(
            "revalidate",
            {
                "items": [
                    {
                        "token": item.token,
                        "item_fingerprint": item.item_fingerprint,
                        "size": item.size,
                    }
                    for item in resources
                ]
            },
        )
        return tuple(str(item) for item in result.get("valid_tokens", []))

    def delete(
        self,
        resources: tuple[PhoneResource, ...],
        *,
        batch_id: str,
        cutoff_at_utc: str,
        plan_sha256: str,
    ) -> PhoneDeleteResult:
        _, result = self._exchange(
            "delete",
            {
                "batch_id": batch_id,
                "cutoff_at_utc": cutoff_at_utc,
                "plan_sha256": plan_sha256,
                "tokens": [item.token for item in resources],
            },
        )
        return PhoneDeleteResult(
            tuple(str(item) for item in result.get("deleted_tokens", [])),
            tuple(str(item) for item in result.get("failed_tokens", [])),
            {
                str(token): str(reason)
                for token, reason in cast(
                    dict[str, Any], result.get("failure_reasons") or {}
                ).items()
            },
            _optional_text(result, "completion_error"),
            str(result.get("delete_method") or "requestDeleteFiles"),
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

    def __enter__(self) -> SwiftPhoneSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class SwiftPhoneClient:
    def __init__(self, helper_path: Path, config: PhoneCleanupConfig | None = None) -> None:
        self.helper_path = helper_path
        self.config = config or PhoneCleanupConfig()

    def open_session(self) -> SwiftPhoneSession:
        return SwiftPhoneSession(self.helper_path, self.config)

    def list_devices(self) -> tuple[PhoneDevice, ...]:
        with self.open_session() as session:
            scan = session.scan("0001-01-01T00:00:00Z")
            return (scan.device,)
