from __future__ import annotations

import csv
import io
import json
import os
from collections import Counter, defaultdict
from contextlib import suppress
from pathlib import Path
from typing import Any

from photoarchive.database import Database, utc_now
from photoarchive.paths import ensure_private_directory

CSV_FIELDS = [
    "asset_key",
    "photos_local_id",
    "creation_at_utc",
    "media_type",
    "required_resource_count",
    "verified_resource_count",
    "remote_paths",
    "verification_checked_at",
    "estimated_release_bytes",
    "warnings",
]

FOLDER_CSV_FIELDS = [
    "asset_key",
    "profile_id",
    "batch_id",
    "source_relative_paths",
    "completed_relative_paths",
    "creation_at_utc",
    "media_type",
    "required_resource_count",
    "verified_resource_count",
    "archive_paths",
    "verification_checked_at",
    "archived_bytes",
    "warnings",
]

PHONE_CSV_FIELDS = [
    "device_asset_key",
    "profile_id",
    "batch_id",
    "creation_at_utc",
    "media_type",
    "cleanup_state",
    "resource_count",
    "bytes",
    "deleted_at",
    "remaining_reason",
]

ICLOUD_CSV_FIELDS = [
    "asset_key",
    "profile_id",
    "batch_id",
    "selection_mode",
    "selection_threshold_bytes",
    "creation_at_utc",
    "media_type",
    "cleanup_state",
    "resource_count",
    "archived_bytes",
    "archive_paths",
    "deleted_at",
    "remaining_reason",
]


def _atomic_report(path: Path, content: bytes) -> None:
    partial = path.with_name(path.name + ".partial")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(partial, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.replace(partial, path)


def generate_report(database: Database, job_id: str, output: Path) -> tuple[Path, Path]:
    output = ensure_private_directory(output)
    job = database.get_job(job_id)
    source_adapter = job["source_adapter"] or job["photos_adapter"]
    is_folder = source_adapter == "folder"
    batch = database.get_batch(str(job["batch_id"])) if is_folder and job["batch_id"] else None
    phone_batch = None
    if is_folder and job["batch_id"]:
        with suppress(KeyError):
            phone_batch = database.get_phone_batch(str(job["batch_id"]))
    icloud_batch = database.get_icloud_batch_for_job(job_id)
    with database.connect() as connection:
        assets = connection.execute(
            """
            SELECT assets.*
            FROM assets
            JOIN job_assets ON job_assets.asset_id = assets.id
            WHERE job_assets.job_id = ?
            ORDER BY assets.creation_at_utc, assets.id
            """,
            (job_id,),
        ).fetchall()
        failures = [
            {
                "asset_key": row["id"][:8],
                "state": row["state"],
                "error_code": row["error_code"],
                "review_required": bool(row["review_required"]),
            }
            for row in assets
            if row["state"] == "FAILED" or row["review_required"]
        ]
        byte_rows = connection.execute(
            """
            SELECT assets.id, assets.state,
                   COALESCE(SUM(
                       CASE WHEN asset_resources.required = 1
                            THEN COALESCE(asset_resources.actual_size,
                                          asset_resources.expected_size, 0)
                            ELSE 0 END
                   ), 0) AS bytes
            FROM assets
            JOIN job_assets ON job_assets.asset_id = assets.id
            LEFT JOIN asset_resources ON asset_resources.asset_id = assets.id
            WHERE job_assets.job_id = ?
            GROUP BY assets.id, assets.state
            """,
            (job_id,),
        ).fetchall()
        verification_rows = connection.execute(
            """
            SELECT asset_resources.asset_id, asset_resources.resource_key,
                   archive_files.remote_path, verification_records.check_type,
                   verification_records.expected, verification_records.actual,
                   verification_records.passed, verification_records.checked_at
            FROM verification_records
            JOIN asset_resources
              ON asset_resources.id = verification_records.resource_id
            JOIN job_assets ON job_assets.asset_id = asset_resources.asset_id
            LEFT JOIN archive_files
              ON archive_files.resource_id = asset_resources.id
            WHERE job_assets.job_id = ?
            ORDER BY asset_resources.asset_id, asset_resources.resource_key,
                     verification_records.checked_at, verification_records.check_type
            """,
            (job_id,),
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        for asset in assets:
            if asset["state"] != "SAFE_TO_DELETE" or asset["review_required"]:
                continue
            resource_rows = connection.execute(
                """
                SELECT asset_resources.*, archive_files.remote_path,
                       MAX(verification_records.checked_at) AS checked_at
                FROM asset_resources
                JOIN archive_files ON archive_files.resource_id = asset_resources.id
                JOIN verification_records ON verification_records.resource_id = asset_resources.id
                WHERE asset_resources.asset_id = ? AND asset_resources.required = 1
                GROUP BY asset_resources.id, archive_files.remote_path
                ORDER BY asset_resources.resource_key
                """,
                (asset["id"],),
            ).fetchall()
            required_count = sum(1 for row in resource_rows if row["required"])
            verified_count = sum(1 for row in resource_rows if row["checked_at"])
            checked_values = [row["checked_at"] for row in resource_rows if row["checked_at"]]
            candidates.append(
                {
                    "asset_key": asset["id"][:8],
                    "photos_local_id": asset["photos_local_id"],
                    "creation_at_utc": asset["creation_at_utc"],
                    "media_type": asset["media_type"],
                    "required_resource_count": required_count,
                    "verified_resource_count": verified_count,
                    "remote_paths": ";".join(row["remote_path"] for row in resource_rows),
                    "verification_checked_at": max(checked_values) if checked_values else "",
                    "estimated_release_bytes": sum(
                        row["actual_size"] or 0 for row in resource_rows
                    ),
                    "warnings": "",
                }
            )
        folder_candidates: list[dict[str, Any]] = []
        if is_folder and batch is not None:
            for asset in assets:
                if asset["state"] != "SAFE_TO_DELETE" or asset["review_required"]:
                    continue
                rows = connection.execute(
                    """
                    SELECT batch_files.relative_path, batch_files.metadata_warning,
                           asset_resources.actual_size, archive_files.remote_path,
                           MAX(verification_records.checked_at) AS checked_at
                    FROM batch_files
                    JOIN asset_resources ON asset_resources.id = batch_files.resource_id
                    JOIN archive_files ON archive_files.resource_id = asset_resources.id
                    JOIN verification_records
                      ON verification_records.resource_id = asset_resources.id
                    WHERE batch_files.batch_id = ? AND asset_resources.asset_id = ?
                    GROUP BY batch_files.relative_path, batch_files.metadata_warning,
                             asset_resources.actual_size, archive_files.remote_path
                    ORDER BY batch_files.relative_path
                    """,
                    (batch["id"], asset["id"]),
                ).fetchall()
                checked = [row["checked_at"] for row in rows if row["checked_at"]]
                source_paths = [str(row["relative_path"]) for row in rows]
                completed_prefix = str(batch["completed_relative_path"])
                folder_candidates.append(
                    {
                        "asset_key": asset["id"][:8],
                        "profile_id": batch["profile_id"],
                        "batch_id": batch["id"],
                        "source_relative_paths": ";".join(source_paths),
                        "completed_relative_paths": ";".join(
                            str(Path(completed_prefix, path)) for path in source_paths
                        ),
                        "creation_at_utc": asset["creation_at_utc"],
                        "media_type": asset["media_type"],
                        "required_resource_count": len(rows),
                        "verified_resource_count": len(checked),
                        "archive_paths": ";".join(str(row["remote_path"]) for row in rows),
                        "verification_checked_at": max(checked) if checked else "",
                        "archived_bytes": sum(int(row["actual_size"] or 0) for row in rows),
                        "warnings": ";".join(
                            sorted(
                                {
                                    str(row["metadata_warning"])
                                    for row in rows
                                    if row["metadata_warning"]
                                }
                            )
                        ),
                    }
                )
        phone_rows: list[dict[str, Any]] = []
        if phone_batch is not None:
            raw_phone_rows = connection.execute(
                """
                SELECT phone_assets.device_asset_key, phone_assets.creation_at_utc,
                       phone_assets.media_type, phone_assets.cleanup_state,
                       phone_assets.deleted_at, phone_assets.error_code,
                       phone_assets.delete_error_detail,
                       COUNT(phone_items.id) AS resource_count,
                       COALESCE(SUM(phone_items.expected_size), 0) AS bytes
                FROM phone_assets
                JOIN phone_items ON phone_items.phone_asset_id = phone_assets.id
                WHERE phone_assets.batch_id = ?
                GROUP BY phone_assets.id
                ORDER BY phone_assets.creation_at_utc, phone_assets.device_asset_key
                """,
                (phone_batch["batch_id"],),
            ).fetchall()
            phone_rows = [
                {
                    "device_asset_key": str(row["device_asset_key"])[:12],
                    "profile_id": phone_batch["profile_id"],
                    "batch_id": phone_batch["batch_id"],
                    "creation_at_utc": row["creation_at_utc"],
                    "media_type": row["media_type"],
                    "cleanup_state": row["cleanup_state"],
                    "resource_count": row["resource_count"],
                    "bytes": row["bytes"],
                    "deleted_at": row["deleted_at"] or "",
                    "remaining_reason": row["delete_error_detail"] or row["error_code"] or "",
                }
                for row in raw_phone_rows
            ]
        icloud_rows: list[dict[str, Any]] = []
        if icloud_batch is not None:
            raw_icloud_rows = connection.execute(
                """
                SELECT assets.id, assets.creation_at_utc, assets.media_type,
                       icloud_batch_assets.cleanup_state,
                       icloud_batch_assets.deleted_at,
                       icloud_batch_assets.error_code,
                       icloud_batch_assets.delete_error_detail,
                       COUNT(asset_resources.id) AS resource_count,
                       COALESCE(SUM(asset_resources.actual_size), 0) AS archived_bytes,
                       GROUP_CONCAT(archive_files.remote_path, ';') AS archive_paths
                FROM icloud_batch_assets
                JOIN assets ON assets.id = icloud_batch_assets.asset_id
                LEFT JOIN asset_resources ON asset_resources.asset_id = assets.id
                LEFT JOIN archive_files ON archive_files.resource_id = asset_resources.id
                WHERE icloud_batch_assets.batch_id = ?
                GROUP BY icloud_batch_assets.id
                ORDER BY assets.creation_at_utc, assets.id
                """,
                (icloud_batch["batch_id"],),
            ).fetchall()
            icloud_rows = [
                {
                    "asset_key": str(row["id"])[:8],
                    "profile_id": icloud_batch["profile_id"],
                    "batch_id": icloud_batch["batch_id"],
                    "selection_mode": icloud_batch["selection_mode"],
                    "selection_threshold_bytes": (
                        icloud_batch["selection_threshold_bytes"] or ""
                    ),
                    "creation_at_utc": row["creation_at_utc"],
                    "media_type": row["media_type"],
                    "cleanup_state": row["cleanup_state"],
                    "resource_count": row["resource_count"],
                    "archived_bytes": row["archived_bytes"],
                    "archive_paths": row["archive_paths"] or "",
                    "deleted_at": row["deleted_at"] or "",
                    "remaining_reason": row["delete_error_detail"]
                    or row["error_code"]
                    or "",
                }
                for row in raw_icloud_rows
            ]
    counts = Counter(row["state"] for row in assets)
    state_bytes: defaultdict[str, int] = defaultdict(int)
    for row in byte_rows:
        state_bytes[row["state"]] += int(row["bytes"])
    states = {
        state: {"asset_count": count, "bytes": state_bytes[state]}
        for state, count in sorted(counts.items())
    }
    public_counts: dict[str, int] = dict(sorted(counts.items()))
    public_states = states
    if is_folder and "SAFE_TO_DELETE" in public_counts:
        public_counts["VERIFIED_FOR_FINALIZE"] = public_counts.pop("SAFE_TO_DELETE")
        public_states = dict(states)
        public_states["VERIFIED_FOR_FINALIZE"] = public_states.pop("SAFE_TO_DELETE")
    verification_evidence = [
        {
            "asset_key": row["asset_id"][:8],
            "resource_key": row["resource_key"],
            "remote_path": row["remote_path"],
            "check_type": row["check_type"],
            "expected": row["expected"],
            "actual": row["actual"],
            "passed": bool(row["passed"]),
            "checked_at": row["checked_at"],
        }
        for row in verification_rows
    ]
    report = {
        "report_schema_version": (
            4
            if icloud_batch is not None
            else (3 if phone_batch is not None else (2 if is_folder else 1))
        ),
        "job_id": job_id,
        "config_hash": job["config_hash"],
        "generated_at_utc": utc_now(),
        "counts": public_counts,
        "states": public_states,
        "candidate_bytes": sum(item["estimated_release_bytes"] for item in candidates),
        "failures": failures,
        "verification_evidence": verification_evidence,
        "candidates": candidates,
        "source_adapter": source_adapter,
        "target_adapter": job["target_adapter"] or job["onedrive_adapter"],
    }
    if is_folder and batch is not None:
        report.update(
            {
                "profile_id": batch["profile_id"],
                "batch_id": batch["id"],
                "batch_state": batch["state"],
                "archived_files": folder_candidates,
                "candidates": [],
            }
        )
    if phone_batch is not None:
        cleanup_counts = Counter(row["cleanup_state"] for row in phone_rows)
        report.update(
            {
                "phone_cleanup": {
                    "state": phone_batch["state"],
                    "cutoff_at_utc": phone_batch["cutoff_at_utc"],
                    "icloud_photos_enabled": (
                        None
                        if phone_batch["icloud_photos_enabled"] is None
                        else bool(phone_batch["icloud_photos_enabled"])
                    ),
                    "device_key": str(phone_batch["device_key"])[:12],
                    "plan_sha256": phone_batch["deletion_plan_sha256"],
                    "counts": dict(sorted(cleanup_counts.items())),
                    "assets": phone_rows,
                }
            }
        )
    if icloud_batch is not None:
        cleanup_counts = Counter(row["cleanup_state"] for row in icloud_rows)
        report.update(
            {
                "profile_id": icloud_batch["profile_id"],
                "batch_id": icloud_batch["batch_id"],
                "candidates": [],
                "icloud_cleanup": {
                    "state": icloud_batch["state"],
                    "cutoff_at_utc": icloud_batch["cutoff_at_utc"],
                    "selection_mode": icloud_batch["selection_mode"],
                    "selection_threshold_bytes": icloud_batch[
                        "selection_threshold_bytes"
                    ],
                    "plan_sha256": icloud_batch["deletion_plan_sha256"],
                    "counts": dict(sorted(cleanup_counts.items())),
                    "assets": icloud_rows,
                    "recently_deleted_action_required": any(
                        row["cleanup_state"] == "DELETED" for row in icloud_rows
                    ),
                },
            }
        )
    json_path = output / "report.json"
    csv_path = output / (
        "icloud_cleanup.csv"
        if icloud_batch is not None
        else (
            "phone_cleanup.csv"
            if phone_batch is not None
            else ("archived_files.csv" if is_folder else "deletion_candidates.csv")
        )
    )
    _atomic_report(
        json_path,
        (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )
    buffer = io.StringIO(newline="")
    csv_fields = (
        ICLOUD_CSV_FIELDS
        if icloud_batch is not None
        else (
            PHONE_CSV_FIELDS
            if phone_batch is not None
            else (FOLDER_CSV_FIELDS if is_folder else CSV_FIELDS)
        )
    )
    csv_rows = (
        icloud_rows
        if icloud_batch is not None
        else (
            phone_rows
            if phone_batch is not None
            else (folder_candidates if is_folder else candidates)
        )
    )
    writer = csv.DictWriter(buffer, fieldnames=csv_fields)
    writer.writeheader()
    writer.writerows(csv_rows)
    _atomic_report(csv_path, buffer.getvalue().encode("utf-8"))
    return json_path, csv_path
