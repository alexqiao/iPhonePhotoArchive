# PhotoArchive

PhotoArchive 可以从两类来源归档超过两年的照片和视频：USB 连接的 iPhone，以及 Mac 系统“照片”图库中的完整 iCloud Photos 资产。它还可以从 iCloud Photos 单独迁移不限拍摄时间、原始文件超过 100 MiB 的视频。外接盘验证成功并经你确认后，它可以删除来源中精确对应的资产。它不需要微软账号。

归档成功后，程序会核对文件大小、SHA-256、QuickXorHash，并执行两次稳定性检查。删除前，它还会重新核对人员、截止日、资产身份和完整资源集合。任何资源缺失、身份不明确或验证失败都会阻止删除。

## 准备工作

当前归档盘配置为：

```text
卷名：ExternalDisk01
挂载点：/Volumes/ExternalDisk01
格式：APFS
归档根目录：/Volumes/ExternalDisk01/PhotoArchive
```

安装程序并初始化数据库：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
native/photos-helper/scripts/build_app.sh
.venv/bin/photoarchive --config config/local.yaml db migrate
```

先检查 Mac 和移动硬盘：

```bash
.venv/bin/photoarchive --config config/local.yaml doctor --adapter folder
.venv/bin/photoarchive --config config/local.yaml doctor --adapter phone
```

输出中的 `status` 应为 `ok`，并且 `external_drive.identity_matches`、`mounted`、`writable` 都应为 `true`。第一次访问 iPhone 时，macOS 可能要求授权。

## 释放 iCloud Photos 空间

该流程读取 Mac 的系统照片图库。请先确认 Mac 已登录需要清理的 Apple Account，并且“照片”App 已为系统照片图库启用 iCloud Photos。Mac 可以保持“优化 Mac 储存空间”；归档时 PhotoKit 会按需下载云端原件。

先做元数据扫描：

```bash
.venv/bin/photoarchive --config config/local.yaml doctor --adapter icloud
.venv/bin/photoarchive --config config/local.yaml icloud scan --profile wife
```

首次运行时，macOS 会请求照片权限，请选择“允许完全访问”。扫描会包含系统图库中本地及仅在 iCloud 的用户图库资产，也会包含隐藏资产和完整连拍。共享相簿和从 iTunes 同步的只读资产不属于清理范围。

PhotoKit 没有公开、无需下载即可读取每个云端原件字节数的接口。因此 `icloud scan` 会准确显示资产数和资源数，但容量显示“需在下载原件后确定”，不会使用私有字段估算。

确认 `ExternalDisk01` 已连接并有足够空间，然后运行：

```bash
.venv/bin/photoarchive --config config/local.yaml icloud sync --profile wife
```

`icloud sync` 是白天交互模式：每次最多处理 `icloud_cleanup.batch_size`
个资产（默认 1000），完成归档与验证后显示该批删除计划并等待一次人工确认。

需要一次确认后自动处理全部候选时，使用完整流水线：

```bash
caffeinate -dimsu .venv/bin/photoarchive --config config/local.yaml \
  icloud sync-all --profile wife --first-batch-size 50
```

`sync-all` 会冻结启动时扫描到的候选集合并显示授权范围 SHA-256。确认一次后，它先清理
已有的验证队列、恢复可安全续传的小批次，再按最多 1000 项逐批归档。全部候选归档后，
程序重新核对每个批次的外接盘证据和照片身份，最后把所有兼容计划合并为一个 PhotoKit
删除事务，因此 macOS 只收到一次系统删除请求。运行期间新增的照片不会被纳入；任何归档、
复核或删除失败都会立即停止，重新运行时必须重新扫描并确认。首个归档批次默认 50 项，
后续使用 `icloud_cleanup.batch_size`（默认 1000）；可通过 `--first-batch-size` 调整。

初始全量扫描后，程序会按 ID 分块复核候选。对于 PhotoKit 全量枚举仍返回、但按 ID 已经
不存在的同步残留资产，会在冻结范围前明确报告并跳过；候选冻结后才消失或变化的资产仍会
立即阻止后续归档或删除。

夜间无人值守时只归档和验证，不删除系统照片：

```bash
.venv/bin/photoarchive --config config/local.yaml icloud archive-night --profile wife
```

第二天可把所有已验证的小批次逐一复核后合并为一个 PhotoKit 删除事务：

```bash
caffeinate -dimsu .venv/bin/photoarchive --config config/local.yaml \
  icloud cleanup-ready --profile wife
```

合并事务的 macOS 删除弹窗最多等待 24 小时，允许程序夜间完成复核后停在弹窗处，第二天
再点击一次“删除”。所有待删批次必须属于同一人员和选择模式；普通按日期筛选的批次即使
跨午夜、冻结了不同截止时间，也会先按各自截止时间复核，再安全合并为一次删除事务。

夜间命令不会接受预先授权或自动确认删除，因为最终文件哈希和删除计划只有在
归档验证完成后才能确定。

程序会逐项执行：

1. 选择严格早于截止时间的照片、视频和 Live Photo。
2. 通过 PhotoKit 下载其公开的全部底层资源，包括 RAW/JPEG、Live Photo 配对视频及调整资源。
3. 生成 manifest，复制到外接盘并通过哈希和两次稳定性验证。
4. 删除已经验证的 Mac staging 临时副本，避免占满内置硬盘。
5. 重新获取同一 PhotoKit 资产并核对资源集合，生成不可变删除计划。
6. 显示整个 iCloud Photos 删除警告；只有你确认后才调用 PhotoKit 删除。

暂不确认或流程中断时，可继续指定批次：

```bash
.venv/bin/photoarchive --config config/local.yaml \
  icloud cleanup --batch-id '<batch_id>'

.venv/bin/photoarchive --config config/local.yaml \
  icloud resume --batch-id '<batch_id>'
```

PhotoKit 删除会同步影响使用同一 Apple Account 的所有设备，并先把资产移入“最近删除”。程序永远不会自动清空“最近删除”。如果目标是立即释放 iCloud 空间，请在确认外接盘归档和报告无误后，亲自在“照片”App 的“最近删除”中永久删除。

iCloud 报告位于 `var/reports/<batch_id>/report.json` 和 `icloud_cleanup.csv`。归档保存的是原件、PhotoKit 暴露的资源和 manifest，不是完整 Photos Library 克隆；相簿层级、人物识别和共享互动信息不能靠这些文件自动恢复。

## 迁移不限时间的大视频

大视频流程与“两年前”流程相互独立。它只选择系统“照片”图库中原始视频严格大于
100 MiB（104857600 字节）的视频资产，不限制拍摄时间；Live Photo 的配对短视频不会
被当作大视频。资产入选后，程序仍会归档 PhotoKit 暴露的全部关联资源。

先做只读元数据扫描：

```bash
.venv/bin/photoarchive --config config/local.yaml \
  icloud large-video scan --profile wife
```

PhotoKit 没有公开的云端原件大小字段，因此这个扫描不会下载视频。输出会区分已经缓存的
大视频、未超过阈值的视频和仍待测量的视频。大小测量只在下面两个归档命令中发生；测量
结果会写入本地数据库，后续运行不会重复读取同一个未变化的原始资源。

白天处理一个有界批次，归档验证后确认是否从整个 iCloud Photos 图库删除：

```bash
caffeinate -dimsu .venv/bin/photoarchive --config config/local.yaml \
  icloud large-video sync --profile wife
```

一次确认后按批处理全部符合条件的大视频：

```bash
caffeinate -dimsu .venv/bin/photoarchive --config config/local.yaml \
  icloud large-video sync-all --profile wife --first-batch-size 50
```

该命令同样先冻结候选范围并逐批归档，全部复核成功后发出一次合并删除请求。历史大视频
批次继续使用各自固化的阈值，新候选使用当前配置的阈值；任一批失败都会停止处理。

夜间测量并归档全部剩余大视频，但不删除照片图库中的资产：

```bash
caffeinate -dimsu .venv/bin/photoarchive --config config/local.yaml \
  icloud large-video archive-night --profile wife
```

第二天仍使用通用的 `icloud cleanup-ready --profile wife` 汇总确认并清理。大小判断采用
严格大于；恰好 100 MiB 的视频不会入选。阈值可以通过配置项
`archive_policy.large_video_threshold_bytes` 调整，并会固化到对应批次及删除计划中。

## 第一次使用

每个人只需创建一次档案。档案 ID 使用小写英文字母或数字；中文姓名放在显示名称中。

```bash
.venv/bin/photoarchive --config config/local.yaml profile add --id alex --name 'Alex'
.venv/bin/photoarchive --config config/local.yaml profile add --id wife --name '妻子'
.venv/bin/photoarchive --config config/local.yaml profile list
```

程序会建立完全分开的目录：

```text
~/Pictures/PhotoArchiveInbox/alex/
~/Pictures/PhotoArchiveInbox/wife/
```

以后还可以继续添加其他家人。自动清理要求每台 iPhone 明确绑定一个人员档案，避免把两个人的批次混在一起。

## 推荐：程序直接导入并清理

连接一台 iPhone，保持解锁，并在手机上选择“信任”。先做只读扫描：

```bash
.venv/bin/photoarchive --config config/local.yaml phone scan --profile wife
```

输出会在迁移前显示手机照片/视频总项数和媒体文件容量、本次迁移项数和容量，以及手机总容量、已用容量、可用容量、手机短标识、iCloud 照片状态、两年截止时间。Live Photo、RAW+JPEG 等关联文件按一个逻辑项目计数，同时在括号内显示实际文件数。容量来自 iPhone 的只读 PTP 信息；如果设备没有提供，程序会明确显示“系统未提供”，不会估算。截止日按 `Asia/Shanghai` 当天零点减两个日历年计算；截止日当天及之后的照片全部保留。

重复扫描会区分“新增归档”和“已归档待清理”。程序按绑定设备和稳定项目指纹复用已经验证的外接盘文件，不会再次下载或复制同一手机项目。删除失败时，终端和报告会保留 ImageCaptureCore 返回的只读、文件缺失、设备断开、取消或完成错误。

删除优先使用 ImageCaptureCore 的 `requestDeleteFiles`。部分 iPhone 不声明逐项删除能力，但允许标准 PTP 命令；此时 helper 会在高级 API 整批失败后读取当前 PTP 对象目录，仅当每个已复核文件都能通过拍摄时间、精确大小或规范化文件名唯一映射到不同的未保护原生对象时，才对同一不可变删除计划使用 PTP `DeleteObject`。任一对象无法唯一映射时整批回退都会停止，并显示实际删除方式和失败原因。

第一次使用时可以预先绑定，也可以让 `phone sync` 引导绑定：

```bash
.venv/bin/photoarchive --config config/local.yaml \
  device bind --profile wife
```

确认 `ExternalDisk01` 已连接，然后运行完整流程：

```bash
.venv/bin/photoarchive --config config/local.yaml phone sync --profile wife
```

手机命令会在终端实时显示扫描、逐项导入、移动硬盘复制、稳定性验证和手机清理进度。进度写到 stderr，最终 JSON 结果写到 stdout，因此脚本仍可单独处理最终结果。审计事件同时保存在 `var/logs/photoarchive.jsonl`；其中只记录短资产键和事件码，不记录照片文件名或完整路径。

程序会依次完成：

1. 只导入严格早于截止时间的照片和视频。
2. 把文件保存在 `~/Pictures/PhotoArchiveInbox/<人员>/pending/<批次>/`。
3. 归档到 `/Volumes/ExternalDisk01/PhotoArchive/<人员>/...`。
4. 完成哈希、manifest 和两次稳定性验证。
5. 显示最终删除数量、容量、手机和 iCloud 状态。
6. 你确认一次后，自动删除本批次在手机上的对应项目。

如果 iCloud 照片已开启，确认提示会明确说明：删除也会同步到 iCloud 和使用同一 Apple 账户的其他设备。程序不会清空“最近删除”，但不要把“最近删除”当作备份。

如果你暂时拒绝删除或手机断开，归档不会丢失。重新连接同一台手机后运行：

```bash
.venv/bin/photoarchive --config config/local.yaml \
  phone cleanup --batch-id '<batch_id>'

# 导入或归档阶段中断时
.venv/bin/photoarchive --config config/local.yaml \
  phone resume --batch-id '<batch_id>'
```

`--yes` 只能用于确认命令中明确指定的一个批次，不支持插入手机后无人值守删除。

成功后的报告位于：

```text
var/reports/<批次或作业>/report.json
var/reports/<批次或作业>/phone_cleanup.csv
```

报告区分 `READY`、`DELETED`、`REMAINING` 和 `FAILED`。隐藏相册、优化储存后未下载到手机的原件或系统没有暴露的项目可能仍留在手机上；程序不会猜测或删除这些项目。

## 备用：手动“图像捕捉”导入

手动流程仍可用于只归档、不删除手机内容。因为手动导入后缺少可靠的设备对象映射，这类批次永远不会获得手机删除资格。

### 1. 创建导入批次

先确认这次是谁的手机，再运行：

```bash
.venv/bin/photoarchive --config config/local.yaml batch prepare --profile wife
```

命令会创建一个带日期的空批次目录，同时打开“图像捕捉”和该目录。记下输出的 `batch_id`。

在“图像捕捉”中：

1. 连接并解锁 iPhone，首次连接时在手机上选择“信任”。
2. 在左侧选择这台 iPhone。
3. 将“导入到”设置为命令输出的 `destination` 目录。
4. 保持“导入后删除”关闭。
5. 选择照片并开始导入；等待“图像捕捉”完全结束。

这些文件不会进入 Mac 的系统“照片”图库，因此不同人的照片不会混在一起。

文件夹批次会归档你本次选中的全部内容，不套用配置中的“两年前”筛选规则。拍摄时间只用于移动硬盘上的年/月分类。

### 2. 归档整个批次

确认 `ExternalDisk01` 已连接，然后运行：

```bash
.venv/bin/photoarchive --config config/local.yaml \
  batch archive --batch-id '<batch_id>' --yes
```

这一条命令会自动完成计划、复制、验证、报告和整理。成功后：

```text
Mac 完成目录：
~/Pictures/PhotoArchiveInbox/<人员>/completed/<batch_id>/

移动硬盘归档：
/Volumes/ExternalDisk01/PhotoArchive/<人员>/<年>/<月>/<资产目录>/

报告：
var/reports/<batch_id>/report.json
var/reports/<batch_id>/archived_files.csv
```

程序只移动 Mac 上的整个批次，不会删除其中的文件，也不会删除 iPhone 内容。

## 中断或失败

查看状态：

```bash
.venv/bin/photoarchive --config config/local.yaml \
  batch status --batch-id '<batch_id>'
```

如果忘记了批次 ID，可以列出最近的批次：

```bash
.venv/bin/photoarchive --config config/local.yaml batch list
```

手动批次失败时，再次执行同一条 `batch archive` 命令即可。手机批次使用 `phone resume`。程序会先对账已有文件，再继续未完成步骤，不会覆盖移动硬盘上的不同内容。

只要批次中有一个文件失败，整个目录都不会移入 `completed`。

## 支持的导入内容

文件夹来源支持常见 iPhone 图片、视频、RAW，以及 AAE/XMP 边车文件。程序优先使用文件中的拍摄时间和 Live Photo 标识；缺少元数据时，会按同名主文件配对并使用文件修改时间，同时在报告中记录告警。

除 `.DS_Store` 和批次标记外，无法识别的文件会阻止整批完成，以免发生静默遗漏。

## 底层命令

手机日常使用只需 `phone scan` 和 `phone sync`。以下底层命令用于排查归档问题：

```bash
.venv/bin/photoarchive --config config/local.yaml \
  plan --adapter folder --batch-id '<batch_id>' --dry-run

.venv/bin/photoarchive --config config/local.yaml run --job-id '<job_id>' --yes
.venv/bin/photoarchive --config config/local.yaml resume --job-id '<job_id>' --yes
.venv/bin/photoarchive --config config/local.yaml verify --job-id '<job_id>'
.venv/bin/photoarchive --config config/local.yaml report --job-id '<job_id>'
```

离线 fixture 仍可用于验证核心归档状态机，不会读取真实媒体或移动硬盘：

```bash
.venv/bin/photoarchive --config config/example.yaml doctor --adapter fixture
.venv/bin/photoarchive --config config/example.yaml plan \
  --fixture fixtures/demo/library.jsonl --dry-run
```

## 质量检查

```bash
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest
native/photos-helper/scripts/build_app.sh
```

如果当前 Command Line Tools 的 SwiftPM 组件版本不一致，`swift test --package-path native/photos-helper` 可能无法载入 Package manifest；`build_app.sh` 仍会直接以 macOS 14 为目标编译并签名 helper。
