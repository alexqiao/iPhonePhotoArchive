# PhotoArchive 开发交接

更新时间：2026-09-06（Asia/Shanghai）

## 项目目标

这是一个 macOS 14+ 的照片归档程序。程序可以从当前连接并绑定到人员档案的 iPhone，或从 Mac 系统照片图库中的 iCloud Photos，选择严格早于两年截止时间的照片和视频，再归档到名为 `ExternalDisk01` 的外接盘。只有全部必要资源通过大小、SHA-256、QuickXorHash、manifest 和两次稳定性验证后，资产才进入对应来源的清理队列。

手机/PTP 与系统“照片”图库使用完全独立的批次和资产身份。不同人员使用独立 profile。任何会同步影响 iCloud 和同一 Apple 账户其他设备的删除都必须经过明确人工确认。

项目不使用 OneDrive 或自建网络 API；仅 iCloud 工作流允许 PhotoKit 按需从 iCloud 下载资源。项目不清空“最近删除”，不使用 AppleScript、UI 自动点击或私有 Apple API。

## 当前技术结构

- Python 3.12：CLI、配置、SQLite、归档状态机、哈希、验证、报告和手机工作流。
- Swift 6.3：通过 ImageCaptureCore 枚举、下载、复核和删除 iPhone 媒体。
- Swift PhotoKit：枚举系统用户图库、按需下载 iCloud 资源、复核和删除已确认资产。
- SQLite：`var/photoarchive.sqlite3`；代码已包含 `008_icloud_large_videos.sql`，实机运行新命令前需执行迁移。
- 外接盘：由 `config/local.yaml` 中的卷名和卷 UUID 双重绑定。
- Swift helper：`native/photos-helper/build/PhotoArchiveMediaHelper.app`。
- 当前应用和 helper 版本：`0.6.0`；手机协议为 JSONL v2，PhotoKit 协议为 JSONL v4。
- 原始需求 DOCX 保留在项目根目录。
- 当前目录是 Git 仓库，当前分支为 `main`；本轮修改尚未提交。

关键代码：

- `src/photoarchive/phone_workflow.py`：手机扫描、导入、归档、复核和清理编排。
- `src/photoarchive/phone_client.py`：Python 与 Swift helper 的 JSONL v2 会话。
- `src/photoarchive/icloud_client.py`：Python 与 Swift helper 的 PhotoKit JSONL v3 会话。
- `src/photoarchive/icloud_source.py`：把 PhotoKit 资源接入通用归档状态机。
- `src/photoarchive/icloud_workflow.py`：iCloud 扫描、归档、复核与清理编排。
- `src/photoarchive/database.py`：手机批次、跨批次复用和删除错误持久化。
- `src/photoarchive/pipeline.py`：归档、外接盘复制和稳定性验证。
- `src/photoarchive/cli.py`：中文进度日志和 CLI。
- `native/photos-helper/Sources/PhotosHelperCore/PhoneDeviceAdapter.swift`：ImageCaptureCore/PTP 手机适配器。
- `native/photos-helper/Sources/PhotosHelperCore/PhotoLibraryAdapter.swift`：系统照片图库适配器。
- `src/photoarchive/migrations/006_phone_dedup_delete_diagnostics.sql`：去重索引和删除错误详情字段。
- `src/photoarchive/migrations/007_icloud_photos.sql`：独立 iCloud 批次、资产、资源和清理状态。

## 已完成能力

### 不限时间的大视频迁移

新增 `icloud large-video scan|sync|sync-all|archive-night`。该流程只针对系统照片图库中的普通
视频资产，以 PhotoKit `.video` 原始资源严格大于 100 MiB 为条件，不限制拍摄时间，
并归档入选资产的全部关联资源。`scan` 不下载原件；`sync` 和 `archive-night` 使用公开
PhotoKit 数据流按需测量并缓存结果。大视频批次会固化选择模式和阈值，清理仍复用现有
人工确认、外接盘证据复核和“最近删除”机制。

### iCloud Photos 扫描、归档与清理

新增独立命令：

```bash
.venv/bin/photoarchive --config config/local.yaml icloud scan --profile wife
.venv/bin/photoarchive --config config/local.yaml icloud sync --profile wife
.venv/bin/photoarchive --config config/local.yaml icloud sync-all --profile wife
.venv/bin/photoarchive --config config/local.yaml icloud archive-night --profile wife
.venv/bin/photoarchive --config config/local.yaml icloud cleanup-ready --profile wife
.venv/bin/photoarchive --config config/local.yaml icloud cleanup --batch-id '<batch_id>'
.venv/bin/photoarchive --config config/local.yaml icloud resume --batch-id '<batch_id>'
```

2026-09-06 增加 iCloud 小批次模式：白天 `icloud sync` 每批最多 1000 个资产，
归档、复核并确认后删除这一批；夜间 `archive-night` 可连续归档和验证但绝不删除；
`cleanup-ready` 为全部已验证资产生成分块计划及汇总 SHA-256，人工确认一次后，
逐批完成删除前复核，再把兼容计划合并为一次 PhotoKit 删除事务。删除响应最长等待 24 小时，
便于夜间完成复核后将系统弹窗保留到第二天处理。
已验证的 PhotoKit local identifier 会跨批次排除，避免夜间重复下载。旧的大批次
不会被 `resume` 意外继续归档。

新增 `icloud sync-all` 完整流水线：初始扫描冻结候选集合并生成授权范围 SHA-256，用户
确认一次后，程序恢复不超过批次上限的中断任务，并将新候选按最多 1000 项逐批归档。
全部归档后再逐批复核外接盘证据和 PhotoKit 身份，最后合并为一个 PhotoKit 删除事务，
macOS 因此只收到一次系统删除请求。任何不完整归档、资产变化、计划漂移或删除失败都会
停止；重新运行必须重新扫描并确认。`icloud large-video sync-all` 提供相同流程，并保留
历史批次各自固化的大小阈值。两个命令的首个归档批次默认 50 项，后续恢复
`icloud_cleanup.batch_size`，也可通过 `--first-batch-size` 调整。

`icloud scan` 使用公开 PhotoKit API 枚举系统用户图库，包括云端占位资产、隐藏资产和完整连拍，不下载原件。PhotoKit 没有公开的云端资源大小元数据，所以扫描阶段只报告资产数与资源数，容量在下载归档后才确定。

`icloud sync` 会按需联网下载 `PHAssetResource` 暴露的全部底层资源，沿用现有外接盘哈希、manifest 与两次稳定性验证。每个资产验证成功后会删除程序自己的 staging 临时副本。删除前再次获取精确 `localIdentifier`、检查截止时间和完整资源键集合，并把全部外接盘证据纳入 SHA-256 删除计划。PhotoKit 删除成功后，资产进入系统“最近删除”；程序不清空它，用户若需立即释放空间必须在“照片”App 手动永久删除。

删除前复核采用一次完整的 SHA-256、QuickXorHash、大小和路径检查；初次归档仍保留两次稳定性检查。`cleanup-ready` 先为所有待删资产生成分块计划并让用户确认一次汇总摘要，随后逐批复核全部候选，只有全部计划兼容且通过复核后才发出一次合并删除请求。

该能力已通过 fake PhotoKit session 的非破坏性测试；真实系统图库授权、扫描、iCloud 原件下载和外接盘归档已经实际运行。PhotoKit 破坏性删除尚未确认执行，因此不能宣称真实 iCloud 删除验收完成。

#### 当前实机状态与账号边界

当前项目已安装在照片所有者的独立 macOS 用户下，该用户登录对应的 Apple Account，系统照片图库和 iCloud Photos 已同步，helper 已获得“完整访问”。PhotoKit 仍始终访问当前 macOS 用户的系统照片图库，不能在程序中选择 Apple Account。

旧的大批次 `icloud-20260906-081545-775111c3` 在中断归档时已有 2400 项进入 `SAFE_TO_DELETE`，19700 项仍为 `DISCOVERED`，另有 1 项为 `UPLOADED`。所有 22101 个 iCloud 批次资产目前仍为 `PENDING`，没有生成删除计划、没有确认、没有执行删除。

2026-09-06 17:09（Asia/Shanghai）仍有一次由旧代码启动的 `cleanup-ready` 删除前复核运行中：Python PID `67288`、`caffeinate` PID `67289`、PhotoKit helper PID `67294`。运行 ID 为 `d1e896a5-febb-4f06-b72b-aa2d25e9312a`，当时已完成约 2242/2400 项。运行中的 Python 进程不会热加载后来加入的分块加速逻辑；它完成复核后仍会先显示删除确认，在用户确认前不会删除。

若要立即改用新逻辑，可在原终端按 `Ctrl+C` 安全中止当前复核，然后重新运行同一条 `cleanup-ready` 命令。新进程会先显示一次汇总确认，确认后按每批最多 1000 项复核并立即删除；中止旧复核不会删除照片，但旧复核进度不会作为新逻辑的检查点复用。

### 扫描与容量

`phone scan` 会显示：

- 手机当前可读取的照片/视频逻辑项目总数、底层文件数和媒体容量。
- 严格早于两年截止时间的项目数和容量。
- 本次需要新增归档的数量。
- 已归档、只需继续手机清理的数量。
- 手机总容量、已用容量和可用容量。

Live Photo、RAW+JPEG 等关联资源按一个逻辑项目显示，同时保留实际文件数。

最近一次实机扫描结果：

```text
手机照片/视频总量 1929 项（2479 个文件，18.6 GiB）
符合两年条件 41 项（42 个文件，87.8 MiB）
本次新增归档 0 项（0 个文件）
已归档待手机清理 41 项（42 个文件）
```

这里的总量是 ImageCaptureCore 当前可见的媒体，不保证包含隐藏项目或仅存在于 iCloud、尚未下载到手机的原件。

### 跨批次幂等

程序按“绑定设备 + 稳定项目指纹 + 文件大小”查询历史归档。只有历史资源仍处于 `SAFE_TO_DELETE`、无 review 标记、归档记录和哈希证据完整且证据唯一时才复用。

重复扫描和同步不再下载或复制这些资源。新批次会引用已有的 `asset_resources` 和 `archive_files`，并在删除前重新检查外接盘证据。

修复前已经运行过两次同步，因此外接盘上存在两套旧的重复归档目录。不要擅自删除；应在后续单独设计“重复归档审计与安全回收”，并获得用户确认后处理。

### 日志

归档日志显示总体队列进度，例如：

```text
[归档 29/42] 稳定性验证 1/2, 下次检查等待 1 秒
[归档 29/42] 稳定性验证 2/2
[归档 29/42] 完成: SAFE_TO_DELETE
```

初次归档时，本地 APFS 外接盘的两次稳定性检查间隔为 1 秒；远程或测试适配器仍可使用配置值。删除前复核只做一次完整读取，不执行第二次稳定性轮询，也不等待 1 秒。

### 删除错误诊断

Swift helper 会返回：

- 成功和失败 token。
- ImageCaptureCore 逐项失败原因。
- 完成回调的 NSError domain、code 和 description。
- 实际使用的删除方法。
- PTP 回退的响应码。

Python 将原因写入 `phone_items.delete_error_detail`、`phone_assets.delete_error_detail`、终端日志和报告。

## 实机删除现状：尚未通过

不要宣称实机自动删除已经验收。

最新批次：

```text
batch_id: 20260905-212224-23206606
cutoff: 2024-09-04T16:00:00Z
state: COMPLETED_WITH_PHONE_ITEMS_REMAINING
iCloud Photos: enabled
归档: 41 个逻辑项目 / 42 个文件，全部已验证
手机删除: 0 成功 / 42 失败
```

第一次删除尝试：

```text
com.apple.ImageCaptureCore code -9941: Delete files failed
```

本机 SDK 将 `-9941` 定义为 `ICReturnDeleteFilesFailed`。实机同时报告：

```text
delete_capability_declared: false
can_accept_ptp_commands: true
```

因此高级 `requestDeleteFiles` 不能在这台 iPhone 上完成删除。代码随后加入了严格受限的标准 PTP `DeleteObject` 回退：只有高级接口 0 个成功、全批失败、错误恰为 `-9941`、没有只读或取消等逐项原因，并且设备允许 PTP 命令时才触发。

用户已经实机重试。PTP 回退也失败，42 个文件全部返回：

```text
PTP DeleteObject InvalidObjectHandle (0x2009)
```

只读实机诊断已确认根因：当前 ImageCaptureCore presentation 下 2479 个媒体文件的 `ptpObjectHandle` 全部为 `0`；标准 PTP `GetObjectHandles` 则返回 4057 个真实对象。切换到 `OriginalAssets` presentation 后 ImageCaptureCore handle 仍为 `0`，因此不能直接使用该属性。

2026-09-05 已完成代码修复，但尚未进行破坏性实机验收：

1. helper 先用 `GetDeviceInfo` 确认设备实际声明 `GetObjectHandles`、`GetObjectInfo` 和 `DeleteObject`。
2. 高级 API 整批以 `-9941` 失败后，只读建立并缓存当前 PTP 对象目录。
3. 每个待删资源必须通过“拍摄时间 + 精确大小”或“规范化文件名 + 精确大小”唯一映射到未保护、非文件夹的真实 PTP 对象。
4. 当前批次的 21 个 HEIC 和 21 个 JPG 均通过“拍摄时间 + 精确大小”唯一映射；42 个资源的映射 handle 也必须一一不同，否则整个 chunk 不执行 PTP 删除。
5. ImageCaptureCore 回调结果改用对象身份关联，不再用全为 `0` 的 handle 判断成功或失败。
6. PTP 目录读取实测约 3.5 分钟，配置中的 helper 命令超时已从 300 秒提高到 600 秒。重试时需保持手机解锁。

手机/PTP 路线的下一步是由用户重新执行原清理命令并再次明确确认。该路线与 iCloud 清理完全独立。不要代替用户确认；只有实机返回 42 个文件删除成功，并在随后扫描中确认对象消失，才能宣称自动删除验收通过。

## 当前批次记录

数据库中有两个已归档但手机项目仍保留的批次：

```text
20260905-203250-131cc3f0  COMPLETED_WITH_PHONE_ITEMS_REMAINING
20260905-212224-23206606  COMPLETED_WITH_PHONE_ITEMS_REMAINING
```

两批都指向同一组手机项目的历史归档。优先使用较新的 `20260905-212224-23206606` 继续诊断。手机/PTP 删除路线与当前 iCloud PhotoKit 进程相互独立。

iCloud 数据库当前有 1 个批次、22101 条批次资产记录：

```text
batch_id: icloud-20260906-081545-775111c3
job_id: 3ca6f06b-ae1b-4dc2-ab7b-9f755f7a90e8
batch state: NEEDS_ATTENTION
archive states: SAFE_TO_DELETE=2400, DISCOVERED=19700, UPLOADED=1
cleanup states: PENDING=22101
deletion_plan_sha256: null
confirmed_at: null
```

因此当前事实是“2400 项外接盘归档证据已就绪并正在做删除前复核”，不是“已从 iCloud 删除”。

## 常用命令

只读扫描：

```bash
.venv/bin/photoarchive --config config/local.yaml phone scan --profile wife
```

iCloud 环境检查和只读扫描应在当前妻子的独立 macOS 用户、妻子的 Apple Account 和系统照片图库下运行：

```bash
.venv/bin/photoarchive --config config/local.yaml doctor --adapter icloud
.venv/bin/photoarchive --config config/local.yaml icloud scan --profile wife
```

白天单批归档、核验并确认删除：

```bash
caffeinate -dimsu .venv/bin/photoarchive --config config/local.yaml \
  icloud sync --profile wife
```

夜间仅归档全部剩余候选，绝不删除：

```bash
caffeinate -dimsu .venv/bin/photoarchive --config config/local.yaml \
  icloud archive-night --profile wife
```

对全部已验证资产确认一次，逐批核验后通过一个 PhotoKit 事务统一删除：

```bash
caffeinate -dimsu .venv/bin/photoarchive --config config/local.yaml \
  icloud cleanup-ready --profile wife
```

查看并应用数据库迁移：

```bash
.venv/bin/photoarchive --config config/local.yaml db migrate
```

重新构建并自检 Swift helper：

```bash
native/photos-helper/scripts/build_app.sh
```

运行 Python 检查：

```bash
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest
```

当前结果：Ruff 全部通过，mypy 检查 24 个源文件成功，90 项 Python 测试通过。helper 构建、自检和版本检查成功，报告版本 `0.6.0`、手机 schema 2、PhotoKit schema 4。由于本机 Command Line Tools 的 SwiftPM manifest 库版本不一致，以下命令会在载入 `Package.swift` 时出现链接错误：

```bash
swift test --package-path native/photos-helper
```

这不是 helper 源码编译错误。`native/photos-helper/scripts/build_app.sh` 会绕过 SwiftPM，直接编译、签名并运行 JSONL self-test，目前可以成功完成。

生成当前批次报告：

```bash
.venv/bin/photoarchive --config config/local.yaml report \
  --job-id 5992b2a6-5711-47fd-b6ea-ebd4b8251956 \
  --output var/reports/20260905-212224-23206606
```

对象句柄映射已通过只读实机诊断，破坏性删除仍需用户重新确认。清理命令为：

```bash
.venv/bin/photoarchive --config config/local.yaml phone cleanup \
  --batch-id 20260905-212224-23206606
```

## 安全约束

- 删除计划必须与用户确认时的 SHA-256 摘要一致。
- 必须重新连接同一绑定手机并重新匹配全部资源。
- 任一必要资源缺失、归档证据不稳定、匹配不唯一或 iCloud 状态未知时禁止删除。
- 不清空“最近删除”。
- macOS Photos 删除只能位于专用 `PhotoLibraryAdapter.swift`，并受独立 iCloud 批次、完整资源复核、外接盘证据和删除计划摘要保护。
- PhotoKit 始终绑定当前 macOS 用户的系统照片图库，程序不能选择 Apple Account。不同 Apple Account 必须使用独立 macOS 用户、独立系统照片图库、独立配置、数据库和 profile。
- 当前独立 macOS 用户登录妻子的 Apple Account；执行前仍须确认“照片”所用的系统图库和 Apple Account 没有被切换。
- PhotoKit 删除会同步影响 iCloud 和同一 Apple Account 的其他设备；必须核对账号、批次和删除计划，并由用户亲自确认。
- 不使用 AppleScript、UI 自动点击或私有 Apple API。
- PTP 删除只能位于专用 Swift 手机适配器内，并受现有批次门禁保护。
- 不记录设备原始标识、完整本地路径、照片内容、经纬度、token 或凭据。

## 最近修改的文件

- `pyproject.toml`
- `src/photoarchive/__init__.py`
- `src/photoarchive/cli.py`
- `src/photoarchive/config.py`
- `src/photoarchive/database.py`
- `src/photoarchive/domain.py`
- `src/photoarchive/external_drive.py`
- `src/photoarchive/icloud_client.py`
- `src/photoarchive/icloud_source.py`
- `src/photoarchive/icloud_workflow.py`
- `src/photoarchive/phone_client.py`
- `src/photoarchive/phone_workflow.py`
- `src/photoarchive/pipeline.py`
- `src/photoarchive/protocols.py`
- `src/photoarchive/reporting.py`
- `src/photoarchive/migrations/006_phone_dedup_delete_diagnostics.sql`
- `src/photoarchive/migrations/007_icloud_photos.sql`
- `native/photos-helper/Info.plist`
- `native/photos-helper/Package.swift`
- `native/photos-helper/PhotoArchiveMediaHelper.entitlements`
- `native/photos-helper/Sources/PhotosHelperCore/PhoneDeviceAdapter.swift`
- `native/photos-helper/Sources/PhotosHelperCore/PhotoLibraryAdapter.swift`
- `native/photos-helper/Sources/photos-helper/main.swift`
- `native/photos-helper/Tests/PhotosHelperCoreTests/ProtocolTests.swift`
- `native/photos-helper/scripts/build_app.sh`
- `tests/test_cli.py`
- `tests/test_external_drive.py`
- `tests/test_icloud_workflow.py`
- `tests/test_phone_workflow.py`
- `tests/test_security.py`
- `README.md`
- `handoff.md`
