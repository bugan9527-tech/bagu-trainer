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
- `dist/BaguTrainer-0.1.0-windows-x64.zip`：分发压缩包。
- 同名 `.zip.sha256`：压缩包校验值。
- 包内 `BUILD_MANIFEST.json`：架构、版本、构建依赖版本及逐文件 SHA256。

`packaging/windows.spec` 通过 `_tkinter` hook 收集 Tcl/Tk，通过显式 backend 和 distribution
metadata 收集 Windows keyring；补充延迟导入的 engine。窗口模式不弹终端，不使用 UPX。
构建脚本只收录白名单资源，生成 Python、Tk、keyring、运行及构建组件的许可和声明，
在归档前拒绝配置、题库正文、SQLite、日志、源码脚本、测试和下载副本。
题库仅收来源清单、固定版本与许可声明元信息，完整题库由用户首次使用时下载。

## GitHub Actions 和草稿 Release

工作流：[windows-portable.yml](.github/workflows/windows-portable.yml)。
进入默认分支后，在 Actions 选择 **Windows x64 portable draft → Run workflow**，选择分支，
tag 必须为 `v` 加 `bagu_trainer/version.py` 的版本，版本变更时同步修改输入默认值。
PR 只运行构建/测试，不获得 Release 写权限。手动运行要求文件已在默认分支，
依据 [GitHub 手动运行规则](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)。

```powershell
gh workflow run windows-portable.yml --ref main -f tag=v0.1.0
gh run list --workflow windows-portable.yml
```

第一阶段在 `windows-2022` 使用独立 x64 环境生成 ZIP/SHA-256，执行 `release_tests`（含实际 EXE），
再构建/运行单独冻结验收程序，全部通过才生成发布说明。
第二阶段仅下载同一 run/attempt 的已验证附件，使用该 job 的 `GITHUB_TOKEN`（contents write、actions read）
创建草稿并上传 ZIP、校验文件、JUnit、`ACCEPTANCE.json`，无需长期 PAT 或模型 Key secret。
构建 job 只有只读权限，checkout 不保留 Git 凭据；工作流不自动正式发布。

`scripts/release_portable.py` 校验版本、归档和逐文件 SHA-256、干净源码提交、无跳过/失败的 JUnit
及同提交冻结验收；拒绝向其他提交的 tag 或正式 Release 上传。
同提交草稿可重跑并替换附件，上传中断仍保持草稿，重新运行即可重试；代码变化使用新版本标签。
发布说明给出 ZIP/校验文件固定入口和首次使用步骤。草稿仅授权者可下载，公开固定链接在正式发布后
才生效，依据 [GitHub Release 可见性说明](https://docs.github.com/en/rest/releases/releases)。审阅草稿直接使用下方 Assets。

诊断附件保留 14 天：`windows-validation-*`、`windows-portable-*`、`clean-windows-acceptance-*`。
失败时下载 JUnit、错误报告和 PyInstaller 警告，修复后重跑；云端实际状态以 run 页面为准。

## 独立冻结验收

```powershell
.\.venv-build\Scripts\python.exe scripts/build_acceptance.py --verify --work build/native-acceptance
```

工具真实创建向导和主窗口，以本地 HTTP 合成题库和模型完成首次使用、监听、弹题、判分、记录、
重启及生产 EXE 换目录读取数据。默认使用隔离的原生 Windows 凭据；CI 加 `--memory-credentials` 并在报告标注。
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
python -m pytest release_tests -q -W error
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
