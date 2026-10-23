# Windows x64 安装与构建

## 普通用户安装

在 Release 的 Assets 优先选择 `BaguTrainer-<版本>-windows-x64-Setup.exe` 及同名 `.sha256`。
核对 SHA-256 后双击，阅读许可协议，确认程序目录与可选桌面入口，完成后从开始菜单启动。
默认只为当前 Windows 用户安装，无需管理员权限，也无需 Python、Git、PowerShell 或 Inno Setup。
草稿附件仅授权用户可下载；正式发布由维护者明确操作完成。

升级时正常退出训练器，再运行同一应用的新 Setup；卸载通过 Windows“已安装的应用”进入。
卸载会保留本机配置、题库、学习记录与 Windows 凭据。详细行为见下文生命周期说明。
便携 ZIP 仍可选，完整解压后运行；两种形式共用个人数据和单实例规则。

以下工具和命令仅供开发者构建/验收，普通用户不需要执行。

产品版本只维护 `bagu_trainer/version.py`，当前为 0.3.0。安装器入口为
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
.\.venv-build\Scripts\python.exe scripts/build_portable.py
.\.venv-build\Scripts\python.exe scripts/build_installer.py `
  --iscc 'C:\path\to\Inno Setup 6\ISCC.exe' `
  --innounp 'C:\path\to\innounp.exe'
```

默认输入 `dist/BaguTrainer/`，可用 `--bundle` 指定其他生产 onedir；
默认输出 `dist/`，可用 `--output-dir` 更改。缺少工具、编译器不匹配、
生产目录版本/来源过期或审计不通过时返回非零并给出修复说明。
PyInstaller 的 Tk 探测失败也会立即中止，避免生成不能打开窗口的包。

图标的可重复生成源为 `packaging/windows_assets.py`，不依赖字体或网络。
`BaguTrainer.ico` 同时用于生产 EXE、Setup、卸载入口和快捷方式；
修改图标源后先运行 `python packaging/windows_assets.py` 再重建 onedir。
PE 产品名、文件版本和产品版本通过该模块由 `version.py` 派生。

## 输出与安全边界

- `BaguTrainer-0.3.0-windows-x64-Setup.exe`。
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
用户数据仍在 `%LOCALAPPDATA%\BaguTrainer`，不触及真实凭据。
开始菜单默认创建，桌面快捷方式可选；完成页“立即启动”默认不勾选，静默不启动。
生命周期实现在 `packaging/windows-installer-lifecycle.iss`，由生产安装器与隔离验收共用。

## 升级、卸载和快捷方式

安装和卸载均取得现有 `runtime/owner.lock` 第 0 字节维护锁，并检查受管程序文件是否被占用。
检测覆盖默认数据目录和当前安装进程显式设置的绝对 `BAGU_USER_DATA_DIR`；
保持锁至文件维护结束。应用没有命名互斥量，因此不设置 `AppMutex`；`SetupMutex` 仅用于安装器互斥。
交互安装/卸载遇到占用时显示中文正常退出提示，每 500 毫秒检查一次，最长等待 120 秒，可取消。
请通过训练器的正常退出入口关闭主实例；只关窗口而保留后台进程仍会阻止维护。
取消或超时将停止维护，不更新程序文件。静默模式默认立即失败；
显式传入 `/MAINTENANCEWAIT=60` 可等待 60 秒，上限 300 秒。
不按进程名查杀，不自动向应用发送退出指令，也不使用重启后替换占用文件的方式。

固定 AppId 识别升级；升级沿用原目录与桌面任务选择，拒绝降级及直接搬动安装位置。
按安装文件清单更新程序、清理淘汰文件并备份旧文件；遇到未知文件冲突即停止。
新安装仅接受空的专用目录，拒绝覆盖便携目录或与个人数据目录重叠的位置。
安装器不解析个人配置，不迁移题库或数据库，不访问 Windows 凭据。

开始菜单入口默认创建，桌面入口按用户选择创建或移除。每次修改/删除入口前，使用原生
Unicode Shell Link 接口核对 EXE 目标和参数属于本安装，随后统一修正工作目录和图标。
已启用且目标属于本安装的 `BaguTrainer.lnk --background` 登录入口会得到修复；
未启用时不会创建入口。指向便携版/其他副本、损坏或无法确认归属的同名入口均保留，
不抢占它们；需要用户通过对应副本的设置调整。兼容旧安装器的 `[Icons]` 卸载记录，
卸载前备份已改指向其他副本的入口，避免旧记录误删，卸载后恢复。

卸载只删除本安装注册、受管程序和归属核对通过的入口，不递归清空目录；未知文件保留。
卸载界面说明默认保留 `%LOCALAPPDATA%\BaguTrainer` 的配置、题库、学习记录及日志，
也保留显式数据目录、自定义数据库和 Windows 凭据管理器中的 API Key。
没有删除个人数据选项，便携版目录不会被修改。

## 验证范围

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

这些流程不覆盖本机客户端、真实快捷方式、个人数据或 Key，也不会调用 GitHub 发布。
该工作区产物记录 `source_dirty=true`，属于本地未签名验证包。
隔离夹具已验证上述升级/卸载场景；生产完整包的默认系统目录、全部向导页面、
故障注入恢复、原生 DPI 和重启登录仍须在一次性 Windows VM 中验收。
不能用夹具验收替代完整目标系统验收。安装器现已接入原 GitHub 工作流，
包含最终 Setup 的临时托管 runner 试装门禁；尚未运行的 CI 检查不会写成通过。
双产物、脱敏附件、权限与草稿发布流程见 [Windows 发布](WINDOWS_PORTABLE.md)。
