# 后端分层重构第 8 步：目录与兼容验收

日期：2026-10-29。当前工作树含用户修改及前 7 步未提交结果；全部保留，没有自动提交、发布或覆盖已有发行目录。已使用 codebase-design 技能，核对基线、前两份阶段记录、当前调用链、脚本、CI 和打包配置。未发现适用的 AGENTS.md。

## 前 7 步核对与必要衔接

| 步骤 | 实际结果及本步处理 |
| --- | --- |
| 1 基线 | docs/BACKEND_REFACTOR_BASELINE.md 记录原五文件 31 项通过；保留其历史结果，不改写成新结果。 |
| 2 实例装配 | Engine、bootstrap.create_engine/get_engine 已存在；移入 application.learning.engine 后仍显式装配，导入不加载题库/创建 session。新增旧/新模块身份及生命周期测试。 |
| 3 学习 Interface | 生成、预留、代次、交付、作答、恢复及历史重练已在 Learning/EngineSession；根 engine 仍承载大量实现，本步把实现放入学习模块，根文件只保留兼容导入。 |
| 4 项目监听 | ProjectWatch 已持范围、读取器、基线、扫描/防抖，Activity 经 monitor 交给 Learning；本步放入正式 application 目录。旧测试仍伪造 engine.session/monitor 字段，本步改为真实 monitor + 公开学习替身并保持原断言。 |
| 5 设置/任务/展示 | settings、banks、commands、presenter 已抽取。发现 banks 反向导入 desktop.presenter 做序列化，改为 application.events；发现 uninstall.launch 漏导入 os，补齐并用模拟交接验证，没有实际卸载。 |
| 6 存储和纯规则 | SQL/连接、查询及备份集中于 Store，纯判分/复习/准入/匹配已存在；将 Store 和文件/HTTP adapter 放入 infrastructure，确定性标题/分类放入 domain。SCHEMA、配置默认值和命令集合与重排前基线对照通过。 |
| 7 内容维护 | application.maintenance 已负责批准、失效、恢复、重载和通知；具体日志/文件/SQLite 操作迁入 infrastructure，保持原实现。继续覆盖中断恢复、重复恢复、作答交错和迟到生成。 |

进一步修复了非生产代码的必要衔接：子进程探针内的导入、旧 Tk 测试替身缺失的公开方法、项目路径大小写规范化、通过学习公开关闭/重新初始化验证待答恢复、CI 单元 gate 的脚本夹具、合成隐私夹具的精确文件哈希。没有降低匹配阈值、延长测试时限或放宽断言。

## 最终变更

- 57 个原根模块/包的实现按 desktop/application/domain/infrastructure 安放，根目录仅保留 __init__.py、version.py、bootstrap.py 和 engine.py。
- DesktopService → desktop/service.py，DesktopMonitor → application/monitor.py，启动与旧 Tk UI → desktop；模型验证 → application/settings/verification.py，生成流程 → application/learning/generator.py，只读历史 → application/learning/history.py。
- Store、配置/凭据、题库加载、HTTP、来源/缓存/版本、具体删除日志、迁移和操作系统访问 → infrastructure；构建包成为 infrastructure/bank_build。
- 修复所有仓内 Python 导入、嵌入的子进程探针、脚本入口、三个 PyInstaller spec 的引用及 Electron 排除名单；CI 的 Tcl/Tk 探针更新为新路径。移动 paths.py 后资源根仍是原项目根，冻结模式不变。
- 增加明确兼容导出，更新现有未使用导入检查；普通 Electron 导入不会因 desktop 包探测子模块而加载 Tk。
- 扩展 electron/smoke.cjs 及合成夹具，用本地模拟 HTTP、合成来源 ZIP/cache 完成真实监听和出题；增加可用于源码/冻结后端的 scripts/acceptance/backend_probe.py。
- 新增 tests/test_architecture.py 和 tests/fixtures/backend_contract_baseline.json。更新 [架构说明](ARCHITECTURE.md)、Electron 文档入口和历史架构文档的当前说明链接。

没有删除仍受支持的旧 engine Interface。实际调用方仍为 scripts/acceptance/bank_lifecycle.py、scripts/check_ui_redesign.py；根 engine.py 将模块对象直接解析为 application.learning.engine，没有另一份 cfg/session。bootstrap、Electron、Tk、旧函数共享同一应用默认实例。旧 scorer/scheduler/quiz_validation 根导入的仓内调用方已全部迁移，移除这三份无调用方的转发文件。

详细目录、依赖方向、状态所有权、锁、任务通道和兼容范围见 ARCHITECTURE.md。本步没有重写算法、执行器或事务模型。

## 数据与协议兼容结论

| 对象 | 证据和结论 |
| --- | --- |
| config.json | 默认值 AST 哈希与 Git 中原实现一致；读改写事务、环境变量/凭据引用及用户目录不变。配置并发、迁移和启动测试继续执行。 |
| SQLite | SCHEMA 文本 SHA-256 与原实现一致；Store 原子作答、独立只读历史、备份、检查点及水合测试保留。没有新 schema 迁移。 |
| 题库 | 保留 flat/UUID 版本、questions/profiles/sources、来源缓存和 active-bank 指针格式；构建/重载/来源清理沿用原实现，固定合成匹配输出验证不变。 |
| 维护日志 | SourceDeletion schema 1、preparing/switching 回滚及 cleaning 向前完成不变；文件系统和 SQLite 仍分步操作，不宣称共同原子提交。 |
| 桌面协议 | 原命令集合的静态基线相同，JSON Lines 协议版本 1、响应/事件字段不变；源码及冻结后端真实管道和 Electron 渲染端验证通过。 |
| 生命周期 | 正常退出写入、关闭后拒绝旧结果、显式重新初始化、实例隔离/共享，以及暂停仍可作答继续验证。 |

## 实际验证与失败记录

使用现有系统 Python 3.10/pytest、Node 22、现有 .venv-build。Python 测试夹具均为临时目录、合成题库/SQLite/凭据和模拟模型；构建下载测试使用合成本地 HTTP。Electron 的模型生成和独立审核共 **2 次本地 HTTP 请求**，外部 HTTP/HTTPS 指向不可用的本地代理，避免误访真实接口。没有调用付费模型或清理真实用户数据。

### 核心基线与架构

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest tests/test_desktop_service.py tests/test_engine_session.py tests/test_generation_concurrency.py tests/test_electron_transport.py tests/test_store.py -q
python -m pytest tests/test_architecture.py -q
```

结果分别为 **39 passed、0 failed、0 skipped**（2.95 秒）与 **4 passed、0 failed、0 skipped**（0.77 秒）。最初基线同五文件为 31 passed；第 7 步结束时已扩展为 38，本步增加卸载交接导入回归。测试数增加，不表示重新定义旧失败。

### 完整源码/发布测试

所有 GUI 测试在批准的沙箱外运行，避免已确认的 Tcl/Tk 沙箱资源限制。原始输出保留在 validation/backend-refactor-step8/logs。

1. 首轮 `python -m pytest tests release_tests -q --tb=short`：**1090 passed、63 failed、31 skipped、16 errors**，372.90 秒。包含本步嵌入探针缩进错误、旧测试替身与已抽取 Interface 不匹配、失败窗口残留导致的后续 Tk 图片错误，以及现存 0.3.0 便携包与 0.5.0 源码不匹配的 5 项 fixture 错误。这些结果没有计作通过。
2. 修正导入和测试衔接后的针对性命令：`python -m pytest tests/test_architecture.py tests/test_desktop_service.py release_tests/test_dashboard_navigation.py release_tests/test_interaction_states.py release_tests/test_project_pruning.py release_tests/test_projects_panel.py release_tests/test_redesign_acceptance.py release_tests/test_due_review.py release_tests/test_first_use.py release_tests/test_startup.py release_tests/test_ui_layout.py release_tests/test_release_audit.py -q --tb=short`。中间一轮为 **153 passed、8 failed**；最终同组 **161 passed、0 failed、0 skipped**，82.66 秒。保留全部原业务断言，使用当前公开方法设置夹具，不向生产代码增加测试替身兼容分支。
3. 最终完整命令：

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest tests release_tests --ignore=release_tests/test_portable_bundle.py -q --tb=short
```

该混合进程组合结果为 **1164 passed、5 failed、31 skipped**，488.35 秒。4 项失败来自另两处旧夹具：启动恢复提示的 engine 替身缺少学习 Interface，以及 CI gate 夹具缺少检查目录；均已补齐。另 1 项是原有 0.15 秒题库批次总截止测试：第三个来源状态偶发为 failed，断言要求 cancelled；没有修改其时限、算法或断言。随后 `python -m pytest release_tests/test_bank_batch.py::test_total_deadline_stops_batch_and_retains_finished_cache release_tests/test_startup_guidance.py release_tests/test_unit_workflow.py -q --tb=short` 为 **18 passed、0 failed、0 skipped**，9.08 秒。该复测不抹去混合组合的 5 个失败。

按现有 CI 的单元/发布分组在独立进程完成最后验证：

```powershell
python -m pytest tests -q --tb=short
python -m pytest release_tests --ignore=release_tests/test_portable_bundle.py -q --tb=short
```

单元组：**299 passed、0 failed、31 skipped**，48.53 秒。发布组：**870 passed、0 failed、0 skipped**，454.37 秒。两个独立进程最终合计 **1169 passed、0 failed、31 skipped**；下述旧便携包的 5 项验收不在此合计中。

排除的 5 项是旧 Tk 便携包二进制验收：已实际尝试，但已有 dist/BaguTrainer 是 0.3.0，源版本为 0.5.0，fixture 拒绝加载。未覆盖旧发行目录来制造通过，也不将这 5 项计作通过。

31 个语料跳过项需要未提供的 BAGU_TEST_BANK_ROOT 外部语料，不计作通过。

### Electron 冒烟

```powershell
npm run build
npm test
$env:BAGU_PYTHON = (Get-Command python).Source
npm run test:desktop
```

TypeScript/Vite 构建通过；Node 协议策略测试 **4 passed、0 failed、0 skipped**。最终 Electron 命令退出码 **0**，19 个业务/安全/布局检查通过，另有正常退出 code 0 和首次使用检查。

证据目录：`validation/electron-1793209151264/`，含 RESULTS.json、EXIT.json、FIRST_USE.json 和截图。检查包括启动、七页导航、只读待答、隐藏答案、单答题窗口/草稿、重复提交、持久作答、历史重练、真实扫描及 15 秒稳定触发、本地模拟生成和审核、缓存离线构建、串行题库重载保留待答、暂停作答、最后来源清理及退出。

冒烟曾有两次验收脚本失败：单知识点合成题库未满足原匹配证据门槛，随后在关闭 Playwright 连接后读取 process 对象失败。分别通过增加无关主题的合成样本、提前保存进程引用修正。没有修改匹配算法或生产协议来绕过它们。

### 打包和冻结后端

```powershell
.venv-build/Scripts/python.exe -m PyInstaller --noconfirm --clean --distpath build/backend-refactor-step8/dist --workpath build/backend-refactor-step8/work packaging/electron-backend.spec
python scripts/acceptance/backend_probe.py
python scripts/acceptance/backend_probe.py --executable build/backend-refactor-step8/dist/BaguBackend/BaguBackend.exe
```

PyInstaller 构建成功。两个独立合成 profile 的源码/冻结后端探针均通过启动、作答、历史、重练、维护重载保留待答及正常退出，退出码 **0**。构建输出位于独立目录，没有覆盖已有发行文件。构建日志保留可选平台模块及被刻意排除旧 Tk 入口的警告；实际冻结 transport 已运行验证。

`python scripts/check_unused_imports.py` 通过（121 个文件、3 个逐项说明的兼容/副作用例外）；`python -m compileall -q bagu_trainer scripts tests release_tests` 和 `git diff --check` 通过，仅有既有 CRLF/LF 转换警告。

## 剩余风险与未执行验收

- 旧 Tk 便携包的 5 项二进制验收因版本不匹配未完成；没有重新制作旧便携包来替代已有发行目录。
- 未运行 Windows 安装器安装/升级/真实卸载、签名和发布验收，未构建新的 Electron 安装器或 ZIP；本次只构建并验证独立 Python 冻结后端。
- 未验证真实付费模型、第三方公网下载、外部完整匹配语料、大规模真实用户库。网络和内容均使用隔离模拟。
- 混合进程大组中出现过一次 0.15 秒批次截止状态断言波动，独立复测通过，原时限/状态算法未修改；后续仍需关注负载下的截止分类稳定性。
- 未穷举断电、磁盘满、跨卷外置库、杀毒软件长期占用和多进程每个文件操作之间的竞争；原备份、路径校验、构建租约和恢复日志继续负责这些失败保护。
- 支持的旧 engine 函数、contextvars 绑定和少量 Engine 历史方法仍在，后续移除必须先迁移两份验收脚本及其真实调用方。未登记的外部源码 import 路径不在兼容承诺内。
