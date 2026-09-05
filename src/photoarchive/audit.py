from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(
        self,
        *,
        level: str,
        event_code: str,
        run_id: str,
        job_id: str | None = None,
        asset_key: str | None = None,
        duration_ms: int | None = None,
        retry: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        del level, details
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record = {
            "event_code": event_code,
            "run_id": run_id,
            "job_id": job_id,
            "asset_key": asset_key,
            "duration_ms": duration_ms,
            "retry": retry,
        }
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
