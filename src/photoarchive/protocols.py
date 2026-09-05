from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from photoarchive.domain import (
    DiscoveredAsset,
    DiscoveredResource,
    DiscoveryRequest,
    ExportReceipt,
    PhoneDeleteResult,
    PhoneDevice,
    PhoneResource,
    PhoneScan,
    PhotoLibraryAsset,
    PhotoLibraryDeleteResult,
    PhotoLibraryRevalidation,
    PhotoLibraryScan,
    RemoteObject,
    UploadRequest,
)


class ArchiveSource(Protocol):
    @property
    def adapter_name(self) -> str: ...

    @property
    def source_reference(self) -> str: ...

    def discover(self, request: DiscoveryRequest) -> Iterable[DiscoveredAsset]: ...

    def export(self, resource: DiscoveredResource, partial_path: Path) -> ExportReceipt: ...


class ArchiveTarget(Protocol):
    @property
    def adapter_name(self) -> str: ...

    @property
    def archive_prefix(self) -> str: ...

    def put(self, request: UploadRequest) -> RemoteObject: ...

    def stat(self, remote_path: str) -> RemoteObject: ...


class PhoneSession(Protocol):
    def scan(self, cutoff_at_utc: str) -> PhoneScan: ...

    def download(self, resource: PhoneResource, partial_path: Path) -> None: ...

    def revalidate(self, resources: tuple[PhoneResource, ...]) -> tuple[str, ...]: ...

    def delete(
        self,
        resources: tuple[PhoneResource, ...],
        *,
        batch_id: str,
        cutoff_at_utc: str,
        plan_sha256: str,
    ) -> PhoneDeleteResult: ...

    def close(self) -> None: ...


class PhoneClient(Protocol):
    def open_session(self) -> PhoneSession: ...

    def list_devices(self) -> tuple[PhoneDevice, ...]: ...


class PhotoLibrarySession(Protocol):
    def scan(self, cutoff_at_utc: str, media_types: frozenset[str]) -> PhotoLibraryScan: ...

    def download(self, asset: PhotoLibraryAsset, resource_key: str, partial_path: Path) -> int: ...

    def revalidate(
        self,
        assets: tuple[PhotoLibraryAsset, ...],
        *,
        cutoff_at_utc: str,
    ) -> PhotoLibraryRevalidation: ...

    def delete(
        self,
        assets: tuple[PhotoLibraryAsset, ...],
        *,
        batch_id: str,
        cutoff_at_utc: str,
        plan_sha256: str,
    ) -> PhotoLibraryDeleteResult: ...

    def close(self) -> None: ...


class PhotoLibraryClient(Protocol):
    def open_session(self) -> PhotoLibrarySession: ...
