# v0.3.0 Windows 安装版实施规范

状态：安装器构建、生命周期及 CI 双产物草稿门禁已实现，当前产品版本为 `0.3.0`。入口为 `packaging/windows-installer.iss`，生命周期共用代码为 `packaging/windows-installer-lifecycle.iss`，输出名使用 `-Setup.exe`。升级、正常退出等待、卸载、快捷方式归属和数据保留已使用独立 AppId、编译时重定向目录与合成数据进行原生隔离夹具验收；开发机没有运行生产安装器。最终 Setup 的试装、同版覆盖、已安装 EXE 冻结及卸载已配置为临时 GitHub 托管 runner 的必要门禁；尚未远端运行的检查不标记通过。实现与复现命令见 [WINDOWS_INSTALLER.md](WINDOWS_INSTALLER.md) 和 [WINDOWS_PORTABLE.md](WINDOWS_PORTABLE.md)，证据与未验范围见 [VALIDATION.md](VALIDATION.md)。完整生产包的默认系统目录/向导、故障恢复、重启登录仍待后续验证。下文保留最初基于 `0.2.0` 的调查与实施计划作为历史设计基线，不代表当前未实现状态。

## 1. 范围与固定决策

v0.3.0 新增 Windows x64 安装器，继续提供便携 ZIP。安装器使用 **Inno Setup 封装现有 PyInstaller onedir**，两种发行形式使用同一份经过审计的 `dist/BaguTrainer/`，不另做 onefile，不引入另一套运行入口。

| 项目 | 规范 |
| --- | --- |
| 安装范围 | 默认且仅支持当前用户；`PrivilegesRequired=lowest`，不允许切换全用户安装，不请求管理员权限 |
| 默认程序目录 | `%LOCALAPPDATA%\Programs\BaguTrainer`，不含版本号 |
| 默认数据目录 | `%LOCALAPPDATA%\BaguTrainer`，沿用现有规则，不随安装形式和版本改变 |
| API Key | 沿用 Windows 凭据管理器，默认 service 为 `bagu-trainer`、user 为 `llm_api_key`；安装器不读取、写入、导出或删除 Key |
| 应用身份 | 固定 AppId `{C95D2707-83B5-4D34-A88D-7F30C5AC6E19}`，自 v0.3.0 起永久沿用 |
| 开始菜单 | 默认创建当前用户的 `BaguTrainer` 入口，指向安装后的 EXE |
| 桌面快捷方式 | 当前用户范围，可选，首次安装默认不勾选；升级沿用此前选择 |
| 登录自启动 | 保留应用现有启用/取消入口；安装器不默认启用，不创建第二套 Run 注册表项、任务或服务 |
| 升级 | 更新程序文件；保留配置、题库、学习记录、日志、窗口状态和凭据 |
| 卸载 | 默认保留全部个人数据及凭据；v0.3.0 不提供“删除个人数据”复选框 |
| 安装版与便携版 | 默认共用数据、凭据与单实例规则，不能同时运行两个主实例 |
| 发布 | ZIP 与 Setup EXE 同版本、同提交、同 payload；继续只创建或更新 GitHub 草稿 Release |

支持目标为 Windows 10/11 x64；不宣称支持 Windows x86、ARM64 或全用户部署。用户端不要求 Python、Git、PowerShell 或 Inno Setup。代码签名不在本次设计实现的前置范围；未签名产物需在发布说明中如实标注。

## 2. 已核查的现状与缺口

| 检查项与源码依据 | 当前行为 | 安装版接入要求 |
| --- | --- | --- |
| `packaging/windows.spec` | windowed `BaguTrainer.exe`、`COLLECT` onedir、`_internal`；白名单资源；显式 Windows keyring backend、Tk、ttkbootstrap 及许可 | 整体封装，不移动 `_internal`，不重新收集开发目录 |
| `scripts/build_portable.py` | 独立构建环境；资源审计；生成 ZIP、SHA-256、`BUILD_MANIFEST.json`；记录提交、dirty 标记和源码指纹 | 安装器只接受校验通过的同一 onedir；另记录安装器自身的来源和工具链 |
| `bagu_trainer/paths.py` | frozen 时 `RESOURCE_ROOT=sys._MEIPASS`、`INSTALL_ROOT=EXE.parent`；默认 `USER_ROOT=LOCALAPPDATA/BaguTrainer` | 无需改变路径逻辑，不把 `{app}` 注入用户数据路径 |
| `bagu_trainer/config.py` | `config.json` 位于用户根目录；`data/` 为数据目录；默认库为 `data/trainer.db`；相对 DB 路径按用户根解析，绝对 DB 路径保持原值 | 安装器不加载配置，不重写 DB 路径，不迁移或删除外部数据库 |
| `bagu_trainer/secrets.py` | Key 解析仍有 env → keyring → 旧配置兼容顺序；新 Key 不回退明文写盘；显式隔离 profile 使用独立 service | 保留兼容顺序，不新增安装版 service 或注册表明文 Key，不触发旧 Key 迁移 |
| `bagu_trainer/desktop_instance.py`、`desktop.py` | 用户根下 `runtime/owner.lock` 的第 0 字节使用 `msvcrt.locking`；管道只接受 `_show_dashboard`、`_desktop_status` | 保留此锁域；没有现成命名 mutex 或远程退出接口，不能直接以 `AppMutex` 替代 |
| `bagu_trainer/desktop.py` | 主入口重复启动时前台请求唤起后退出；`--background` 重复启动直接退出；配置、诊断、自启动参数有独立分支 | 单实例验收针对正常主入口；安装前还需检查占用安装文件的辅助窗口/进程 |
| `bagu_trainer/login_startup.py` | 当前用户 Startup 下固定 `BaguTrainer.lnk`；原生 Unicode Shell COM；目标为启用时 EXE，参数 `--background`，工作目录为 `INSTALL_ROOT` | 稳定安装路径利于升级；必须处理便携版既有入口及卸载归属 |
| `scripts/install-startup.ps1` | 源码/便携维护脚本，可转调用 EXE 参数 | 不作为安装器运行依赖，不向用户安装 PowerShell 脚本 |
| `.github/workflows/windows-portable.yml` | PR 构建验证；手动运行经只读构建 job 后由独立写权限 job 发草稿；附件限定当前 run/attempt | 在原工作流增加安装器构建与验收，不另建不受控发布通道 |
| `scripts/release_portable.py` | 当前只验证和上传 ZIP；校验 tag、完整提交 SHA、归档清单、无失败/跳过 JUnit、同提交冻结验收；拒改正式 Release/其他提交 tag | 扩展为双产物验证，并新增安装器验收门禁；不得复用旧报告冒充安装验收 |

`bagu_trainer/version.py` 当前仍为 `0.2.0`。仓库当前未发现 `.iss` 或安装器构建流程。现有自动化报告描述的是便携/冻结应用验证，不能证明安装、升级或卸载已通过。

## 3. 程序目录与数据目录冲突检查

默认布局如下，二者是 `%LOCALAPPDATA%` 下不同分支，互不包含，不存在默认路径冲突：

```text
%LOCALAPPDATA%\
  Programs\BaguTrainer\          ← Inno 管理的程序目录
    BaguTrainer.exe
    _internal\                  ← PyInstaller 只读资源与依赖
    BUILD_MANIFEST.json
    使用说明.md
    unins*.exe / unins*.dat      ← Inno 卸载文件
  BaguTrainer\                  ← 应用管理，安装/卸载不得清理
    config.json
    window.json
    data\                       ← 题库、onboarding、trainer.db 等
    logs\
    runtime\                    ← owner.lock、管道端点信息
```

路径结论来自源码规则与路径结构比较，不是对本机实际数据的扫描。程序根和资源根已有分离，安装目录不可写时也应正常工作。

实施时增加以下安装器路径校验（交互安装与静默 `/DIR` 均执行）：

1. 按 Windows 大小写不敏感规则规范化绝对路径；比较完整路径组件，禁止用字符串前缀代替父子目录判断。
2. 安装目标不得等于、包含或位于默认数据目录之内；拒绝盘根、用户目录根、`LOCALAPPDATA` 根等过宽目标。自定义目标仅接受专用程序目录，不接管已有便携目录或不明内容目录；已有同 AppId 的安装目录例外。
3. 对候选路径及父目录的 junction/symlink/reparse point 解析后再比较；无法安全确定实际目标则拒绝安装。不能让目录别名绕过数据保护。
4. `BAGU_USER_DATA_DIR` 是现有高级隔离能力，不是安装版新默认。安装器不持久化此变量，快捷方式不附带它；若安装器环境显式提供该变量，要求绝对路径，并对它执行相同的冲突校验和锁保护。
5. 不通过读取用户 `config.json` 搜索自定义 DB。无法在不读配置的前提下穷举外部数据库或其他进程的 profile；因此只更新清单拥有的程序文件，绝不递归删除自定义目标中的未知内容。发现未知文件与新 payload 路径碰撞时应停止并提示换目录。
6. 首次安装允许选择安全的自定义目录；升级使用同 AppId 上次目录，v0.3.0 不支持通过升级搬家。拒绝升级时传入另一目标造成重复登记；搬家需卸载后重装，数据仍保留。

禁止把个人配置、题库正文、SQLite、日志或 `bridge-credentials.json` 放进安装 payload。安装器不生成默认配置，不下载题库，不调用 `--migrate-from`；首次运行继续使用现有向导，旧源码数据继续由用户显式迁移。

## 4. Inno Setup 配置约定

实现文件为 `packaging/windows-installer.iss`。以下保留设计时的关键约束示意；实际可编译脚本与构建使用方法见上述实现文档：

```ini
[Setup]
AppId={{C95D2707-83B5-4D34-A88D-7F30C5AC6E19}
AppName=BaguTrainer
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\BaguTrainer
DefaultGroupName=BaguTrainer
PrivilegesRequired=lowest
UsePreviousAppDir=yes
UsePreviousTasks=yes
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
Uninstallable=yes
CloseApplications=no
RestartApplications=no
OutputBaseFilename=BaguTrainer-{#AppVersion}-windows-x64-setup

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\BaguTrainer\BaguTrainer"; Filename: "{app}\BaguTrainer.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\BaguTrainer"; Filename: "{app}\BaguTrainer.exe"; WorkingDir: "{app}"; Tasks: desktopicon
```

- `AppId` 字面值中的左花括号需用 `{{` 转义；不得每次构建生成新 GUID，不拼接版本、安装路径或发行名称。注册表卸载身份为固定 AppId 加 `_is1`，登记在当前用户范围，后续版本保持相同安装范围、架构登记规则和 AppId。
- 不设置 `PrivilegesRequiredOverridesAllowed` 开放提权；不写 HKLM、不创建公共桌面入口、不修改 PATH/文件关联/防火墙。用户以另一个账户主动提权执行会改变当前用户含义，应在说明中要求以本人普通权限运行。
- `AppVersion` 和数值版本资源由 `bagu_trainer/version.py` 派生；v0.3.0 对应 `0.3.0` / `0.3.0.0`。tag 必须为 `v0.3.0`。固定 AppId 解决应用身份，不自动解决降级：检测到已安装更高版本时拒绝降级，同版允许修复。
- `[Files]` 的递归输入只能是已审核 payload，不能指向仓库根、用户目录或整个 `dist`；必须包含 notices、licenses、manifest、EXE 和完整 `_internal`。
- 不添加指向数据目录的 `[InstallDelete]`、`[UninstallDelete]` 或 `[Dirs]` 清理规则；禁止对 `{app}` 使用不加约束的递归清空。
- 开始菜单入口不依赖可选 task；桌面 task 首次默认不选，后续沿用，显式取消时只删除本安装所拥有的旧入口。
- 完成页提供可选“启动 BaguTrainer”，默认不启动；静默安装不启动。只有成功完成且释放维护锁后才允许执行 EXE；不做安装时模型连接测试。

## 5. 单实例与安装期间的维护保护

应用不新增实例标识。默认安装版、便携版、源码入口仍锁定同一 `USER_DATA_ROOT/runtime/owner.lock`。显式设置不同 `BAGU_USER_DATA_DIR` 时沿用既有 profile 隔离语义；不能声称不同 profile 也全局互斥。

安装器在 Pascal Script 中通过 Windows 文件 API 实现与 `OwnerLock` 兼容的第 0 字节独占锁（`CreateFileW` / `LockFileEx`，非阻塞）。实现前必须用实际 EXE 验证其与 `msvcrt.locking` 的互斥性；不能只检查锁文件“存在”，也不能依赖 EXE 文件名或 `AppMutex` 猜测运行状态。

1. 安装、升级、修复、卸载均先取得当前数据根的维护锁；若有显式 profile，则同时保护默认根与该 profile，按规范化路径固定顺序获取，失败释放全部已获锁。
2. 获取失败时提示用户从现有窗口正常退出后重试；静默模式返回明确非零状态且不改程序。现有管道不支持退出，不增造 `_quit` 请求，不强杀进程，不删除锁或管道凭据绕过检测。
3. 无锁文件时可创建 `runtime/owner.lock` 并初始化第 0 字节，取得锁后持续持有至程序文件和快捷方式维护完成，避免检查后应用又启动的竞态。此操作只协调运行状态，不读取配置、SQLite 或 Key；失败/取消时释放句柄，不删除应用运行目录。
4. 默认主实例锁不足以覆盖独立配置窗口、不同 profile 的同一 EXE 或进程退出尾声。还必须检测目标目录 EXE/DLL 等文件占用，发现占用则提示正常关闭后重试；静默失败。不得自动强制关闭或安排重启后覆盖。`CloseApplications=no` / `RestartApplications=no` 用于避免隐式关停。
5. 两个安装维护进程也须串行；维护锁和固定安装器互斥共同覆盖安装/卸载并发，不能与应用实例锁混为一谈。

文件锁兼容、占用检测和并发验收是首版安装器上线阻断项。不能因为没有修改 Python 运行逻辑就省略它们。

## 6. 自启动与快捷方式归属

登录自启动继续由应用的 `login_startup.install()` / `uninstall()` 管理，仍为一个当前用户 Startup `BaguTrainer.lnk`。冻结入口支持 `--install-startup`、`--uninstall-startup`，但这些参数经 `runtime.run()` 会先配置用户日志；安装器不应为探测状态随意执行 EXE。

安装器仅使用 Unicode Shell Link COM 读取这一个快捷方式的目标、参数和工作目录，不读数据文件或凭据；按下列规则处理：

| 情况 | 安装/升级 | 卸载 |
| --- | --- | --- |
| 没有 Startup 入口 | 不创建；用户可在应用内启用 | 无操作 |
| 目标为本安装的 EXE，参数为 `--background` | 保留已启用状态；同路径升级不重写，必要修复只更新本安装目标和工作目录 | 删除该快捷方式，避免留下失效目标 |
| 目标为便携 EXE、源码 Python 或另一个目录 | 默认保留，不自动抢占；说明若要改用安装版登录启动，应在安装版内重新启用 | 保留，不调用无归属检查的 `login_startup.uninstall()` |
| 快捷方式损坏或无法判定归属 | 不覆盖，给出可读提示 | 不删除，报告未处理的入口 |

目标比较需规范化绝对路径，不能只比较 `BaguTrainer.exe` 文件名。不得将 Startup 入口写进 Inno 无条件删除清单：用户可能在安装后通过便携版重新启用，卸载必须检查当时的实际归属。

开始菜单及桌面入口与 Startup 是不同入口；开始菜单/桌面由安装器管理，始终指向 `{app}\BaguTrainer.exe`，参数为空、工作目录为 `{app}`。所有路径支持中文和空格，不使用 shell 字符串拼接执行。

## 7. 升级、修复与卸载边界

- v0.2.0 便携版没有 Inno 安装登记，因此首次安装 v0.3.0 是“首次程序安装、复用既有数据”，不是识别并卸载便携目录。便携目录不移动、不覆盖、不删除；安装后读取同一用户目录即可恢复现有学习记录。
- 自 v0.3.0 起，后续安装器通过固定 AppId 和当前用户安装登记识别原安装。同版修复和升级均使用上一安装目录、沿用快捷方式选择，最终仅有一个卸载条目。
- 写入前验证完整 payload、版本、路径、空间、权限和维护锁。在安装器私有临时目录暂存新文件，对旧清单拥有的程序文件做可恢复备份；失败时恢复旧程序或明确报告可重试修复状态，不能报告成功或启动混合版本。断电不承诺多文件原子性；再次运行同版安装器应能修复。
- 清理旧 `_internal` 残留仅限上一版受管清单拥有且新清单不再包含的文件，并校验实际目标位于受管安装根。禁止通配符删除未知用户文件；未知内容保留，阻止目录清空即可。卸载仅清理安装登记、受管程序文件、受管开始菜单/桌面入口以及确认归属的 Startup 入口。
- 升级和卸载不改 `config.json`、`data/`、`window.json`、日志、外部 DB、题库版本指针或 Windows 凭据。库结构若未来需迁移，属于独立运行时方案，不能藏在本安装器工作中。
- 卸载后重新安装或启动保留的便携版应能恢复原记录；保留的 Key 继续由同一凭据项解析。应用配置完整时不应因换成安装版而重新初始化。

## 8. 构建、依赖与发布流程

### 8.1 构建输入及产物

构建基线继续使用仓库锁定的 PyInstaller `6.22.3` 和现有隔离 x64 Python 环境。新增构建依赖 **Inno Setup 6.7.3 / ISCC**（仅构建端）；下载地址、安装包 SHA-256、工具版本应登记到新增 `packaging/installer-toolchain.json`，安装工具前校验来源与摘要。不使用 runner 预装的浮动版本或 `latest`，不把 ISCC 作为 Python requirements 加入运行依赖。

实施阶段需从官方发行渠道取得并核实工具安装包摘要后填写真实值；本文不编造 hash。CI 工具安装可使用 runner 权限，这不改变最终应用安装器的当前用户、无提权约定。

依赖顺序：

```text
干净提交 + version.py + 固定 Python/依赖
  → build_portable.py / windows.spec
  → 审计 onedir + BUILD_MANIFEST + ZIP/SHA256
  → 原 release_tests + 单独冻结验收
  → build_installer.py 校验相同 payload → 固定 ISCC 编译
  → Setup EXE/SHA256 + INSTALLER_MANIFEST.json
  → 安装/升级/卸载隔离验收 → INSTALLER_ACCEPTANCE.json
  → release_portable.py 双产物校验 → 当前 run/attempt 附件 → 草稿 Release
```

计划新增的公开附件：

- `BaguTrainer-0.3.0-windows-x64-setup.exe` 及 `.exe.sha256`。
- `INSTALLER_MANIFEST.json`：应用版本、架构、固定 AppId、源码提交/dirty、输入 `BUILD_MANIFEST.json` 摘要、ZIP 摘要、Setup 摘要、ISCC 版本与工具链锁文件/`.iss`/构建脚本摘要、安装资源清单。
- `INSTALLER_ACCEPTANCE.json`：同提交与同 Setup 摘要、平台/隔离方式、各安装场景结果及明确未覆盖项；不能包含真实账户数据、Key、管道认证字节或凭据值。

继续发布原 ZIP/校验文件、JUnit、`ACCEPTANCE.json`。验收驱动与干净 Windows kit 仍独立分发，不嵌入生产安装包。

现有 `source_provenance()` 只枚举 `.py`、`.spec`、requirements，**不涵盖 `.iss`**。新增安装器 manifest 必须覆盖 `.iss` 和工具链锁文件；如扩充共同源码指纹，冻结驱动也必须使用同一规则，避免生产/验收来源比较失配。

### 8.2 发布门禁

1. 保留现有 tag/版本一致、完整提交 SHA、clean source、ZIP 清单逐文件 hash、无失败/跳过 JUnit、同提交冻结验收等检查。
2. 安装器构建前校验 onedir 实际文件与 ZIP 的 manifest 完全相符，防止 ZIP 验证后 payload 又被修改；安装器验收后复核 Setup hash。不得从另外一次构建“找一个相同版本 EXE”。
3. 新增安装器 manifest、SHA-256 和安装验收报告一致性校验；安装后除 Inno 卸载文件外，受管 payload 必须与原 manifest 逐文件一致。污染包、缺资源、错版本、错 AppId、报告缺项或任一验收失败均不得发草稿。
4. PR 只构建与测试，无发布写权限；手动运行仍经独立 draft-release job，只下载同一 run/attempt 的 ZIP、Setup 和报告，不重新构建。
5. 发布说明同时提供“安装版 EXE”“便携版 ZIP”及各自校验链接，解释共享数据、先退出再升级、卸载保留数据、自启动切换与未签名状态。
6. 继续拒绝覆盖正式 Release、不同提交的 tag/草稿；同提交草稿可重试。正式发布仍由维护者在 GitHub 确认，本设计不授权实际发布。

## 9. 具体文件变更与实施顺序

以下均为**后续实施计划**；本次实际只新增 `INSTALLER_DESIGN.md`。

| 阶段 | 文件 | 具体职责及依赖 |
| --- | --- | --- |
| A 身份与脚本 | 新增 `packaging/windows-installer.iss` | 固定 AppId、当前用户路径、快捷方式、版本比较、锁与占用保护、升级/卸载归属清理；先确定路径和锁兼容策略 |
| A 工具链 | 新增 `packaging/installer-toolchain.json` | 固定 ISCC 版本、官方来源、经核实的 SHA-256；由构建脚本与 CI 共同读取 |
| B 构建 | 新增 `scripts/build_installer.py` | 接收 onedir/版本/输出目录/ISCC；复用原审计并验证 manifest、调用 ISCC、输出 Setup/hash/安装器 manifest；依赖 A 和便携产物 |
| B 现有打包 | `scripts/build_portable.py`、`packaging/windows.spec` | 保持 onedir 和白名单；按需补共享指纹规则、安装版说明资源；不把整个包装目录打进 payload |
| C 安装验收 | 新增 `release_tests/test_installer_bundle.py`、`release_tests/test_installer_lifecycle.py`、`scripts/acceptance/installer_acceptance.ps1` | 校验身份/清单、真实安装/同版修复/后续版升级/卸载/共用实例/并发；脚本只用于测试环境，不是用户端依赖 |
| C 验收调度 | `scripts/run_release_tests.py`、`scripts/build_acceptance.py`、`scripts/acceptance/desktop_acceptance.py` | 接入实际安装产物和报告；沿用冻结应用验收，确保来源指纹一致，不用便携换目录测试冒充安装升级 |
| D 发布校验 | `scripts/release_portable.py`、`release_tests/test_release_workflow.py`、`release_tests/test_release_privacy.py` | 双附件、双摘要、安装报告门禁、说明与隐私回归；保持 draft-only 边界 |
| D CI | `.github/workflows/windows-portable.yml` | 固定工具链安装、编译、安装验收、传递新增附件；保留权限隔离与同 run/attempt 限制，名称可改为 Windows distribution draft |
| E 版本与文档 | `bagu_trainer/version.py`、`README.md`、`WINDOWS_PORTABLE.md`、`PORTABLE_README.md`、`STARTUP.md`、`CLEAN_WINDOWS_ACCEPTANCE.md`、`VALIDATION.md` | 实施完成后统一到 0.3.0/tag 默认值，补安装/升级/卸载、自启动说明与实际验证记录；便携说明保留 |

`bagu_trainer/paths.py`、`config.py`、`secrets.py`、`desktop_instance.py`、`login_startup.py`、`desktop.py` 的现有运行规则是兼容基线，计划无需修改。若锁兼容或其他验收证明必须修改运行模块，应单独提出实现范围和回归依据，不将其视为本文已批准的隐藏改动。

## 10. 验收标准

所有实际安装、卸载、注册表和 Startup 测试在一次性 Windows VM/Sandbox 或专用测试账户完成，不在开发者日常账户试装。临时 `BAGU_USER_DATA_DIR` 只能隔离应用数据，**不能隔离安装登记和系统快捷方式**，不能代替 VM/测试账户。

纯自动化使用合成数据、内存凭据或独立 profile 的测试凭据、本地 HTTP 模拟服务。测试默认数据路径与默认 service 的兼容性时使用专用测试账户的合成 Key，不读取开发者账户真实 Key；不记录 Key 值或认证文件内容。测试自启动使用临时目录；真实 Startup 与登录验证仅在一次性测试系统执行。

| 场景 | 必须满足的结果/证据 |
| --- | --- |
| 当前文档交付 | 工作树仅新增本文；无运行模块/版本/CI 修改；不启动应用、不调用 Key 解析、不读个人配置或本机数据 |
| 标准用户首次安装 | 不出现 UAC、无管理员口令；默认目录准确；当前用户卸载条目与默认开始菜单入口存在；未选择桌面时无桌面入口 |
| 权限与架构 | Windows 10/11 x64 通过；x86 明确拒绝；不写 HKLM/公共快捷方式，不创建服务/任务/Run 项 |
| 中文、空格与只读安装目录 | EXE、Tk/主题、凭据 backend 等资源正常；程序目录无运行数据写入；用户数据均进入既有用户根 |
| 路径冲突与别名 | 默认两目录无包含关系；相同、父子、大小写变体、junction、自定义 `/DIR`、显式 profile 冲突均在写文件前拒绝；错误目标和原数据不变 |
| payload 一致性与隐私 | 安装后受管文件 hash 对齐 ZIP manifest；完整 `_internal`、许可存在；无个人数据、明文 Key、认证文件、题库正文或测试工具混入 |
| 已有便携用户首次安装 | 使用合成已有配置、题库、学习记录；正常退出便携版后安装，安装版直接读取原记录，不重置/覆盖；便携原目录保持原样 |
| 跨形式单实例 | 便携→安装、安装→便携、安装→源码三方向均验证；初始化和主窗口阶段只一个正常主实例，第二次前台启动唤起，后台重复启动不另建引擎 |
| 锁与维护并发 | 实际 EXE 与安装器字节锁互斥；在程序运行、安装过程中尝试启动、双安装、安装/卸载并发均无并行写程序或删数据；静默忙碌时非零退出 |
| 辅助窗口与文件占用 | 独立配置窗口、其他 profile 的同一安装 EXE、尚未退出的进程占用时升级/卸载安全停止；不强杀、不留下混合文件版本 |
| 同版修复与未来升级 | v0.3.0 同版修复、相同 AppId 的测试后续版本升级后仅一个卸载条目；目录和桌面选项沿用；旧受管残留清理，未知文件保留；降级拒绝 |
| 数据保留 | 应用关闭时记录合成配置/题库/SQLite 等 hash；安装、升级、卸载前后业务文件不变；运行后用记录内容验证历史/掌握度/复习信息保持；外部 DB 不触碰 |
| 凭据兼容 | 同一测试账户下便携/安装版仍使用同 service/user；安装/升级/卸载不调用凭据 CRUD，不打印 Key；重装后仍可使用合成凭据 |
| 自启动归属 | 未启用不新增；本安装入口升级保持、卸载移除；便携/源码入口不覆盖不删除；中文目标及参数正确；VM 真实重启登录后台启动符合原规则 |
| 卸载与重装 | 默认保留全部数据和凭据；仅删除受管程序/入口/登记；重装或继续启动便携版能恢复记录，不自动导入或删除旧目录 |
| 失败、取消与恢复 | 安装取消、空间/权限不足、文件占用、暂存或复制失败均不报成功，不删除数据，锁释放；旧版可用或同版修复可恢复；静默无意外启动 |
| 发布阻断 | 错 hash、脏提交、错 tag/AppId/输入清单、不同 run 附件、缺安装报告或失败/跳过均被拒绝；正式 Release 和其他提交 tag 不受修改 |

原 `release_tests`、冻结应用验收继续通过，新增安装器测试在发布 runner 上不得因“找不到 Setup”静默跳过。Windows runner 的开发工具环境不能代替全新 Windows 验收；Windows 10、Windows 11、无 Python/Git、实际安装向导和重启登录的实机证据及未覆盖项写入 `VALIDATION.md`。尚未执行的安装器验收一律标记“待验证”。

## 11. Inno Setup 官方依据

- [PrivilegesRequired](https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm)：`lowest` 使用非管理员安装模式。
- [AppId](https://jrsoftware.org/ishelp/topic_setup_appid.htm)：同一身份用于后续卸载日志及登记识别；GUID 转义参见 [Constants](https://jrsoftware.org/ishelp/topic_consts.htm)。
- [UsePreviousAppDir](https://jrsoftware.org/ishelp/topic_setup_usepreviousappdir.htm)：沿用同一应用此前登记的目录。
- [AppMutex](https://jrsoftware.org/ishelp/topic_setup_appmutex.htm)：需要应用本身创建对应命名 mutex，不能直接识别本项目现有文件锁。
- [CloseApplications](https://jrsoftware.org/ishelp/topic_setup_closeapplications.htm)：自动关停策略与静默安装行为，因此本方案关闭自动关停并使用显式维护保护。
- [ArchitecturesAllowed](https://jrsoftware.org/ishelp/topic_setup_architecturesallowed.htm)：使用明确 x64 OS 限制，避免将 ARM64 仿真作为已验收支持范围。
- [官方工具下载](https://jrsoftware.org/isdl.php)：工具链选定 Inno Setup 6.7.3；实施时还须核验并固定真实下载摘要。
