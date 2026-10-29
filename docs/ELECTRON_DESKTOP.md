# Electron 桌面版（0.5.1 当前桌面说明入口）

本文件是 **v0.5.1 Electron 桌面版**的使用与开发入口。桌面界面使用 Electron + React + TypeScript + Vite；Python 继续负责代码监听、知识点匹配、AI 出题审核、题库构建和 SQLite 学习记录。

旧版 0.4.x Tk 兼容说明保留在仓库根目录的同名文档中，并在 README 的“旧版 Tk 兼容说明（归档）”集中标注；那些 `start_desktop.py`、`--install-startup`、`--migrate-from` 命令不是当前 Electron 行为。

## 当前版本与运行结构

后端目录、依赖方向、状态所有权、并发和兼容范围见 [架构说明](ARCHITECTURE.md)，实际重构验收见 [第 8 步记录](BACKEND_REFACTOR_STEP8.md)。

- 版本来源：`package.json` 与 `bagu_trainer/version.py` 均为 `0.5.1`。
- 界面：左侧七页导航 + 右侧工作区；练习使用唯一的独立答题窗口。
- 运行链路：React → 受限 preload → Electron 主进程 → JSON 行协议（stdin/stdout）→ Python `DesktopService` → `StartupCoordinator` / `DesktopMonitor` / learning engine。
- Python 运行时使用 `%LOCALAPPDATA%\BaguTrainer\` 保存配置、题库、学习记录、日志和运行认证信息；程序目录可以只读。

## 从源码运行

需要 Windows x64、Python 3.10+、Node.js 22.12+。在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:BAGU_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
npm ci
npx install-electron --no
npm start
```

- `npm start` 先执行 TypeScript 检查和 Vite 生产构建，再启动 Electron。
- 开发界面可用 `npm run dev`，仅在本机 `127.0.0.1` 开启 Vite；正式程序不启动 Vite，也不要求用户安装 Node.js。
- 默认使用 `.venv-build\Scripts\python.exe`（存在时）或 PATH 中的 `python`；设置 `BAGU_PYTHON` 可指向实际解释器。
- Electron 正常学习流程不导入 Tk 窗口；完整卸载工具仍使用 Tk 确认窗口，因此源码版完整卸载、冻结 `BaguCleanup` 构建和旧版 Tk 兼容包需要 Tcl/Tk。
- Python 依赖请安装到实际运行后端的解释器；修改解释器后重新运行同一组命令。

## 便携包结构

Electron 便携 ZIP 必须**完整解压**，保留根目录全部文件，不能只复制 `BaguTrainer.exe`：

```text
BaguTrainer.exe          Electron 主程序
BaguBackend.exe          Python 学习引擎
BaguCleanup.exe          完整卸载与残留重试工具
_internal\               Python 引擎运行库（含 Tcl/Tk、许可文件）
resources\               Electron 资源（app.asar 等）
locales\、*.dll、*.pak    Electron 运行库
使用说明.md              本说明的打包副本
BUILD_MANIFEST.json      受管程序文件清单，供完整卸载核对
```

启动方式：完整解压后双击 `BaguTrainer.exe`。便携版与安装版共用同一份个人数据和单实例规则，不需要安装 Python、Node.js 或 Tcl/Tk。

升级时正常退出旧程序，把新版完整解压到新目录后运行新 EXE；不要用“完整卸载”代替升级，也不要只覆盖主 EXE。

## 安装版

普通用户推荐 Windows x64 安装包 `BaguTrainer-<版本>-windows-x64-Setup.exe`：

- 中文安装向导，当前用户安装，默认无需管理员权限。
- 默认程序目录：`%LOCALAPPDATA%\Programs\BaguTrainer\`。
- 个人数据目录：`%LOCALAPPDATA%\BaguTrainer\`。
- 升级或同版修复保留程序、数据与 Windows 凭据；主动完整卸载会清除受管数据及本机凭据。
- 完整卸载使用独立的 `BaguCleanup.exe`，需要两阶段确认；升级时不要先卸载。

安装器与 Electron 载荷的构建边界见“构建、发布与验证”；真实 Windows 安装、覆盖升级、登录自启动和完整卸载仍须在本机实测，不能仅由构建或解包审计推断通过。

## 首次使用

每次启动直接进入同一个正式工作台，完整显示七项侧栏，不打开独立首次使用向导：

1. **题库管理**：选择来源，阅读许可声明，点击“下载并构建”；可设置任务总时限、取消构建或仅重试失败来源。
2. **模型配置**：填写接口基础地址、模型名称、API Key、超时（秒）；主动“测试连接”成功后点击“保存配置”。Key 写入 Windows 凭据管理器，输入框不回填明文。
3. **监听项目**：点击“添加项目”，选择实际代码目录，再点击“保存并应用”。启动或新增项目会先建立文件基线。

三项没有强制顺序；缺项时仍保留七页工作台，在对应页面修复。引擎加载失败时回到工作台点击“重新检查”。启动检查不下载题库、不自动测试模型。

## 独立答题窗口与快捷键

代码改动稳定约 15 秒后匹配知识点；新题只更新待答徽标，不切走当前页面。

- 在主窗口“练习中心”选择待答题，点击题目行或“进入本题”，打开唯一的独立答题窗口。
- 选择 A–D，点击“提交答案”或按 `Ctrl+Enter`；查看判分、正确答案和解析。
- `Alt+N` 进入下一题；没有其他待答题时按钮显示“结束练习”，隐藏窗口。
- 关闭答题窗口只隐藏，当前题、未提交选择和判分保留；重新进入可恢复草稿。
- 主窗口始终可操作；`Ctrl+R` 打开或恢复答题窗口，表单输入时不拦截。
- 提交与切题互斥；同一题重复提交会被拒绝，不重复计数。
- 历史记录可查看解析，并“重新练习本题”；重练复用合格 AI 快照，不额外调用模型。

## 登录后自动启动

在“应用设置 → 登录后启动”启用或取消。快捷方式指向当前安装的 `BaguTrainer.exe`，参数为 `--background`；登录后窗口最小化启动，可从任务栏恢复。

- 仅打包后的安装版/便携版支持该设置；源码运行时复选框不可用。
- Electron 主进程只处理 `--background`；`--install-startup` / `--uninstall-startup` 是旧版 Tk 入口，不适用于当前 Electron。
- 移动程序目录后重新启用一次；凭据不随目录复制。

## 换目录或换电脑

当前 Electron 没有 `--migrate-from` 迁移命令，也没有“迁移旧数据”按钮。需要人工处理：

1. 退出所有 BaguTrainer 副本，备份 `%LOCALAPPDATA%\BaguTrainer\`。
2. 在新位置安装/解压程序，把备份复制到新用户的同一数据目录；确认没有其他副本占用。
3. 重新在“监听项目”核对绝对路径，在“模型配置”重新保存 API Key（Windows 凭据不随目录复制）。
4. 如需登录自启动，在新位置重新启用。

旧版 Tk 的 `--migrate-from` 和“迁移旧数据”向导只适用于 0.4.x 兼容版，见 README 的归档说明。

## 构建、发布与验证

```powershell
python -B scripts/check_unused_imports.py
npm run build
npm test
python -B -m pytest tests/test_desktop_service.py tests/test_electron_transport.py tests/test_engine_session.py tests/test_generation_concurrency.py tests/test_store.py tests/test_learning_loop.py tests/test_pending_flow.py tests/test_privacy.py release_tests/test_installer_bundle.py -q -W error
npm run test:desktop
npm run package:dir
npm run package:zip
npm run package:installer
```

- `python -B scripts/check_unused_imports.py` 检查 `bagu_trainer/` 和 `scripts/` 的未使用导入；兼容导出和 Tk 主题副作用导入在脚本内逐项列出理由，不使用全局忽略。测试/验收目录中的 fixture 导入仍需人工判断。
- `npm run build` 的 TypeScript 检查已启用 `noUnusedLocals` 与 `noUnusedParameters`；CI 使用同一构建命令。
- `package:dir` 生成 `output/electron/win-unpacked/`；`package:zip` 生成便携 ZIP；`package:installer` 使用现有 Inno Setup 生命周期封装并解包审计。
- `test:desktop` 使用自编合成题目、隔离数据和隔离 Chromium 目录，验证真实 Python 引擎与 Electron 窗口；可用 `BAGU_ELECTRON_EXECUTABLE` 指向打包后的 EXE。
- 当前 `.github/workflows/windows-electron.yml` 执行构建、核心测试、源码/冻结 EXE 验收并上传 artifact，**不创建草稿 Release**。`windows-portable.yml` 是 v0.4.x 旧 Tk 兼容工作流，仍可能创建草稿 Release；两者不能混用。
- `package:installer` 只构建和解包审计，不执行安装、升级或卸载；真实安装生命周期须另行实测。
- 产物未签名；模型连接与出题使用用户配置的接口，可能产生服务商费用。

## 数据与边界

- 模型出题会向配置的接口发送经过筛选、脱敏和截断的项目上下文及题库来源正文。
- 文件采集受项目白名单、扩展名白名单和敏感文件排除规则限制；会话摄取默认关闭。
- 新保存的 API Key 进入 Windows 凭据管理器；`config.json` 不保存新 Key 明文。
- 学习记录默认位于 `%LOCALAPPDATA%\BaguTrainer\data\trainer.db`；运行文件在 `runtime\`；滚动日志在 `logs\desktop.log`。
- 未配置模型、无可靠来源、模型输出不合格或审核未通过时允许没有题目；不会交付离线回退题。
- 代码改动稳定 15 秒只是触发近似，不能精确判断 AI 任务何时结束。

## 旧版 Tk 兼容

Tk 入口仍保留用于兼容测试和 v0.4.x 发布工作流，Electron 运行不依赖它。旧版操作说明（`FIRST_USE.md`、`DESKTOP_MODE.md`、`STARTUP.md`、`PORTABLE_README.md`、`WINDOWS_PORTABLE.md`、`UI_*.md` 等）已在 README 的归档区标注版本；不要把这些文档中的 `start_desktop.py`、`--install-startup`、`--migrate-from` 或 Tk 窗口行为当作当前 Electron 指南。
