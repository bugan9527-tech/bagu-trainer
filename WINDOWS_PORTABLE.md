# Windows x64 便携应用构建与验证

产物为 PyInstaller **onedir**：`BaguTrainer.exe` 与 `_internal/` 必须一起解压和移动。
双击 EXE 会进入现有启动协调层，首次使用中文向导，准备齐全后直接打开主窗口。
不需要用户安装 Python、Git 或 PowerShell。使用和迁移说明见 [PORTABLE_README.md](PORTABLE_README.md)。

## 构建

在 Windows 上使用带 Tcl/Tk 的 x64 CPython，创建独立构建环境，避免把开发环境中的额外包带入产物：

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv-build\Scripts\python.exe scripts/build_portable.py
```

输出：

- `dist/BaguTrainer/`：可直接启动的完整目录。
- `dist/BaguTrainer-0.3.0-windows-x64.zip`：分发压缩包，文件名版本由 `version.py` 生成。
- 同名 `.zip.sha256`：压缩包校验值。
- 包内 `BUILD_MANIFEST.json`：架构、版本、构建依赖版本及逐文件 SHA256。

本地验收可以指定独立输出目录，保留已有构建产物且不碰已安装客户端：

```powershell
$env:BAGU_USER_DATA_DIR = Join-Path $env:TEMP 'bagu-isolated-build'
.\.venv-build\Scripts\python.exe scripts/build_portable.py --output-dir dist/ui-redesign-stage6
$env:BAGU_PORTABLE_BUNDLE = "$PWD/dist/BaguTrainer"
$version = python -c "from bagu_trainer.version import __version__; print(__version__)"
$env:BAGU_PORTABLE_ARCHIVE = "$PWD/dist/BaguTrainer-$version-windows-x64.zip"
python -m pytest release_tests tests -q -W error --junitxml=validation/stage6/full.xml
.\.venv-build\Scripts\python.exe scripts/build_acceptance.py --verify --memory-credentials --output-dir dist/ui-redesign-stage6 --work validation/stage6/frozen
```

上述命令不调用 Release 脚本。工作区有未提交修改时 manifest 如实记录 `source_dirty=true`；`source_tree` 给出运行/构建源码（包括新增模块）的逐文件指纹，冻结驱动与生产包必须匹配。它是本地工作区验收包，不代表已满足干净提交的发布门禁。完整测试仍要求匹配器的只读历史题库基准，详情见 README。

`packaging/windows.spec` 通过 `_tkinter` hook 收集 Tcl/Tk，通过显式 backend 和 distribution
metadata 收集 Windows keyring；补充延迟导入的 engine。窗口模式不弹终端，不使用 UPX。
公共桌面主题见 [UI_DESIGN.md](UI_DESIGN.md)。两个 spec 显式收集 ttkbootstrap 数据及递归 metadata，
Pillow 由标准 hook 收集；主题和图像库的完整许可进入 `licenses/`，缺失时构建失败。
构建脚本只收录白名单资源，生成 Python、Tk、keyring、运行及构建组件的许可和声明，
在归档前拒绝配置、题库正文、SQLite、日志、源码脚本、测试和下载副本。
题库仅收来源清单、固定版本与许可声明元信息，完整题库由用户首次使用时下载。

## GitHub Actions 和草稿 Release

工作流：[windows-portable.yml](.github/workflows/windows-portable.yml)。
进入默认分支后，在 Actions 选择 **Windows x64 installer and portable draft → Run workflow**，选择分支，
tag 必须为 `v` 加 `bagu_trainer/version.py` 的版本，输入没有另一个需要同步维护的版本默认值。
PR 只运行构建/测试，不获得 Release 写权限。手动运行要求文件已在默认分支，
依据 [GitHub 手动运行规则](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)。

```powershell
gh workflow run windows-portable.yml --ref main -f tag=v0.3.0
gh run list --workflow windows-portable.yml
```

第一阶段在 GitHub 托管的临时 `windows-2022` 上依次执行：

1. 要求干净 checkout，仅构建一次生产 `dist/BaguTrainer`，生成 ZIP/SHA-256。
2. 按 `packaging/installer-toolchain.json` 的 HTTPS 来源和 SHA-256 下载 Inno Setup 6.7.3、innounp；
   解包工具而不安装，额外核对工具 EXE hash。不使用预装浮动版本或 `latest`。
3. 执行全部 `release_tests`（含实际便携 EXE、隐私与发布拒绝用例），不允许失败或跳过；
   构建/运行原独立冻结验收驱动，使用内存凭据与 localhost 模型。
4. 隔离 AppId/目录夹具检查跨版本覆盖、占用等待、快捷方式归属、同名便携副本和卸载数据保留。
5. 从同一个生产目录构建 Setup、逐文件解包核对并生成独立 SHA-256。
6. 在临时 runner 按默认当前用户目录试装最终生产 Setup，个人数据指向临时独立 profile，
   核对空配置首次引导、运行时维护阻止、重复安装、实际注册和开始菜单/桌面入口，
   复用冻结驱动验证已安装 EXE，再验证同版覆盖、已启用自启动入口、数据保留和卸载。
7. 以 `--prepare-evidence` 生成隐私扫描尚为 pending 的脱敏材料，扫描源码、载荷、嵌套归档、
   Setup、ZIP 和附件，再核对扫描绑定的摘要。全部通过后才生成可交付总结果。

`scripts/acceptance/installed_release.py` 只允许 `GITHUB_ACTIONS=true` 且
`RUNNER_ENVIRONMENT=github-hosted` 的 Windows，拒绝开发机及自托管 runner；
预先发现应用注册、默认数据目录或同名入口也拒绝执行。生产 AppId 的注册和真实 shell 目录
仅存在于临时 runner；程序在该用户默认目录，学习数据在 `RUNNER_TEMP` 的独立目录，凭据为内存/空 backend，
不调用真实模型或 Windows 凭据接口。本地使用 `installer_lifecycle.py` 的重定向夹具。

第二阶段仅下载同一 run/attempt 的已验证附件，使用该 job 的 `GITHUB_TOKEN`（contents write、actions read）
重新计算 ZIP/Setup hash，复核版本、提交、源码指纹、相同 payload manifest、完整验收与脱敏报告，
再创建/更新草稿，无需长期 PAT 或模型 Key secret。
构建 job 只有只读权限，checkout 不保留 Git 凭据；工作流不自动正式发布。

草稿附件为 Setup、ZIP、各自 `.sha256`、脱敏 JUnit、`ACCEPTANCE.json`、
`INSTALLED_ACCEPTANCE.json`、`INSTALLER_LIFECYCLE.json`、`INSTALLER_MANIFEST.json`、
`RELEASE_VALIDATION.json`、`PRIVACY_AUDIT.json`。原始本机路径、机器名、测试 stdout、配置、凭据与验收驱动不上传 Release。
原始诊断只保留在 Actions 诊断附件，不属于用户下载。

`scripts/release_portable.py` 保留旧入口名称，负责双产物门禁；缺报告、错版本/提交/hash、
脏源码、测试失败/跳过、冻结或安装未完成均阻止上传。跨版本由合成夹具验证，
最终 Setup 实测的是同版覆盖，不将它描述成两版生产发布包的实测升级。
仍拒绝向其他提交的 tag 或正式 Release 上传。
同提交草稿可重跑并替换附件，上传中断仍保持草稿，重新运行即可重试；代码变化使用新版本标签。
发布说明突出 Setup 下载、把 ZIP 列为备选，并给出校验入口和首次使用步骤。
维护者审阅报告和草稿附件后，必须在 GitHub 明确点击 **Publish release** 才会正式发布；
本流程没有定时发布或自动转正式的路径。草稿仅授权者可下载，公开固定链接在正式发布后
才生效，依据 [GitHub Release 可见性说明](https://docs.github.com/en/rest/releases/releases)。审阅草稿直接使用下方 Assets。

诊断附件保留 14 天：`windows-validation-*`、`windows-release-*`、`clean-windows-acceptance-*`。
失败时下载 JUnit、错误报告和 PyInstaller 警告，修复后重跑；云端实际状态以 run 页面为准。

## 独立冻结验收

```powershell
.\.venv-build\Scripts\python.exe scripts/build_acceptance.py --verify --work build/native-acceptance
```

工具真实创建向导和主壳，以本地 HTTP 合成题库和模型完成首次使用、监听、出题/独立审核、内嵌练习、判分、历史快照、重练、幂等记录和重启。冻结驱动检查七页与图标、切页保留基线、新题不抢页；生产 EXE 单独检查主题/图标诊断、后台启动、单实例唤起和换目录读取临时学习数据。默认使用隔离的原生 Windows 凭据；本轮使用 `--memory-credentials`，不验证真实凭据库写入，并在报告标注。
生成的 `BaguTrainerAcceptance-windows-x64.zip` 可在未安装 Python/Git 的机器运行。
`.wsb`、人工复验和证据要求见 [CLEAN_WINDOWS_ACCEPTANCE.md](CLEAN_WINDOWS_ACCEPTANCE.md)。

## 目录和升级约定

| 接口/目录 | 用途 |
| --- | --- |
| `paths.RESOURCE_ROOT` | 只读源码根或 PyInstaller `_MEIPASS`（`_internal`） |
| `paths.INSTALL_ROOT` | 源码根或 EXE 所在目录，只用于启动与快捷方式 |
| `config.USER_DATA_ROOT` | 默认 `%LOCALAPPDATA%\BaguTrainer`，不包含程序版本或安装路径 |
| `config.CONFIG_PATH` / `config.data_dir()` / `config.db_path()` | 配置、版本题库和学习库的统一位置；相对 DB 路径按用户目录解析 |
| `runtime/` | 当前用户共用的单实例文件锁和私有命名管道凭据 |
| `logs/desktop.log` | 2 MiB 滚动日志，最多 3 个备份；启动错误只写异常类型，已解析 Key 脱敏 |

源码仍可通过 `python -B scripts/start_desktop.py` 启动，与 EXE 共用同一用户数据和实例锁。
移动或替换整个程序目录不会重置学习库。先退出再升级，移动后重新启用登录自启动。
`BAGU_USER_DATA_DIR` 仅用于高级隔离，必须是绝对路径；不要将用户目录放进程序目录。

旧项目数据不自动读取。首次向导“迁移旧数据…”或 `--migrate-from SOURCE` 调用
`migration.migrate_legacy(source, cancel=..., progress=...)`：

1. 检查旧实例已退出且目标没有现有配置或学习数据，不覆盖已有用户数据。
2. 在用户目录的独立暂存区复制并 SHA256 校验题库；只复制受支持的数据文件与版本目录。
3. 使用 SQLite backup API 快照默认或自定义路径的学习库，检查完整性并显式关闭句柄。
4. 将相对监听路径转换为旧项目下的绝对路径；旧明文 Key 经原 keyring 凭据项保存、回读确认，
   新配置只写空 Key。凭据失败则不启用迁移结果。
5. 完成校验后启用数据、配置和迁移回执；普通失败恢复原目标占位数据，原项目不删除。
   同一来源重复迁移幂等，不覆盖迁移后修改的配置。

旧库若采用 WAL 模式，SQLite 只读快照可能创建协调用的 `-wal` / `-shm` 辅助文件，
原配置、题库和数据库内容不变。突然断电后的整个多文件迁移不承诺事务恢复，原目录仍可作为备份；
目标存在未完成数据时应先备份核对，迁移程序会拒绝直接覆盖。

## 测试

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
python scripts/run_release_tests.py --output validation/release-tests.xml
python -m pytest tests -q
git diff --check
```

`test_portable_bundle.py` 需要先构建实际 EXE；未构建或非 Windows 时明确跳过。
测试全部使用临时用户目录、测试 Key、本地模拟接口或内存凭据，不修改真实自启动或真实凭据。
实际 Windows keyring 后端探测只实例化 backend，不读写账户 Key。

本机 Windows 10 x64（19045）、CPython 3.10.11、PyInstaller 6.22.3 验证：

| 场景 | 验证内容 |
| --- | --- |
| 中文和空格路径 | 拷贝整个包，在中文程序目录和中文 LOCALAPPDATA 下启动；清空 PATH |
| 程序目录不可写 | Windows ACL 拒绝数据和属性写入，确认写入探针失败；诊断、首次向导、配置窗口仍启动，程序文件 SHA256 不变 |
| 重复启动 | 初始化和主窗口阶段第二个 EXE 正常退出；跨安装目录共用原实例及管道凭据 |
| 运行库 | 实际 Tk 窗口、Tcl 8.6、Windows WinVaultKeyring、SSL、SQLite 和来源元信息加载成功 |
| 自启动 | 原生 Shell COM 创建临时 `.lnk`，检查目标为 EXE、参数 `--background`，随后删除临时快捷方式 |
| 迁移和升级 | 带已答题、作答明细、掌握度和复习时间的真实 SQLite；迁移后启动，换成第二个干净程序目录并删除第一个目录，再启动，历史保留 |
| 失败重试 | 取消、损坏学习库、Key 写入失败、启用配置失败回滚、目标冲突、旧实例运行、默认/相对/绝对自定义数据库路径 |
| 发布内容 | 清单逐文件 hash 校验，拒绝第三方正文、个人数据、开发脚本和测试进入包 |

发布测试 160 项通过，历史回归 171 项通过，共 331 项。历史回归仍需要已有本地题库快照；
该快照不进入分发包。本机验证未覆盖 Windows 11、其他机器的安全软件、代码签名或系统重启登录；
自启动验证检查真实快捷方式，不修改实际 Startup 目录。
冻结验收和未覆盖的干净系统、人工操作以 [VALIDATION.md](VALIDATION.md) 为准。

## 后续接入接口

- `paths.command(*arguments)`：源码和冻结 EXE 共用的子窗口入口；配置窗口传 `--configure`。
- `login_startup.install()` / `uninstall()`：主窗口菜单和 EXE 参数共用的原生快捷方式接口。
- `runtime.package_report(destination)` / `--check-package REPORT`：写入不含 Key 的运行环境 JSON。
- `migration.migrate_legacy()`：可用于后续安装器，进度从工作线程回调，取消使用 `threading.Event`。
- `StartupCoordinator`、`BankBuildService`、`onboarding` 接口沿用；可继续接入版本回退或安装器，
  更新程序不能覆盖用户目录，也不能将用户完整题库重新打入发布包。
