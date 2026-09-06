from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from photoarchive.batches import BatchManager
from photoarchive.config import AppConfig, load_config
from photoarchive.database import Database
from photoarchive.domain import (
    ICloudCleanupComplete,
    PhoneCleanupComplete,
    PhoneSafetyError,
    PhotoArchiveError,
)
from photoarchive.external_drive import ExternalDriveClient
from photoarchive.fake_archive import FakeArchiveTarget
from photoarchive.fixture import FixturePhotosClient
from photoarchive.icloud_client import SwiftPhotoLibraryClient
from photoarchive.icloud_workflow import (
    ICloudCleanupPlan,
    ICloudPipelineScope,
    ICloudWorkflow,
)
from photoarchive.locking import FileLock, LockUnavailableError
from photoarchive.paths import safe_path
from photoarchive.phone_client import SwiftPhoneClient
from photoarchive.phone_workflow import CleanupPlan, PhoneWorkflow
from photoarchive.pipeline import ArchiveRunner, Planner
from photoarchive.protocols import ArchiveSource, ArchiveTarget
from photoarchive.reporting import generate_report

app = typer.Typer(help="Archive verified iPhone and iCloud Photos originals safely.")
db_app = typer.Typer(help="Manage the local database.")
profile_app = typer.Typer(help="Keep each person's imports separate.")
batch_app = typer.Typer(help="Prepare and archive one Image Capture import.")
device_app = typer.Typer(help="Bind one physical iPhone to a person profile.")
phone_app = typer.Typer(help="Import, verify, and safely clean an iPhone.")
icloud_app = typer.Typer(help="Archive, verify, and safely clean the system iCloud Photos library.")
large_video_app = typer.Typer(help="Find and archive any-date iCloud Photos videos over 100 MiB.")
app.add_typer(db_app, name="db")
app.add_typer(profile_app, name="profile")
app.add_typer(batch_app, name="batch")
app.add_typer(device_app, name="device")
app.add_typer(phone_app, name="phone")
app.add_typer(icloud_app, name="icloud")
icloud_app.add_typer(large_video_app, name="large-video")


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    config_path: Path
    overrides: dict[str, Any]


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[Path, typer.Option("--config")] = Path("config/example.yaml"),
    timezone: Annotated[str | None, typer.Option("--timezone")] = None,
    archive_after_years: Annotated[
        int | None, typer.Option("--archive-after-years", min=1, max=20)
    ] = None,
    cutoff_date: Annotated[str | None, typer.Option("--cutoff-date")] = None,
    staging_root: Annotated[Path | None, typer.Option("--staging-root")] = None,
    fake_archive_root: Annotated[Path | None, typer.Option("--fake-archive-root")] = None,
    external_volume: Annotated[Path | None, typer.Option("--external-volume")] = None,
    inbox_root: Annotated[Path | None, typer.Option("--inbox-root")] = None,
) -> None:
    ctx.obj = RuntimeOptions(
        config,
        {
            "timezone": timezone,
            "archive_policy": {
                "archive_after_years": archive_after_years,
                "cutoff_date": cutoff_date,
            },
            "storage": {
                "staging_root": staging_root,
                "fake_archive_root": fake_archive_root,
            },
            "external_drive": {"volume_path": external_volume},
            "folder_import": {"inbox_root": inbox_root},
        },
    )


def _options(ctx: typer.Context) -> RuntimeOptions:
    root = ctx.find_root()
    if not isinstance(root.obj, RuntimeOptions):
        raise RuntimeError("PhotoArchive runtime options are unavailable")
    return root.obj


def _config(ctx: typer.Context) -> AppConfig:
    options = _options(ctx)
    return load_config(options.config_path, options.overrides)


def _database(config: AppConfig) -> Database:
    return Database(config.storage.database_path)


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable byte unit")


def _optional_human_bytes(value: Any) -> str:
    return _human_bytes(value) if isinstance(value, int) else "系统未提供"


def _optional_count(value: Any) -> str:
    return str(value) if isinstance(value, int) else "系统未提供"


def _phone_progress(event: str, details: dict[str, Any]) -> None:
    current = details.get("current")
    total = details.get("total")
    position = f" {current}/{total}" if current is not None and total is not None else ""
    archive_current = details.get("archive_current")
    archive_total = details.get("archive_total")
    archive_position = (
        f" {archive_current}/{archive_total}"
        if archive_current is not None and archive_total is not None
        else ""
    )
    asset = details.get("asset_key")
    asset_text = f" 资产 {asset}" if asset else ""
    stage = {
        "export": "准备校验副本",
        "copy": "复制到移动硬盘",
        "verify": "验证移动硬盘",
    }.get(str(details.get("stage")), str(details.get("stage", "unknown")))
    wait_seconds = float(details.get("wait_seconds", 0))
    wait_text = f", 下次检查等待 {wait_seconds:g} 秒" if wait_seconds > 0 else ""
    messages = {
        "PHONE_SCAN_STARTED": "正在读取 iPhone 媒体目录",
        "PHONE_SCAN_COMPLETED": (
            f"扫描完成: 手机照片/视频总量 "
            f"{_optional_count(details.get('total_media_assets'))} 项 "
            f"({_optional_count(details.get('total_media_resources'))} 个文件, "
            f"{_optional_human_bytes(details.get('total_media_bytes'))}); "
            f"符合两年条件 {details.get('assets', 0)} 项 "
            f"({details.get('resources', 0)} 个文件, "
            f"{_human_bytes(int(details.get('bytes', 0)))}); "
            f"本次新增归档 {details.get('new_archive_assets', 0)} 项 "
            f"({details.get('new_archive_resources', 0)} 个文件, "
            f"{_human_bytes(int(details.get('new_archive_bytes', 0)))}), "
            f"已归档待手机清理 {details.get('reusable_assets', 0)} 项 "
            f"({details.get('reusable_resources', 0)} 个文件); "
            f"手机总容量 "
            f"{_optional_human_bytes(details.get('total_capacity_bytes'))}, "
            f"已用 {_optional_human_bytes(details.get('used_capacity_bytes'))}, "
            f"可用 {_optional_human_bytes(details.get('available_capacity_bytes'))}"
        ),
        "PHONE_IMPORT_STARTED": (
            f"开始导入 {details.get('total', 0)} 个资源, "
            f"共 {_human_bytes(int(details.get('bytes', 0)))}"
        ),
        "PHONE_IMPORT_RESOURCE_STARTED": (
            f"[导入{position}]{asset_text} 开始读取 ({_human_bytes(int(details.get('bytes', 0)))})"
        ),
        "PHONE_IMPORT_RESOURCE_COMPLETED": f"[导入{position}]{asset_text} 完成",
        "PHONE_IMPORT_RESOURCE_REUSED": f"[导入{position}] 已归档: 跳过重复复制",
        "PHONE_IMPORT_RESOURCE_SKIPPED": (
            f"[导入{position}]{asset_text} 跳过: {details.get('reason', '未知原因')}"
        ),
        "PHONE_IMPORT_RESOURCE_FAILED": (
            f"[导入{position}]{asset_text} 失败: {details.get('reason', '未知原因')}"
        ),
        "PHONE_IMPORT_RESUMED": f"继续导入批次, 共 {details.get('total', 0)} 个资源",
        "PHONE_LEGACY_WARNING_REPAIRED": (f"已修复 {details.get('resources', 0)} 个旧版关联误判"),
        "PHONE_ARCHIVE_STARTED": "开始复制到 ExternalDisk01 并验证",
        "ARCHIVE_RUN_STARTED": f"归档队列共 {details.get('total', 0)} 个资产",
        "ARCHIVE_ASSET_STARTED": f"[归档{position}] 开始",
        "ARCHIVE_ASSET_COMPLETED": (
            f"[归档{position}] 完成: {details.get('state', 'UNKNOWN')}"
        ),
        "ARCHIVE_STAGE": f"[归档{archive_position}] 阶段: {stage}",
        "ARCHIVE_VERIFY_POLL": (
            f"[归档{archive_position}] 稳定性验证 {current}/{total}{wait_text}"
        ),
        "ARCHIVE_RUN_COMPLETED": f"归档队列完成: {details.get('counts', {})}",
        "PHONE_ARCHIVE_COMPLETED": f"外接盘归档完成: {details.get('counts', {})}",
        "PHONE_CLEANUP_REVALIDATION_STARTED": "正在重新核对手机删除对象",
        "PHONE_CLEANUP_READY": (
            f"删除计划就绪: {details.get('assets', 0)} 个资产, "
            f"{details.get('resources', 0)} 个文件, "
            f"{_human_bytes(int(details.get('bytes', 0)))}"
        ),
        "PHONE_DELETE_CHUNK_STARTED": f"[手机清理{position}] 开始",
        "PHONE_DELETE_CHUNK_COMPLETED": (
            f"[手机清理{position}] 删除 {details.get('deleted', 0)} 个文件, "
            f"保留 {details.get('failed', 0)} 个文件"
            f", 删除方式: {details.get('method', 'requestDeleteFiles')}"
            + (
                f", 失败原因: {'; '.join(str(item) for item in details.get('reasons', []))}"
                if details.get("reasons")
                else ""
            )
        ),
        "PHONE_CLEANUP_COMPLETED": (
            f"手机清理完成: 删除 {details.get('deleted', 0)} 个文件, "
            f"保留 {details.get('failed', 0)} 个文件"
        ),
    }
    message = messages.get(event)
    if message:
        timestamp = datetime.now().strftime("%H:%M:%S")
        typer.echo(f"[{timestamp}] {message}", err=True)


def _phone_workflow(config: AppConfig, database: Database) -> PhoneWorkflow:
    return PhoneWorkflow(
        config,
        database,
        SwiftPhoneClient(config.media_helper.helper_path, config.phone_cleanup),
        progress=_phone_progress,
    )


def _icloud_progress(event: str, details: dict[str, Any]) -> None:
    if event.startswith("ARCHIVE_"):
        _phone_progress(event, details)
        return
    messages = {
        "ICLOUD_SCAN_STARTED": "正在读取系统照片图库 (包括 iCloud Photos 元数据)",
        "ICLOUD_SCAN_COMPLETED": (
            f"扫描完成: 图库共 {details.get('total_assets', 0)} 项 "
            f"({details.get('total_resources', 0)} 个资源); "
            f"符合截止时间 {details.get('assets', 0)} 项 "
            f"({details.get('resources', 0)} 个资源); 容量需下载原件后确定"
        ),
        "ICLOUD_LARGE_VIDEO_SCAN_STARTED": "正在读取照片图库中的视频元数据",
        "ICLOUD_LARGE_VIDEO_SCAN_COMPLETED": (
            f"大视频扫描完成: 视频 {details.get('videos', 0)} 项; "
            f"已确认超过阈值 {details.get('large', 0)} 项; "
            f"待测量 {details.get('pending', 0)} 项"
        ),
        "ICLOUD_LARGE_VIDEO_MEASURE_STARTED": (
            f"[测量 {details.get('current', 0)}/{details.get('total', 0)}] "
            f"正在读取原始视频数据"
        ),
        "ICLOUD_LARGE_VIDEO_MEASURE_COMPLETED": (
            f"[测量 {details.get('current', 0)}/{details.get('total', 0)}] 完成: "
            + (
                "超过阈值"
                if details.get("exceeds_threshold")
                else f"{_human_bytes(int(details.get('observed_bytes', 0)))}"
            )
        ),
        "ICLOUD_LARGE_VIDEO_UNMEASURABLE": "跳过缺少原始视频资源的资产",
        "ICLOUD_ARCHIVE_STARTED": (
            f"开始下载并归档 {details.get('assets', 0)} 项 "
            f"({details.get('resources', 0)} 个资源)"
        ),
        "ICLOUD_ARCHIVE_COMPLETED": f"iCloud 原件归档完成: {details.get('counts', {})}",
        "ICLOUD_CLEANUP_REVALIDATION_STARTED": "正在重新核对 iCloud 资产与外接盘证据",
        "DELETE_EVIDENCE_ASSET_STARTED": (
            f"[删除前核验 {details.get('current', 0)}/{details.get('total', 0)}] "
            f"资产 {details.get('asset_key', '')}"
        ),
        "ICLOUD_CLEANUP_READY": (
            f"iCloud 删除计划就绪: {details.get('assets', 0)} 个资产, "
            f"{details.get('resources', 0)} 个资源, "
            f"{_human_bytes(int(details.get('bytes', 0)))} 已归档"
        ),
        "ICLOUD_DELETE_STARTED": (
            f"正在通过 PhotoKit 删除 {details.get('assets', 0)} 个 iCloud Photos 资产"
        ),
        "ICLOUD_CLEANUP_COMPLETED": (
            f"iCloud 清理完成: 删除 {details.get('deleted', 0)} 个资产, "
            f"保留 {details.get('failed', 0)} 个资产"
        ),
    }
    message = messages.get(event)
    if message:
        timestamp = datetime.now().strftime("%H:%M:%S")
        typer.echo(f"[{timestamp}] {message}", err=True)


def _icloud_workflow(config: AppConfig, database: Database) -> ICloudWorkflow:
    return ICloudWorkflow(
        config,
        database,
        SwiftPhotoLibraryClient(config.media_helper.helper_path, config.icloud_cleanup),
        progress=_icloud_progress,
    )


def _device_json(device: Any) -> dict[str, Any]:
    return {
        "device_key": device.device_key[:12],
        "name": device.name,
        "product_kind": device.product_kind,
        "locked": device.locked,
        "trusted": device.trusted,
        "icloud_photos_enabled": device.icloud_photos_enabled,
        "can_delete": device.can_delete,
        "delete_capability_declared": device.delete_capability_declared,
        "can_accept_ptp_commands": device.can_accept_ptp_commands,
        "total_capacity_bytes": device.total_capacity_bytes,
        "used_capacity_bytes": device.used_capacity_bytes,
        "available_capacity_bytes": device.available_capacity_bytes,
        "total_capacity": _optional_human_bytes(device.total_capacity_bytes),
        "used_capacity": _optional_human_bytes(device.used_capacity_bytes),
        "available_capacity": _optional_human_bytes(device.available_capacity_bytes),
    }


def _cleanup_prompt(plan: CleanupPlan) -> str:
    message = (
        f"Delete {len(plan.asset_ids)} verified assets ({len(plan.resources)} files, "
        f"{plan.total_bytes} bytes) "
        f"older than {plan.cutoff_at_utc} from {plan.device.name}?"
    )
    if plan.device.icloud_photos_enabled:
        message += (
            " WARNING: iCloud Photos is enabled, so deletion also affects iCloud "
            "and other devices using the same Apple Account."
        )
    return message


def _icloud_cleanup_prompt(plan: ICloudCleanupPlan) -> str:
    selection = (
        f"with an original video larger than {plan.selection_threshold_bytes} bytes, regardless "
        "of creation date,"
        if plan.selection_mode == "large_video"
        else f"older than {plan.cutoff_at_utc}"
    )
    return (
        f"Delete {len(plan.assets)} verified assets ({plan.total_bytes} archived bytes) "
        f"{selection} from the ENTIRE iCloud Photos library? "
        "This affects every device using this Apple Account. Items move to Recently Deleted; "
        "permanently delete them there manually if you need the iCloud space immediately."
    )


def _icloud_group_cleanup_prompt(
    plans: tuple[ICloudCleanupPlan, ...], aggregate_sha256: str
) -> str:
    assets = sum(len(plan.assets) for plan in plans)
    total_bytes = sum(plan.total_bytes for plan in plans)
    batches = len({plan.batch_id for plan in plans})
    large_video_assets = sum(
        len(plan.assets) for plan in plans if plan.selection_mode == "large_video"
    )
    selection_note = (
        f" This includes {large_video_assets} any-date large-video assets."
        if large_video_assets
        else ""
    )
    return (
        f"Delete {assets} verified assets ({total_bytes} archived bytes) from "
        f"{batches} archive batches in {len(plans)} chunks? "
        f"Aggregate plan SHA-256: {aggregate_sha256}.{selection_note} "
        "This affects every device using this Apple Account. Items move to Recently Deleted."
    )


def _icloud_pipeline_prompt(scope: ICloudPipelineScope) -> str:
    policy = (
        f"new videos strictly larger than {scope.selection_threshold_bytes} bytes"
        if scope.selection_mode == "large_video"
        else f"new assets older than {scope.cutoff_at_utc}"
    )
    historical = (
        f" Historical large-video thresholds: {', '.join(map(str, scope.historical_thresholds))}."
        if scope.historical_thresholds
        else ""
    )
    return (
        f"Authorize this entire sync-all run for {scope.authorized_assets} frozen iCloud Photos "
        f"assets ({scope.ready_assets} already verified, {scope.resume_assets} in resumable "
        f"batches, {scope.new_assets} new; batch size {scope.batch_size}; {policy})? "
        f"Authorization SHA-256: {scope.authorization_sha256}.{historical} "
        "Each batch will be archived, fully revalidated, and deleted before the next batch. "
        "This affects every device using this Apple Account. Items move to Recently Deleted."
    )


def _lock_path(config: AppConfig) -> Path:
    return config.storage.database_path.with_suffix(config.storage.database_path.suffix + ".lock")


def _echo(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _fail(exc: Exception) -> None:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(code=1)


@db_app.command("migrate")
def migrate(ctx: typer.Context) -> None:
    """Apply verified database migrations."""
    try:
        config = _config(ctx)
        with FileLock(_lock_path(config)):
            applied = _database(config).migrate()
        _echo({"applied": applied, "status": "ok"})
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        _fail(exc)


@profile_app.command("add")
def profile_add(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Option("--id")],
    name: Annotated[str, typer.Option("--name")],
) -> None:
    """Create one explicit person profile."""
    try:
        config = _config(ctx)
        with FileLock(_lock_path(config)):
            root = BatchManager(config, _database(config)).add_profile(profile_id, name)
        _echo({"display_name": name.strip(), "inbox": str(root), "profile_id": profile_id})
    except (KeyError, OSError, RuntimeError, sqlite3.IntegrityError, ValueError) as exc:
        _fail(exc)


@profile_app.command("list")
def profile_list(ctx: typer.Context) -> None:
    """List configured person profiles."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        profiles = [
            {"display_name": row["display_name"], "profile_id": row["id"]}
            for row in database.list_profiles()
        ]
        _echo({"profiles": profiles})
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        _fail(exc)


@device_app.command("bind")
def device_bind(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
    replace: Annotated[bool, typer.Option("--replace")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Bind the one connected iPhone to a person profile."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _phone_workflow(config, database)
        with FileLock(_lock_path(config)):
            scan, _ = workflow.scan()
            if not yes and not typer.confirm(
                f"Bind {scan.device.name} ({scan.device.device_key[:12]}) to {profile}?"
            ):
                raise typer.Abort()
            binding = workflow.bind(profile, scan.device, replace=replace)
        _echo(
            {
                "device": _device_json(scan.device),
                "profile_id": profile,
                "binding_id": binding["id"],
                "status": "bound",
            }
        )
    except typer.Abort:
        raise
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@phone_app.command("scan")
def phone_scan(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    """Preview old media on the one connected iPhone without changing it."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _phone_workflow(config, database)
        workflow.bound_device(profile)
        with FileLock(_lock_path(config)):
            _, summary = workflow.scan()
        payload = asdict(summary)
        payload["device"] = _device_json(summary.device)
        binding = workflow.bound_device(profile)
        if binding is not None and binding["device_key"] != summary.device.device_key:
            raise PhotoArchiveError("connected phone is bound to a different profile device")
        payload["profile_id"] = profile
        payload["device_bound"] = binding is not None
        payload["candidate_capacity"] = _human_bytes(summary.candidate_bytes)
        payload["estimated_reclaimable_capacity"] = _human_bytes(summary.candidate_bytes)
        _echo(payload)
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


def _finish_phone_cleanup(
    workflow: PhoneWorkflow,
    batch_id: str,
    *,
    yes: bool,
) -> dict[str, Any]:
    def with_report(payload: dict[str, Any]) -> dict[str, Any]:
        batch = workflow.database.get_phone_batch(batch_id)
        if not batch["job_id"]:
            return payload
        json_path, csv_path = generate_report(
            workflow.database,
            str(batch["job_id"]),
            workflow.config.storage.reports_root / batch_id,
        )
        return {**payload, "report_json": str(json_path), "report_csv": str(csv_path)}

    session = workflow.client.open_session()
    try:
        try:
            plan = workflow.prepare_cleanup(batch_id, session)
        except PhoneCleanupComplete:
            return with_report({"batch_id": batch_id, "status": "completed", "reconciled": True})
        if not yes and not typer.confirm(_cleanup_prompt(plan)):
            return with_report(
                {
                    "batch_id": batch_id,
                    "plan_sha256": plan.plan_sha256,
                    "status": "ready_for_phone_cleanup",
                }
            )
        result = workflow.execute_cleanup(plan, session)
        return with_report(
            {
                "batch_id": batch_id,
                "plan_sha256": plan.plan_sha256,
                "status": (
                    "completed" if result["failed"] == 0 else "completed_with_items_remaining"
                ),
                **result,
            }
        )
    finally:
        session.close()


@phone_app.command("sync")
def phone_sync(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    """Import old media, verify its archive, then confirm phone cleanup once."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _phone_workflow(config, database)
        session = workflow.client.open_session()
        try:
            with FileLock(_lock_path(config)):
                scan, summary = workflow.scan(session)
                binding = workflow.bound_device(profile)
                if binding is None:
                    if not typer.confirm(
                        f"First use: bind {scan.device.name} "
                        f"({scan.device.device_key[:12]}) to {profile}?"
                    ):
                        raise typer.Abort()
                    workflow.bind(profile, scan.device)
                archived = workflow.archive_scan(profile, session, scan)
                plan = workflow.prepare_cleanup(archived.batch_id, session)
                if not typer.confirm(_cleanup_prompt(plan)):
                    report_json, report_csv = generate_report(
                        database,
                        archived.job_id,
                        config.storage.reports_root / archived.batch_id,
                    )
                    _echo(
                        {
                            "batch_id": archived.batch_id,
                            "job_id": archived.job_id,
                            "report_json": str(report_json),
                            "report_csv": str(report_csv),
                            "status": "ready_for_phone_cleanup",
                        }
                    )
                    return
                result = workflow.execute_cleanup(plan, session)
                report_json, report_csv = generate_report(
                    database,
                    archived.job_id,
                    config.storage.reports_root / archived.batch_id,
                )
            _echo(
                {
                    "batch_id": archived.batch_id,
                    "job_id": archived.job_id,
                    "completed": str(archived.completed_path),
                    "candidate_assets": summary.candidate_assets,
                    "candidate_bytes": summary.candidate_bytes,
                    "report_json": str(report_json),
                    "report_csv": str(report_csv),
                    **result,
                    "status": (
                        "completed" if result["failed"] == 0 else "completed_with_items_remaining"
                    ),
                }
            )
        finally:
            session.close()
    except typer.Abort:
        raise
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@phone_app.command("cleanup")
def phone_cleanup(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Option("--batch-id")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Revalidate and clean one previously verified phone batch."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _phone_workflow(config, database)
        with FileLock(_lock_path(config)):
            result = _finish_phone_cleanup(workflow, batch_id, yes=yes)
        _echo(result)
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@phone_app.command("resume")
def phone_resume(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Option("--batch-id")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Resume phone import, archive verification, or cleanup."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _phone_workflow(config, database)
        with FileLock(_lock_path(config)):
            batch = database.get_phone_batch(batch_id)
            items = database.list_phone_items(batch_id)
            missing_archives = any(
                row["status"] == "FAILED" and row["resource_id"] is None for row in items
            )
            legacy_relationship_skips = any(
                row["status"] == "SKIPPED" and row["error_code"] == "INCOMPLETE_RELATED_ASSET"
                for row in items
            )
            if (
                missing_archives
                or legacy_relationship_skips
                or batch["state"]
                not in {
                    "READY_FOR_PHONE_CLEANUP",
                    "COMPLETED_WITH_PHONE_ITEMS_REMAINING",
                    "CLEANING_PHONE",
                }
            ):
                session = workflow.client.open_session()
                try:
                    workflow.resume_archive(batch_id, session)
                finally:
                    session.close()
            result = _finish_phone_cleanup(workflow, batch_id, yes=yes)
        _echo(result)
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


def _icloud_report(
    workflow: ICloudWorkflow, batch_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    batch = workflow.database.get_icloud_batch(batch_id)
    json_path, csv_path = generate_report(
        workflow.database,
        str(batch["job_id"]),
        workflow.config.storage.reports_root / batch_id,
    )
    return {**payload, "report_json": str(json_path), "report_csv": str(csv_path)}


def _finish_icloud_cleanup(
    workflow: ICloudWorkflow,
    batch_id: str,
    *,
    yes: bool,
) -> dict[str, Any]:
    session = workflow.client.open_session()
    try:
        try:
            plan = workflow.prepare_cleanup(batch_id, session)
        except ICloudCleanupComplete:
            return _icloud_report(
                workflow,
                batch_id,
                {
                    "batch_id": batch_id,
                    "status": "completed",
                    "reconciled": True,
                    "recently_deleted_action_required": True,
                },
            )
        chunks = workflow.split_cleanup_plan(plan)
        if len(chunks) != 1:
            raise PhoneSafetyError(
                "this legacy batch exceeds the configured iCloud batch size; "
                "use 'icloud cleanup-ready --profile <profile>' for bounded deletion"
            )
        plan = chunks[0]
        if not yes and not typer.confirm(_icloud_cleanup_prompt(plan)):
            return _icloud_report(
                workflow,
                batch_id,
                {
                    "batch_id": batch_id,
                    "plan_sha256": plan.plan_sha256,
                    "status": "ready_for_icloud_cleanup",
                },
            )
        result = workflow.execute_cleanup(plan, session)
        return _icloud_report(
            workflow,
            batch_id,
            {
                "batch_id": batch_id,
                "plan_sha256": plan.plan_sha256,
                "status": (
                    "completed" if result["failed"] == 0 else "completed_with_items_remaining"
                ),
                "recently_deleted_action_required": result["deleted"] > 0,
                **result,
            },
        )
    finally:
        session.close()


@icloud_app.command("scan")
def icloud_scan(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    """Read the full system Photos library without downloading originals."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _icloud_workflow(config, database)
        with FileLock(_lock_path(config)):
            _, summary = workflow.scan(profile)
        payload = asdict(summary)
        payload["profile_id"] = profile
        payload["candidate_capacity"] = "需在下载原件后确定"
        payload["scope"] = "system_photos_library_including_icloud"
        _echo(payload)
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


def _run_icloud_sync_all(
    ctx: typer.Context,
    profile: str,
    *,
    selection_mode: str,
) -> None:
    config = _config(ctx)
    if not config.icloud_cleanup.enabled:
        raise PhoneSafetyError("iCloud cleanup is disabled in configuration")
    ExternalDriveClient(config.external_drive).assert_ready()
    database = _database(config)
    database.migrate()
    workflow = _icloud_workflow(config, database)
    session = workflow.client.open_session()
    try:
        with FileLock(_lock_path(config)):
            if selection_mode == "large_video":
                scan, large_video_summary = workflow.scan_large_videos(profile, session)
                selected = workflow.select_large_videos(profile, session, scan)
                threshold = large_video_summary.threshold_bytes
            else:
                selected, _ = workflow.scan(profile, session)
                threshold = None
            scope = workflow.build_pipeline_scope(
                profile,
                selected,
                selection_mode=selection_mode,
                selection_threshold_bytes=threshold,
            )
            if not scope.authorized_assets:
                _echo(
                    {
                        "authorization_sha256": scope.authorization_sha256,
                        "candidate_assets": 0,
                        "profile_id": profile,
                        "selection_mode": selection_mode,
                        "status": "no_pipeline_candidates",
                    }
                )
                return
            if not typer.confirm(_icloud_pipeline_prompt(scope)):
                _echo(
                    {
                        "authorization_sha256": scope.authorization_sha256,
                        "authorized_assets": scope.authorized_assets,
                        "profile_id": profile,
                        "selection_mode": selection_mode,
                        "status": "cancelled",
                    }
                )
                return

            reports: dict[str, dict[str, str]] = {}

            def write_batch_report(batch_id: str) -> None:
                batch = workflow.database.get_icloud_batch(batch_id)
                report = _icloud_report(
                    workflow,
                    batch_id,
                    {
                        "authorization_sha256": scope.authorization_sha256,
                        "batch_id": batch_id,
                        "selection_mode": str(batch["selection_mode"]),
                        "status": str(batch["state"]).lower(),
                    },
                )
                reports[batch_id] = {
                    "batch_id": batch_id,
                    "report_csv": str(report["report_csv"]),
                    "report_json": str(report["report_json"]),
                }

            result = workflow.execute_pipeline(
                scope,
                session,
                batch_completed=write_batch_report,
            )
        _echo(
            {
                "archived_assets": result.archived_assets,
                "authorization_sha256": scope.authorization_sha256,
                "authorized_assets": scope.authorized_assets,
                "batch_ids": list(result.batch_ids),
                "batch_size": scope.batch_size,
                "completed_chunks": result.completed_chunks,
                "deleted": result.deleted,
                "failed": result.failed,
                "profile_id": profile,
                "ready_assets": scope.ready_assets,
                "recently_deleted_action_required": result.deleted > 0,
                "reports": list(reports.values()),
                "resume_assets": scope.resume_assets,
                "selection_mode": selection_mode,
                "status": "completed",
            }
        )
    finally:
        session.close()


@icloud_app.command("sync-all")
def icloud_sync_all(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    """Confirm once, then archive, verify, and delete every bounded old-photo batch."""
    try:
        _run_icloud_sync_all(ctx, profile, selection_mode="age_cutoff")
    except typer.Abort:
        raise
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@icloud_app.command("sync")
def icloud_sync(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    """Download old iCloud originals, verify them, then ask once before deletion."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _icloud_workflow(config, database)
        session = workflow.client.open_session()
        try:
            with FileLock(_lock_path(config)):
                scan, summary = workflow.scan(profile, session)
                remaining = workflow.unarchived_scan(profile, scan)
                if not remaining.assets:
                    _echo(
                        {
                            "candidate_assets": 0,
                            "cutoff_at_utc": summary.cutoff_at_utc,
                            "profile_id": profile,
                            "status": "no_unarchived_candidates",
                        }
                    )
                    return
                archived = workflow.archive_scan(profile, session, remaining)
                plan = workflow.prepare_cleanup(archived.batch_id, session)
                if not typer.confirm(_icloud_cleanup_prompt(plan)):
                    _echo(
                        _icloud_report(
                            workflow,
                            archived.batch_id,
                            {
                                "batch_id": archived.batch_id,
                                "job_id": archived.job_id,
                                "plan_sha256": plan.plan_sha256,
                                "status": "ready_for_icloud_cleanup",
                            },
                        )
                    )
                    return
                result = workflow.execute_cleanup(plan, session)
                payload = _icloud_report(
                    workflow,
                    archived.batch_id,
                    {
                        "batch_id": archived.batch_id,
                        "job_id": archived.job_id,
                        "candidate_assets": sum(archived.counts.values()),
                        "batch_size": config.icloud_cleanup.batch_size,
                        "archived_bytes": plan.total_bytes,
                        "plan_sha256": plan.plan_sha256,
                        "recently_deleted_action_required": result["deleted"] > 0,
                        "status": (
                            "completed"
                            if result["failed"] == 0
                            else "completed_with_items_remaining"
                        ),
                        **result,
                    },
                )
            _echo(payload)
        finally:
            session.close()
    except typer.Abort:
        raise
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@icloud_app.command("archive-night")
def icloud_archive_night(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    """Archive all remaining candidates in small batches without deleting Photos."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _icloud_workflow(config, database)
        session = workflow.client.open_session()
        try:
            with FileLock(_lock_path(config)):
                scan, summary = workflow.scan(profile, session)
                remaining = workflow.unarchived_scan(profile, scan)
                if not remaining.assets:
                    _echo(
                        {
                            "candidate_assets": 0,
                            "profile_id": profile,
                            "status": "no_unarchived_candidates",
                        }
                    )
                    return
                archived = workflow.archive_all_batches(profile, session, remaining)
            _echo(
                {
                    "archived_assets": sum(sum(item.counts.values()) for item in archived),
                    "batch_ids": [item.batch_id for item in archived],
                    "batch_size": config.icloud_cleanup.batch_size,
                    "cutoff_at_utc": summary.cutoff_at_utc,
                    "deletion_performed": False,
                    "profile_id": profile,
                    "status": "ready_for_group_cleanup",
                }
            )
        finally:
            session.close()
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@large_video_app.command("scan")
def icloud_large_video_scan(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    """List video metadata and cached size classifications without downloading originals."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _icloud_workflow(config, database)
        with FileLock(_lock_path(config)):
            _, summary = workflow.scan_large_videos(profile)
        payload = asdict(summary)
        payload.update(
            {
                "profile_id": profile,
                "scope": "system_photos_library_any_date_original_videos",
                "measurement_performed": False,
            }
        )
        _echo(payload)
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@large_video_app.command("sync")
def icloud_large_video_sync(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    """Measure and archive one batch of any-date large videos, then confirm deletion."""
    try:
        config = _config(ctx)
        if not config.icloud_cleanup.enabled:
            raise PhoneSafetyError("iCloud cleanup is disabled in configuration")
        ExternalDriveClient(config.external_drive).assert_ready()
        database = _database(config)
        database.migrate()
        workflow = _icloud_workflow(config, database)
        session = workflow.client.open_session()
        try:
            with FileLock(_lock_path(config)):
                scan, summary = workflow.scan_large_videos(profile, session)
                selected = workflow.select_large_videos(
                    profile,
                    session,
                    scan,
                    limit=config.icloud_cleanup.batch_size,
                )
                if not selected.assets:
                    _echo(
                        {
                            "candidate_assets": 0,
                            "profile_id": profile,
                            "status": "no_unarchived_large_videos",
                            "threshold_bytes": summary.threshold_bytes,
                        }
                    )
                    return
                archived = workflow.archive_scan(
                    profile,
                    session,
                    selected,
                    candidate_limit=len(selected.assets),
                    selection_mode="large_video",
                    selection_threshold_bytes=summary.threshold_bytes,
                )
                plan = workflow.prepare_cleanup(archived.batch_id, session)
                if not typer.confirm(_icloud_cleanup_prompt(plan)):
                    _echo(
                        _icloud_report(
                            workflow,
                            archived.batch_id,
                            {
                                "batch_id": archived.batch_id,
                                "job_id": archived.job_id,
                                "plan_sha256": plan.plan_sha256,
                                "selection_mode": plan.selection_mode,
                                "status": "ready_for_icloud_cleanup",
                                "threshold_bytes": plan.selection_threshold_bytes,
                            },
                        )
                    )
                    return
                result = workflow.execute_cleanup(plan, session)
                payload = _icloud_report(
                    workflow,
                    archived.batch_id,
                    {
                        "batch_id": archived.batch_id,
                        "job_id": archived.job_id,
                        "candidate_assets": len(selected.assets),
                        "archived_bytes": plan.total_bytes,
                        "plan_sha256": plan.plan_sha256,
                        "selection_mode": plan.selection_mode,
                        "threshold_bytes": plan.selection_threshold_bytes,
                        "recently_deleted_action_required": result["deleted"] > 0,
                        "status": (
                            "completed"
                            if result["failed"] == 0
                            else "completed_with_items_remaining"
                        ),
                        **result,
                    },
                )
            _echo(payload)
        finally:
            session.close()
    except typer.Abort:
        raise
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@large_video_app.command("sync-all")
def icloud_large_video_sync_all(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    """Confirm once, then archive, verify, and delete every bounded large-video batch."""
    try:
        _run_icloud_sync_all(ctx, profile, selection_mode="large_video")
    except typer.Abort:
        raise
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@large_video_app.command("archive-night")
def icloud_large_video_archive_night(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    """Measure and archive all any-date large videos without deleting Photos assets."""
    try:
        config = _config(ctx)
        if not config.icloud_cleanup.enabled:
            raise PhoneSafetyError("iCloud cleanup is disabled in configuration")
        ExternalDriveClient(config.external_drive).assert_ready()
        database = _database(config)
        database.migrate()
        workflow = _icloud_workflow(config, database)
        session = workflow.client.open_session()
        try:
            with FileLock(_lock_path(config)):
                scan, summary = workflow.scan_large_videos(profile, session)
                selected = workflow.select_large_videos(profile, session, scan)
                if not selected.assets:
                    _echo(
                        {
                            "candidate_assets": 0,
                            "profile_id": profile,
                            "status": "no_unarchived_large_videos",
                            "threshold_bytes": summary.threshold_bytes,
                        }
                    )
                    return
                archived = workflow.archive_all_batches(
                    profile,
                    session,
                    selected,
                    selection_mode="large_video",
                    selection_threshold_bytes=summary.threshold_bytes,
                )
            _echo(
                {
                    "archived_assets": sum(sum(item.counts.values()) for item in archived),
                    "batch_ids": [item.batch_id for item in archived],
                    "batch_size": config.icloud_cleanup.batch_size,
                    "deletion_performed": False,
                    "profile_id": profile,
                    "selection_mode": "large_video",
                    "status": "ready_for_group_cleanup",
                    "threshold_bytes": summary.threshold_bytes,
                }
            )
        finally:
            session.close()
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@icloud_app.command("cleanup-ready")
def icloud_cleanup_ready(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
) -> None:
    """Confirm once, then delete all verified batches in bounded PhotoKit chunks."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _icloud_workflow(config, database)
        session = workflow.client.open_session()
        try:
            with FileLock(_lock_path(config)):
                plans = workflow.preview_cleanup_plans(profile)
                if not plans:
                    _echo({"profile_id": profile, "status": "no_verified_assets_ready"})
                    return
                index = [
                    {
                        "assets": len(plan.assets),
                        "batch_id": plan.batch_id,
                        "bytes": plan.total_bytes,
                        "plan_sha256": plan.plan_sha256,
                        "selection_mode": plan.selection_mode,
                        "selection_threshold_bytes": plan.selection_threshold_bytes,
                    }
                    for plan in plans
                ]
                aggregate_sha256 = hashlib.sha256(
                    json.dumps(index, separators=(",", ":"), sort_keys=True).encode()
                ).hexdigest()
                grouped = tuple(plans)
                if not typer.confirm(
                    _icloud_group_cleanup_prompt(grouped, aggregate_sha256)
                ):
                    _echo(
                        {
                            "aggregate_plan_sha256": aggregate_sha256,
                            "assets": sum(len(plan.assets) for plan in grouped),
                            "chunks": len(grouped),
                            "large_video_assets": sum(
                                len(plan.assets)
                                for plan in grouped
                                if plan.selection_mode == "large_video"
                            ),
                            "profile_id": profile,
                            "status": "ready_for_group_cleanup",
                        }
                    )
                    return
                deleted = 0
                failed = 0
                completed_chunks = 0
                for expected_plan in grouped:
                    identifiers = frozenset(
                        asset.local_identifier for asset in expected_plan.assets
                    )
                    plan = workflow.prepare_cleanup(
                        expected_plan.batch_id,
                        session,
                        local_identifiers=identifiers,
                    )
                    if plan.plan_sha256 != expected_plan.plan_sha256:
                        raise PhoneSafetyError(
                            "iCloud deletion plan changed during chunk verification"
                        )
                    result = workflow.execute_cleanup(plan, session)
                    deleted += result["deleted"]
                    failed += result["failed"]
                    completed_chunks += 1
                    if result["failed"]:
                        break
            _echo(
                {
                    "aggregate_plan_sha256": aggregate_sha256,
                    "completed_chunks": completed_chunks,
                    "deleted": deleted,
                    "failed": failed,
                    "large_video_assets": sum(
                        len(plan.assets)
                        for plan in grouped
                        if plan.selection_mode == "large_video"
                    ),
                    "profile_id": profile,
                    "recently_deleted_action_required": deleted > 0,
                    "status": "completed" if failed == 0 else "stopped_after_failure",
                    "total_chunks": len(grouped),
                }
            )
        finally:
            session.close()
    except typer.Abort:
        raise
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@icloud_app.command("cleanup")
def icloud_cleanup(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Option("--batch-id")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Revalidate and delete one previously verified iCloud Photos batch."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _icloud_workflow(config, database)
        with FileLock(_lock_path(config)):
            result = _finish_icloud_cleanup(workflow, batch_id, yes=yes)
        _echo(result)
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@icloud_app.command("resume")
def icloud_resume(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Option("--batch-id")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Resume an interrupted iCloud download, verification, or cleanup."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        workflow = _icloud_workflow(config, database)
        with FileLock(_lock_path(config)):
            batch = database.get_icloud_batch(batch_id)
            counts = database.state_counts(str(batch["job_id"]))
            incomplete = sum(
                count for state, count in counts.items() if state != "SAFE_TO_DELETE"
            )
            if incomplete:
                if sum(counts.values()) > config.icloud_cleanup.batch_size:
                    raise PhoneSafetyError(
                        "legacy iCloud batch is larger than the configured batch size; "
                        "use cleanup-ready for its verified assets and archive-night "
                        "for the remaining library"
                    )
                session = workflow.client.open_session()
                try:
                    workflow.resume_archive(batch_id, session)
                finally:
                    session.close()
            result = _finish_icloud_cleanup(workflow, batch_id, yes=yes)
        _echo(result)
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@batch_app.command("prepare")
def batch_prepare(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
    no_open: Annotated[bool, typer.Option("--no-open")] = False,
) -> None:
    """Create an empty batch folder and open Image Capture."""
    try:
        config = _config(ctx)
        with FileLock(_lock_path(config)):
            batch_id, destination = BatchManager(config, _database(config)).prepare(
                profile, open_image_capture=False if no_open else None
            )
        _echo(
            {
                "batch_id": batch_id,
                "destination": str(destination),
                "next": (
                    "Select this folder in Image Capture, then run: "
                    f"photoarchive batch archive --batch-id {batch_id}"
                ),
                "status": "ready_for_import",
            }
        )
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError) as exc:
        _fail(exc)


@batch_app.command("status")
def batch_status(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Option("--batch-id")],
) -> None:
    """Show one batch without reading media content."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        batch = database.get_batch(batch_id)
        counts = database.state_counts(batch["job_id"]) if batch["job_id"] else {}
        display_counts = {
            ("VERIFIED_FOR_FINALIZE" if state == "SAFE_TO_DELETE" else state): count
            for state, count in counts.items()
        }
        _echo(
            {
                "batch_id": batch_id,
                "job_id": batch["job_id"],
                "profile_id": batch["profile_id"],
                "state": batch["state"],
                "asset_counts": display_counts,
                "error_code": batch["error_code"],
            }
        )
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError) as exc:
        _fail(exc)


@batch_app.command("list")
def batch_list(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
) -> None:
    """List recent import batches."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        batches = [
            {
                "batch_id": row["id"],
                "job_id": row["job_id"],
                "profile_id": row["profile_id"],
                "state": row["state"],
            }
            for row in database.list_batches(profile)
        ]
        _echo({"batches": batches})
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        _fail(exc)


@app.command()
def doctor(
    ctx: typer.Context,
    adapter: Annotated[str, typer.Option(help="fixture, folder, phone, or icloud")] = "folder",
) -> None:
    """Check prerequisites without opening media files."""
    try:
        if adapter not in {"fixture", "folder", "phone", "icloud"}:
            raise ValueError("adapter must be fixture, folder, phone, or icloud")
        config = _config(ctx)
        lock_status = "available"
        try:
            with FileLock(_lock_path(config)):
                pass
        except LockUnavailableError:
            lock_status = "busy"
        database_status = "not_created"
        if config.storage.database_path.exists():
            connection = sqlite3.connect(f"file:{config.storage.database_path}?mode=ro", uri=True)
            try:
                database_status = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                connection.close()
        nearest = config.storage.staging_root
        while not nearest.exists() and nearest != nearest.parent:
            nearest = nearest.parent
        free_bytes = shutil.disk_usage(nearest).free
        helper_status = "not_required"
        drive_status: dict[str, Any] | None = None
        if adapter in {"folder", "phone", "icloud"}:
            helper_root = Path("native/photos-helper").resolve()
            build = subprocess.run(
                [str(helper_root / "scripts" / "build_app.sh")],
                capture_output=True,
                check=False,
                text=True,
                timeout=120,
            )
            helper_status = "ok" if build.returncode == 0 else "failed"
            drive_status = ExternalDriveClient(config.external_drive).inspect().as_dict()
            if adapter == "folder":
                BatchManager(config, _database(config)).root.mkdir(
                    mode=0o700, parents=True, exist_ok=True
                )
        healthy = lock_status == "available" and helper_status != "failed"
        if drive_status is not None:
            healthy = healthy and bool(
                drive_status["mounted"]
                and drive_status["writable"]
                and drive_status["identity_matches"]
            )
        _echo(
            {
                "adapter": adapter,
                "database": database_status,
                "external_drive": drive_status,
                "free_bytes": free_bytes,
                "helper_build": helper_status,
                "lock": lock_status,
                "connected_device_permission_required": adapter == "phone",
                "phone_cleanup_enabled": config.phone_cleanup.enabled,
                "photos_full_access_required": adapter == "icloud",
                "icloud_cleanup_enabled": config.icloud_cleanup.enabled,
                "status": "ok" if healthy else "warning",
            }
        )
    except (OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


def _source_for_plan(
    config: AppConfig,
    database: Database,
    adapter: str,
    fixture: Path | None,
    batch_id: str | None,
) -> tuple[ArchiveSource, str]:
    if adapter == "fixture":
        if fixture is None:
            raise ValueError("--fixture is required with --adapter fixture")
        return FixturePhotosClient(fixture), "fake"
    if adapter == "folder":
        if fixture is not None or batch_id is None:
            raise ValueError("--batch-id is required and --fixture is not allowed with folder")
        return BatchManager(config, database).source(batch_id), "external"
    raise ValueError("adapter must be fixture or folder")


@app.command("plan")
def plan_command(
    ctx: typer.Context,
    fixture: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, readable=True)
    ] = None,
    adapter: Annotated[str, typer.Option(help="fixture or folder")] = "fixture",
    batch_id: Annotated[str | None, typer.Option("--batch-id")] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    create_job: Annotated[bool, typer.Option("--create-job")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Preview files or create an immutable archive job."""
    try:
        if dry_run == create_job:
            raise ValueError("choose exactly one of --dry-run or --create-job")
        config = _config(ctx)
        database = _database(config)
        source, target = _source_for_plan(config, database, adapter, fixture, batch_id)
        planner = Planner(config, database, source, candidate_limit=limit, target_adapter=target)
        if dry_run:
            _echo(asdict(planner.preview()))
            return
        if not yes and not typer.confirm("Create this archive job?"):
            raise typer.Abort()
        with FileLock(_lock_path(config)):
            summary = planner.create_job()
            if batch_id and summary.job_id:
                database.attach_batch_job(batch_id, summary.job_id)
        _echo(asdict(summary))
    except typer.Abort:
        raise
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


def _runner_for_job(config: AppConfig, database: Database, job_id: str) -> ArchiveRunner:
    job = database.get_job(job_id)
    source_name = job["source_adapter"] or job["photos_adapter"]
    target_name = job["target_adapter"] or job["onedrive_adapter"]
    source: ArchiveSource
    target: ArchiveTarget
    if source_name == "fixture" and target_name == "fake":
        source = FixturePhotosClient(Path(job["fixture_path"]))
        target = FakeArchiveTarget(config.storage.fake_archive_root)
    elif source_name == "folder" and target_name == "external" and job["batch_id"]:
        source = BatchManager(config, database).source(str(job["batch_id"]))
        target = ExternalDriveClient(config.external_drive)
    else:
        raise ValueError("LEGACY_ADAPTER_UNSUPPORTED: this job can only be reported")
    return ArchiveRunner(config, database, source, target)


def _run(ctx: typer.Context, job_id: str, yes: bool, *, resume: bool) -> None:
    try:
        if not yes and not typer.confirm("Copy and verify pending files?"):
            raise typer.Abort()
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        with FileLock(_lock_path(config)):
            counts = _runner_for_job(config, database, job_id).run_job(job_id, resume=resume)
        _echo({"counts": counts, "job_id": job_id})
    except typer.Abort:
        raise
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@app.command("run")
def run_command(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Option("--job-id")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Run pending archive stages."""
    _run(ctx, job_id, yes, resume=False)


@app.command("resume")
def resume_command(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Option("--job-id")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Reconcile and continue a failed job."""
    _run(ctx, job_id, yes, resume=True)


@app.command("verify")
def verify_command(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Option("--job-id")],
) -> None:
    """Recheck archived files."""
    try:
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        with FileLock(_lock_path(config)):
            counts = _runner_for_job(config, database, job_id).verify_job(job_id)
            job = database.get_job(job_id)
            review_count = database.review_count(job_id)
            if job["batch_id"]:
                batch_id = str(job["batch_id"])
                if review_count:
                    database.set_batch_state(
                        batch_id, "NEEDS_ATTENTION", error_code="VERIFY_FAILED"
                    )
                elif set(counts) == {"SAFE_TO_DELETE"}:
                    manager = BatchManager(config, database)
                    batch = database.get_batch(batch_id)
                    completed = safe_path(manager.root, str(batch["completed_relative_path"]))
                    if completed.is_dir():
                        database.set_batch_state(batch_id, "COMPLETED")
        _echo({"counts": counts, "job_id": job_id, "review_required": review_count})
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        _fail(exc)


@app.command("report")
def report_command(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Option("--job-id")],
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Write an atomic JSON and CSV archive report."""
    try:
        config = _config(ctx)
        destination = output or config.storage.reports_root / job_id
        json_path, csv_path = generate_report(_database(config), job_id, destination)
        _echo({"csv": str(csv_path), "json": str(json_path), "job_id": job_id})
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError) as exc:
        _fail(exc)


@batch_app.command("archive")
def batch_archive(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Option("--batch-id")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Archive, verify, report, and finalize one complete import batch."""
    database: Database | None = None
    try:
        if not yes and not typer.confirm(
            "Archive every file in this batch and move it to completed after verification?"
        ):
            raise typer.Abort()
        config = _config(ctx)
        database = _database(config)
        database.migrate()
        manager = BatchManager(config, database)
        with FileLock(_lock_path(config)):
            batch = database.get_batch(batch_id)
            job_id = batch["job_id"]
            if batch["state"] == "COMPLETED" and job_id:
                completed = manager.finalize(batch_id)
            else:
                if not job_id:
                    source = manager.source(batch_id)
                    summary = Planner(
                        config, database, source, target_adapter="external"
                    ).create_job()
                    if summary.job_id is None:
                        raise RuntimeError("archive job was not created")
                    job_id = summary.job_id
                    database.attach_batch_job(batch_id, job_id)
                else:
                    Planner(
                        config,
                        database,
                        manager.source(batch_id),
                        target_adapter="external",
                    ).refresh_job(str(job_id))
                counts = _runner_for_job(config, database, str(job_id)).run_job(
                    str(job_id), resume=True
                )
                if set(counts) != {"SAFE_TO_DELETE"}:
                    database.set_batch_state(
                        batch_id, "NEEDS_ATTENTION", error_code="VERIFY_FAILED"
                    )
                    raise PhotoArchiveError(
                        "one or more files failed; the batch remains in pending"
                    )
                completed = manager.finalize(batch_id)
            report_json, report_csv = generate_report(
                database, str(job_id), config.storage.reports_root / batch_id
            )
        _echo(
            {
                "batch_id": batch_id,
                "completed": str(completed),
                "job_id": job_id,
                "report_csv": str(report_csv),
                "report_json": str(report_json),
                "status": "completed",
            }
        )
    except typer.Abort:
        raise
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, PhotoArchiveError) as exc:
        if database is not None:
            try:
                batch = database.get_batch(batch_id)
                if batch["state"] != "COMPLETED":
                    code = getattr(exc, "code", type(exc).__name__.upper())
                    database.set_batch_state(batch_id, "NEEDS_ATTENTION", error_code=str(code))
            except (KeyError, OSError, sqlite3.DatabaseError):
                pass
        _fail(exc)
