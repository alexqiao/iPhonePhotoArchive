from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from photoarchive.domain import (
    AdapterError,
    DiscoveredAsset,
    DiscoveredResource,
    DiscoveryRequest,
    ExportReceipt,
    PhotoLibraryAsset,
    PhotoLibraryResource,
    PhotoLibraryScan,
)
from photoarchive.hashing import hash_file
from photoarchive.protocols import PhotoLibrarySession


def _encode_source(asset: PhotoLibraryAsset, resource: PhotoLibraryResource) -> Path:
    payload = {
        "local_identifier": asset.local_identifier,
        "creation_at_utc": asset.creation_at_utc.isoformat().replace("+00:00", "Z"),
        "media_type": asset.media_type,
        "resource": {
            "resource_key": resource.resource_key,
            "resource_type": resource.resource_type,
            "resource_type_code": resource.resource_type_code,
            "original_name": resource.original_name,
            "uti": resource.uti,
            "ordinal": resource.ordinal,
        },
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode()
    return Path(f"photokit-{encoded}")


def decode_source(source: Path) -> tuple[PhotoLibraryAsset, PhotoLibraryResource]:
    value = str(source)
    if not value.startswith("photokit-"):
        raise AdapterError("invalid persisted PhotoKit resource reference")
    try:
        payload = cast(
            dict[str, Any],
            json.loads(base64.urlsafe_b64decode(value.removeprefix("photokit-")).decode()),
        )
        raw = cast(dict[str, Any], payload["resource"])
        resource = PhotoLibraryResource(
            resource_key=str(raw["resource_key"]),
            resource_type=str(raw["resource_type"]),
            resource_type_code=int(raw["resource_type_code"]),
            original_name=str(raw["original_name"]),
            uti=str(raw["uti"]) if raw.get("uti") is not None else None,
            ordinal=int(raw["ordinal"]),
        )
        asset = PhotoLibraryAsset(
            local_identifier=str(payload["local_identifier"]),
            creation_at_utc=datetime.fromisoformat(
                str(payload["creation_at_utc"]).replace("Z", "+00:00")
            ).astimezone(UTC),
            media_type=str(payload["media_type"]),
            resources=(resource,),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AdapterError("invalid persisted PhotoKit resource reference") from exc
    return asset, resource


class PhotoLibraryArchiveSource:
    def __init__(
        self,
        session: PhotoLibrarySession,
        *,
        profile_id: str,
        library_id: str,
        initial_scan: PhotoLibraryScan | None = None,
    ) -> None:
        self.session = session
        self.profile_id = profile_id
        self.library_id = library_id
        self.initial_scan = initial_scan

    @property
    def adapter_name(self) -> str:
        return "icloud"

    @property
    def source_reference(self) -> str:
        return self.library_id

    @property
    def remove_staged_after_verify(self) -> bool:
        return True

    def discover(self, request: DiscoveryRequest) -> Iterable[DiscoveredAsset]:
        cutoff = request.cutoff_at_utc.astimezone(UTC).isoformat().replace("+00:00", "Z")
        scan = self.initial_scan
        self.initial_scan = None
        if scan is None:
            scan = self.session.scan(cutoff, request.media_types)
        for asset in scan.assets:
            resources = tuple(
                DiscoveredResource(
                    resource_key=resource.resource_key,
                    resource_type=resource.resource_type,
                    uti=resource.uti,
                    original_name=resource.original_name,
                    required=True,
                    source_path=_encode_source(asset, resource),
                    expected_size=0,
                )
                for resource in asset.resources
            )
            required_types = tuple(sorted({item.resource_type for item in asset.resources}))
            if not required_types:
                required_types = ("missing_photokit_resource",)
            yield DiscoveredAsset(
                library_id=self.library_id,
                photos_local_id=asset.local_identifier,
                creation_at_utc=asset.creation_at_utc,
                media_type=asset.media_type,
                required_resource_types=required_types,
                resources=resources,
                profile_id=self.profile_id,
                metadata_warnings=("NO_PHOTOKIT_RESOURCES",) if not resources else (),
            )

    def export(self, resource: DiscoveredResource, partial_path: Path) -> ExportReceipt:
        asset, persisted = decode_source(resource.source_path)
        if persisted.resource_key != resource.resource_key:
            raise AdapterError("persisted PhotoKit resource key changed")
        partial_path.unlink(missing_ok=True)
        reported_size = self.session.download(asset, resource.resource_key, partial_path)
        size, sha256, quickxor = hash_file(partial_path)
        if size != reported_size:
            partial_path.unlink(missing_ok=True)
            raise AdapterError("downloaded PhotoKit resource size differs from helper result")
        return ExportReceipt(size=size, sha256=sha256, quickxor=quickxor)
