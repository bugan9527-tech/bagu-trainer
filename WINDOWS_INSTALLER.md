# Windows x64 安装、维护与构建（0.4.x Tk 时代记录，安装器共用）

> 版本范围：本文记录 0.4.0 Tk 时代的安装/升级/卸载细节，保留历史事实；安装器脚本目前仍被 Electron 0.5.0 复用。当前 Electron 载荷、使用和验证边界见[项目仓库 docs/ELECTRON_DESKTOP.md](https://github.com/bugan9527-tech/bagu-trainer/blob/main/docs/ELECTRON_DESKTOP.md) 与[项目仓库 docs/ELECTRON_VALIDATION.md](https://github.com/bugan9527-tech/bagu-trainer/blob/main/docs/ELECTRON_VALIDATION.md)。真实安装、覆盖升级、登录自启动和完整卸载仍须另行实测，不因本文或构建审计自动通过。

## 普通用户安装

在 Release 的 Assets 优先选择 `BaguTrainer-<版本>-windows-x64-Setup.exe` 及同名 `.sha256`。
核对 SHA-256 后双击，阅读许可协议，确认程序目录与可选桌面入口，完成后从开始菜单启动。
默认只为当前 Windows 用户安装，无需管理员权限，也无需 Python、Git、PowerShell 或 Inno Setup。
草稿附件仅授权用户可下载；正式发布由维护者明确操作完成。

升级时正常退出训练器，再运行同一应用的新 Setup；卸载通过 Windows“已安装的应用”进入。
主动卸载会调用 BaguCleanup.exe 清除当前用户受管配置、题库、学习记录及本机 Windows 凭据；失败保留程序和重试入口。升级或修复不调用数据清理。详细行为见下文生命周期说明。
便携 ZIP 仍可选，完整解压后运行；两种形式共用个人数据和单实例规则。

### 启动、准备与练习

0.4.x Tk 兼容版从开始菜单启动即直接进入唯一正式工作台，显示完整七项侧栏，不打开首次使用向导。
Setup 的安装向导与应用使用准备是两回事；安装完成后在以下页面完成准备，无强制顺序：

1. “题库管理”：选择来源、阅读许可声明，点击“构建 / 重新构建”；失败可“仅重试失败项”。
2. “模型配置”：填写“接口地址”“模型名称”“API Key”“超时（秒）”，主动“测试连接”成功后“保存”。
   地址通常含 `/v1`，不含 `/chat/completions`；超时须大于 0 且不超过 120 秒。测试可能计费，修改参数后须重新测试。
3. “监听项目”：点击“添加项目文件夹”选择至少一个可读取的代码目录，再“保存并应用”。

准备齐全后在原窗口加载引擎，必要时回工作台点“重新检查 / 加载引擎”。
文件修改稳定约 15 秒后匹配出题，新题只更新徽标，不抢当前页面。进入“练习中心”，
选中待答题并“进入本题”，选择 A–D 后“提交答案”，在同页“作答结果与解析”查看结果。

缺配置时仍保留主窗口，回对应页面修复；删除最后一个来源后监听与练习停用，
在“题库管理”重新选择来源并“构建 / 重新构建”即可继续准备。已确认删除的关联历史不会因重建恢复。
损坏的 `config.json` 不会被默认配置覆盖，先退出并备份，再修复格式或权限。
详情见 [首次使用与修复](FIRST_USE.md)。

### 旧数据迁移

0.4.0 没有向导迁移按钮。先退出新旧程序，在当前安装目录的 PowerShell 中运行：

```powershell
.\BaguTrainer.exe --migrate-from "D:\旧目录\bagu trainer"
```

默认安装目录是 `%LOCALAPPDATA%\Programs\BaguTrainer\`；选择过其他位置时以“应用设置”的程序目录为准。
迁移复制并校验数据、保留旧目录，目标已有配置、题库或学习数据时拒绝覆盖，不能删除唯一副本绕过检查。
便携版在解压后的程序目录执行同一 EXE 参数；源码版命令见 [README](README.md#换目录或换电脑)。
迁移成功后回同一工作台核对题库、模型与监听项目。

以下工具和命令仅供开发者构建/验收，普通用户不需要执行。

产品版本只维护 `bagu_trainer/version.py`，当前为 0.4.0。安装器入口为
`packaging/windows-installer.iss`；AppId 永久固定为
`{C95D2707-83B5-4D34-A88D-7F30C5AC6E19}`。

## 构建工具

使用现有 Windows x64 Python 独立构建环境与 `requirements-build.txt`，另准备
`packaging/installer-toolchain.json` 固定的 Inno Setup 6.7.3 和 innounp。
编译器与解包工具只在构建端使用，不进入应用发布包。

Inno Setup 官方安装包 URL、SHA-256、编译器 SHA-256 和中文语言文件来源均在锁文件中。
下载后先核对摘要。可使用现有编译器，或将工具解包到项目 `build/installer-tools/`；
不必实际安装编译工具，不需要试装训练器。
中文语言文件已按官方仓库的固定 tag 收录在 `packaging/languages/`，保留上游署名。
CI 使用 `python scripts/prepare_installer_tools.py` 读取锁文件下载、逐级校验并解包到
`build/installer-tools/`，不安装编译工具。

```powershell
.\.venv-build\Scripts\python.exe scripts/build_portable.py --output-dir $buildOutput
.\.venv-build\Scripts\python.exe scripts/build_installer.py `
  --output-dir $buildOutput `
  --iscc 'C:\path\to\Inno Setup 6\ISCC.exe' `
  --innounp 'C:\path\to\innounp.exe'
```

默认输入 `dist/BaguTrainer/`，可用 `--bundle` 指定其他生产 onedir；
`--output-dir` 同时指定生产输入的父目录与 Setup 输出目录，并与便携构建及验收共用。
`$buildOutput` 使用 [便携构建入口](WINDOWS_PORTABLE.md) 中新建的目录；已有 Setup 或清单拒绝覆盖。
未配置时仍使用 `dist/`，也兼容 `BAGU_PORTABLE_*`。缺少工具、编译器不匹配、
生产目录版本/来源过期或审计不通过时返回非零并给出修复说明。
PyInstaller 的 Tk 探测失败也会立即中止，避免生成不能打开窗口的包。

图标的可重复生成源为 `packaging/windows_assets.py`，不依赖字体或网络。
`BaguTrainer.ico` 同时用于生产 EXE、Setup、卸载入口和快捷方式；
修改图标源后先运行 `python packaging/windows_assets.py` 再重建 onedir。
PE 产品名、文件版本和产品版本通过该模块由 `version.py` 派生。

## 输出与安全边界

- `BaguTrainer-0.4.0-windows-x64-Setup.exe`（版本由 `version.py` 生成；产物是否已构建或验收以报告为准）。
- 同名 `.exe.sha256`。
- `INSTALLER_MANIFEST.json`：输入清单摘要、源码来源、固定 AppId、工具版本、
  构建输入 hash、安装器 hash 和解包检查结果。
- 可选 `INSTALLER_LIFECYCLE.json`：通过 `--lifecycle-report` 关联的隔离生命周期验收。
- 原便携 ZIP、校验文件及完整 onedir 保留。

构建器先复用便携目录审计，核对 manifest 的完整文件集合、大小、hash、
版本和当前源码指纹，再静态检查 PE 图标/版本资源及 PyInstaller 归档入口。
测试/验收模块与驱动、配置、Key 文件、数据库及辅助文件、日志及轮转文件均拒绝收录。
冻结快照后以精确文件列表交给 ISCC，不使用仓库或 `dist` 的递归通配符。

编译成功后使用 innounp 解包到临时目录，核对每个 payload 文件的大小和 SHA-256，
并检查恢复脚本中的产品、版本、AppId、当前用户安装及默认目录，核对许可文本。
仅全部通过后才写入最终 EXE、SHA-256 和报告。
除完整生产目录外，安装器仅增加 `BaguTrainer.install-files.txt` 受管文件清单与 Inno
生成的向导/卸载资源；清单用于后续升级清理和失败恢复，不是个人数据。

安装器默认安装到 `%LOCALAPPDATA%\Programs\BaguTrainer`，
用户数据默认在 `%LOCALAPPDATA%\BaguTrainer\`；安装、升级与修复不清除数据或本机凭据。
开始菜单默认创建，桌面快捷方式可选；完成页“立即启动”默认不勾选，静默不启动。
生命周期实现在 `packaging/windows-installer-lifecycle.iss`，由生产安装器与隔离验收共用。

## 升级、卸载和快捷方式

安装和卸载均取得现有 `runtime/owner.lock` 第 0 字节维护锁，并检查受管程序文件是否被占用。
检测覆盖默认数据目录和当前安装进程显式设置的绝对 `BAGU_USER_DATA_DIR`；
保持锁至文件维护结束。应用没有命名互斥量，因此不设置 `AppMutex`；`SetupMutex` 仅用于安装器互斥。
交互安装/卸载遇到占用时显示中文正常退出提示，每 500 毫秒检查一次，最长等待 120 秒，可取消。
关闭训练器主窗口后等待安全退出完成；只最小化窗口或仍有后台任务收尾时，进程占用会阻止维护。
取消或超时将停止维护，不更新程序文件。静默模式默认立即失败；
显式传入 `/MAINTENANCEWAIT=60` 可等待 60 秒，上限 300 秒。
不按进程名查杀，不自动向应用发送退出指令，也不使用重启后替换占用文件的方式。

固定 AppId 识别升级；升级沿用原目录与桌面任务选择，拒绝降级及直接搬动安装位置。
按安装文件清单更新程序、清理淘汰文件并备份旧文件；遇到未知文件冲突即停止。
新安装仅接受空的专用目录，拒绝覆盖便携目录或与个人数据目录重叠的位置。
Setup 安装、升级和修复不迁移题库或数据库、不执行完整数据清理；
主动卸载则显式调用 `BaguCleanup.exe` 核对并清除受管数据和本机 Windows 凭据。
不要为保留数据而“先卸载再升级”或“先卸载再搬家”；升级须使用原安装位置。

开始菜单入口默认创建，桌面入口按用户选择创建或移除。每次修改/删除入口前，使用原生
Unicode Shell Link 接口核对 EXE 目标和参数属于本安装，随后统一修正工作目录和图标。
已启用且目标属于本安装的 `BaguTrainer.lnk --background` 登录入口会得到修复；
未启用时不会创建入口。指向便携版/其他副本、损坏或无法确认归属的同名入口均保留，
不抢占它们；需要用户通过对应副本的设置调整。兼容旧安装器的 `[Icons]` 卸载记录，
卸载前备份已改指向其他副本的入口，避免旧记录误删，卸载后恢复。

### 主动完整卸载

安装版可从 Windows“已安装的应用”启动卸载，或在“应用设置”点击“卸载应用并清除全部数据…”，
核对清理范围后点“确认退出并进入完整卸载”。主程序安全退出后交接系统卸载器和独立清理工具；
在工具中点击“确认完整卸载 / 重试”才执行清理。可通过“补充旧数据位置”加入未登记的旧自定义目录，仍需核对归属。

0.4.0 主动卸载清除受管题库及历史版本、待答与作答记录、题目快照、配置、窗口状态、日志、
已确认归属的自定义学习库及本机 API 凭据；随后移除本安装注册、受管程序和归属确认的入口。
没有旧版“卸载默认保留个人数据”的语义。安装版、便携版和源码版默认共用数据，其他副本再次启动会进入缺配置状态。

只处理能确认归属的受管对象，不递归清空共用目录或监听项目，不整体删除源码仓库及其他便携副本的程序。
用户级 `BAGU_LLM_API_KEY`、`BAGU_USER_DATA_DIR` 环境配置也在完整清理范围；
系统级变量无法自动清除时会提示手动处理，其他已运行进程继承的环境变量不受影响。
本机凭据清除不代表吊销服务商后台的 Key。

清理失败或取消时保留程序或重试入口并显示残留；按提示解除占用、修复权限后重试。
**失败不代表已经清除的数据会回滚**，升级/修复如需保留数据不要进入此流程。
便携版从同一应用设置入口交接临时清理工具；源码版只清除受管数据和凭据，保留源码与虚拟环境。
仅手动删除便携目录或源码目录不等于完整卸载。

## 验证方法与历史范围

以下命令是验证入口，不表示本次文档核对已执行安装或卸载。历史证据按 [VALIDATION.md](VALIDATION.md)
中的版本和夹具范围阅读，旧向导、卸载保留数据的结果不能转用为 0.4.0 通过结论。

```powershell
python -m pytest release_tests/test_installer_bundle.py `
  release_tests/test_release_privacy.py release_tests/test_release_workflow.py -q -W error
```

构建与解包检查不执行生产 Setup、不启动生产应用。生命周期测试使用单独编译的合成安装夹具：

```powershell
python scripts/acceptance/installer_lifecycle.py `
  --iscc 'build/installer-tools/inno/{app}/ISCC.exe' `
  --work build/installer-lifecycle-new-run
```

`--work` 必须是项目 `build/` 下尚不存在的新目录。夹具共用上述 Pascal 生命周期代码，
使用随机测试 AppId，编译时将安装、数据、桌面、开始菜单和 Startup 全部重定向至测试目录。
仅在 HKCU 注册该测试 AppId，完成后验证对应卸载注册已删除；不使用生产 AppId。
数据为临时配置、合成题库/SQLite、凭据标记文件，不调用 Windows 凭据接口。
占用测试使用真正的 `OwnerLock` 和合成 EXE，正常释放/退出；同名便携夹具持续运行。
两组场景覆盖本安装入口和被改指向便携版的入口，包括旧版图标卸载记录，
并实际验证等待窗口在锁释放后继续安装。报告保存在工作目录。

构建时可添加 `--lifecycle-report build/installer-lifecycle-new-run/INSTALLER_LIFECYCLE.json`。
构建器核对报告通过、两组场景完整、注册清理完毕及两个 `.iss` 的当前摘要，
过期报告拒绝关联；通过后把摘要写入构建报告，并复制验收报告至输出目录。

这些隔离流程不覆盖本机客户端、真实快捷方式、个人数据或 Key，也不会调用 GitHub 发布。
历史 0.1.0 → 0.2.0 合成夹具验证了当时的升级、入口归属及卸载保留语义；
测试夹具不执行生产完整凭据清理，不能证明 0.4.0 的完整卸载已通过。
本地脏源码构建会记录 `source_dirty=true`，不满足干净提交发布门禁。
0.4.0 生产完整包的默认系统目录、完整卸载及故障恢复、原生 DPI 和重启登录仍需对应版本的实际验证。
不能用夹具验收替代完整目标系统验收。安装器现已接入原 GitHub 工作流，
包含最终 Setup 的临时托管 runner 试装门禁；尚未运行的 CI 检查不会写成通过。
双产物、脱敏附件、权限与草稿发布流程见 [Windows 发布](WINDOWS_PORTABLE.md)。

0.4.0 最近的工作区产物路径修复证据 `validation/artifact-path-fix-20261025/RESULTS.md`
（未随仓库或发布包分发）记录 148 项相关回归通过，
完整冻结 UI 验收仍报 `No acceptance milestones completed`；没有据此生成新的 Setup 或完成正式发布。
本次仅校正文档，不新增验收通过结论。

## 0.4.0 真实 Setup 生命周期验收入口

操作说明见 [真实安装生命周期验收](INSTALLED_LIFECYCLE_ACCEPTANCE.md)。现有 `installed_release.py`
已补齐真实旧 Setup 升级、同版修复、原生测试凭据、完整清理、数据删除故障与重试、卸载后重装六项检查；
`installer_lifecycle.py` 保持为隔离合成夹具，不替代真实安装结果。
当前没有可用的隔离执行环境，六项真实场景均未执行；本地仅有脚本回归与包摘要预检，不能标记安装通过。
自动真实执行仍只允许空的 GitHub 托管一次性 Windows 账户，不对日常用户环境开放绕过参数。
