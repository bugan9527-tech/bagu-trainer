# 后端分层重构第 7 步：内容维护与恢复

日期：2026-10-29。基于当前含第 1–6 步及用户未提交修改的工作树实施；没有重置、提交或清理真实用户数据。已阅读 codebase-design 技能、基线、来源删除、题库清理、构建租约、维护日志、启动、学习会话、两个桌面入口及卸载写入门禁；适用目录未发现 AGENTS.md。

## 公开 Interface 与装配

`application/maintenance.ContentMaintenance` 负责维护协议，接受清理 adapter、当前引擎提供函数、监听失效、完成通知、不可用通知及题库任务忙碌检查：

- `plan(kind, repositories, protected=()) -> MaintenancePreview`：检查范围和任务冲突，将实际计划及保护版本留在模块内，只返回预览和一次性 token。新预览使前一份 token 失效；修改预览数据不能改变已批准的删除对象。执行前仍由原实现精确重验文件、来源、活动指针和学习记录。
- `clean(token, include_unknown=False, progress=None) -> CleanupResult`：消费批准，失效监听回调和学习生成窗口，释放学习连接，协调原清理流程，重新载入存续内容，再通知调用方。来源不明记录仍须显式确认。计划失效、异常及部分失败不变成成功。
- `recover() -> CleanupResult | None`：没有日志且没有失败的重载时不做修改；有日志时执行原阶段恢复；运行时重载失败但日志已经移除时，重试磁盘题库重载。没有恢复成功不能继续学习。
- `apply_bank()`：在原串行命令/worker 通道应用已完成的题库；存在删除日志时拒绝应用，先要求恢复。保留已有待答，不新增删除事件或清空作答草稿。
- `recovery_pending()`：供旧 Tk 重试按钮判断；UI 不再识别日志路径。
- `close()`：等待当前维护完成，废弃计划，拒绝迟到恢复和应用；不会初始化引擎或打开旧会话。

Electron 的 DesktopService 与 StartupCoordinator 共用一份维护模块。`bank.plan`、`bank.clean` 只调用公开 Interface，命令名称、预览字段、`{ok, note}` 和 `content_changed` 的数据格式保持不变。BankWorkflow 在原串行 `prepare` 命令中交给维护模块应用，后台构建线程仍不直接操作学习状态。

Tk 的 BankPanel 使用同一维护模块生成预览、清理和恢复；原回调参数位置保留，传递的是维护模块和预览。worker 的独立上下文显式装配维护模块，维护回调只捕获 monitor，不捕获 Tk App。Tk 的 `bank_applied`、`bank_cleaned`、进度与错误事件仍由桌面 adapter 将返回值映射为原格式。

正常 StartupCoordinator.prepare 在就绪检查、引擎加载/初始化之前恢复。Electron prepare 也通过同一 Interface 保留“恢复失败向命令调用者抛错”的行为；随后的协调器检查在无日志时是幂等空操作。启动失败保留原脱敏错误报告。启动和恢复均不会为了恢复而提前装配第二套引擎。

## 维护协议与具体操作

具体实现继续分别位于 BankCleanupService、SourceDeletion、BankBuildService、bank_storage 与 Store。没有新增通用事务或 CRUD 框架，没有把文件系统和 SQLite 当成同一个原子事务。

来源删除日志仍为 `data/source-deletion.json`，schema **1**，字段、路径校验、原始指针、替代数据库/备份及 legacy 摘要格式保持不变：

1. 获取学习锁和构建租约，重验批准，先写 `preparing` 意图。
2. 使用本地来源构建未激活的保留版本，准备 legacy 重写与 SQLite 替代副本；不访问模型、不增加下载或模型自动重试。
3. 持久写入 `switching`，失效内容后逐项替换文件、检查点/移动数据库及切换活动题库指针，重载题库。
4. 持久写入 `cleaning` 后执行原版本、缓存、备份和临时对象删除，并核验剩余来源和学习关联。
5. 无失败才移除日志。部分失败返回 failures、保留日志供显式重试。原 Windows 占用重试、链接/路径/活动版本保护继续由具体实现负责。

恢复规则保持原实现：**cleaning 阶段向前完成；更早阶段回滚**。回滚恢复原数据库与题库指针，只在摘要符合预期时恢复 legacy 文件，不覆盖无关外部修改。重复恢复在日志移除后为空操作；不存在的清理对象仍按原规则处理。缓存、历史版本和临时对象清理仍采用原退休/删除机制，没有为它们发明新的持久日志。

## 锁、代次与失败规则

- 新维护模块的 RLock 只串行维护操作，不改变普通命令、读取、后台任务及 generation 执行器。
- 顺序：维护模块锁 → monitor reset（释放 monitor 锁）→ EngineSession RLock → 构建租约 → Store 锁/事务。原 monitor → session 的路径不变；在持有学习锁时不调用 monitor 或 UI 完成回调。题库任务忙碌检查放在维护锁外，避免与 BankWorkflow 的任务状态锁反向嵌套。
- 模型请求仍在学习锁外；维护不等待模型 HTTP。开始清理就清空旧预留并推进 revision，回包仍重验 session identity、revision、bank identity、允许项目和 reservation。完成前后的监听 reset 淘汰旧扫描/发布回调。
- 作答与清理竞争同一学习锁：清理先执行时，等待的旧题答案不能写回被删除的快照；作答先提交时，清理计划的学习摘要变化，要求重新预览确认，不丢掉已经接受的作答。
- `engine.reload_bank()` 在学习锁内读取指针、题库及匹配依赖，再替换内存状态。等待维护的重载不能预先读取旧文件后装回旧题库。
- 不完整维护或重载失败会在释放学习锁前通过 Learning.content_unavailable 阻断学习、清空旧预留和连接；session 的恢复标记仅在内存中，成功重载解除。即使日志已由原回滚流程移除，模块仍记住需要重载并允许公开 recover 重试。只读历史查询继续使用独立连接。
- 关闭仍是终止状态，重载和恢复不调用 initialize/reopen。显式应用重新初始化保留在原生命周期入口，必须先经过启动恢复。原退出队列继续排空已接受的提交/清理，然后关闭维护入口和学习连接。

## 修改范围

- 新增 `application/maintenance/__init__.py`、`release_tests/test_content_maintenance.py` 和本文。
- 接入：`startup.py`、`desktop_service.py`、`desktop/commands.py`、`application/banks/__init__.py`、`desktop/tk_app.py`、`bank_panel.py`。
- 一致性保护：`engine.py`、`application/learning/__init__.py`、`application/learning/session.py`。
- `tests/test_practice_latency.py` 的测试替身改接当前公开学习/维护 Interface，保留“不等模型且迟到结果不入库”的断言。
- 没有改 SourceDeletion/BankCleanupService 的日志、文件操作、数据库替换、schema 或 Store 原子作答事务；全量卸载继续使用原 AppCleanup 协议。

## 实际验证

使用项目已有系统 Python 3.10/pytest，所有数据为现有临时目录夹具和合成题库/SQLite，模型为替身，构建测试仅访问合成本地 HTTP。中断测试用真实子进程在写入 preparing、switching、cleaning 后 `os._exit(77)`，随后使用正常 StartupCoordinator.prepare 恢复；没有清理真实用户数据。

新增公开 Interface 测试：

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest release_tests/test_content_maintenance.py -q
```

结果：**17 passed，0 failed，0 skipped**（14.44 秒）。覆盖三个阶段进程中断及正常启动恢复、重复恢复、无效日志、一次性批准、双向作答/清理交错、迟到生成及关闭、串行重载、部分失败、重载失败、关闭等待维护、待答/重复提交保留和 Electron 响应/事件。

环境问题和接入失败均保留记录：

- 首轮沙箱内命令：`python -m pytest tests/test_desktop_service.py tests/test_engine_session.py tests/test_generation_concurrency.py tests/test_engine_instances.py tests/test_electron_transport.py tests/test_store.py release_tests/test_startup.py release_tests/test_bank_cleanup.py release_tests/test_feature_deletion.py -q`。结果 **114 passed、7 failed、0 skipped**；7 个失败均在 Tk 创建窗口时无法加载 init.tcl，行为测试未执行，属于环境问题。
- 显式设置系统 TCL_LIBRARY/TK_LIBRARY 后运行 `python -m pytest release_tests/test_startup.py release_tests/test_bank_cleanup.py tests/test_practice_latency.py -q`，仍为 **54 passed、7 failed、16 errors、0 skipped**，均为同一 Tcl 环境限制。独立 `python -c "import tkinter; print(tkinter.Tcl().eval('info patchlevel'))"` 也失败。
- 相同临时数据用例 `python -m pytest release_tests/test_bank_cleanup.py::test_history_cleanup_entry_has_its_own_confirmation_and_keeps_current -q` 在批准的沙箱外执行为 **1 passed**，确认是沙箱限制。后续 GUI 回归均在该执行环境运行，没有修改或跳过断言来绕过环境问题。
- 首轮沙箱外组合回归为 **232 passed、1 failed、0 skipped**（78.36 秒）。真实接入回归是 Tk 独立 worker 上下文缺少 maintenance 依赖；已补齐，同时避免模块闭包持有 Tk App。定位期间目标命令 `python -m pytest tests/test_practice_latency.py::test_queued_bank_maintenance_finishes_while_model_is_blocked -q` 曾多次返回 **1 failed、1 passed**；修正后整条目标命令为 **2 passed**。旧模块函数 hook 也改接当前公开学习 Interface。失败没有计作通过。
- 增加失败阻断后，独立命令 `python -m pytest release_tests/test_content_maintenance.py tests/test_desktop_service.py tests/test_generation_concurrency.py tests/test_engine_session.py -q` 为 **43 passed、0 failed、0 skipped**。

最终组合命令（沙箱外，保留 PYTHONPATH）：

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest release_tests/test_content_maintenance.py tests/test_desktop_service.py tests/test_engine_session.py tests/test_generation_concurrency.py tests/test_engine_instances.py tests/test_electron_transport.py tests/test_store.py tests/test_learning_loop.py tests/test_pending_flow.py tests/test_pending_retention.py tests/test_source_revision.py tests/test_content_chain.py tests/test_projects_module.py tests/test_desktop.py tests/test_practice_latency.py release_tests/test_startup.py release_tests/test_bank_cleanup.py release_tests/test_feature_deletion.py release_tests/test_answer_history.py release_tests/test_single_main.py release_tests/test_bank_build.py release_tests/test_bank_inventory.py release_tests/test_config_concurrency.py release_tests/test_migration.py -q
```

最终结果：**358 passed，0 failed，0 skipped**，119.24 秒。没有把此前的失败或环境错误计作通过。

`python -m compileall -q bagu_trainer` 通过；`git diff --check` 无空白错误，仅有工作树既有的 CRLF/LF 转换警告。

## 未覆盖与保留依赖

- 覆盖的是阶段标记后的进程终止；未穷举每次 os.replace 前后的断电、磁盘满、硬件写缓存丢失或文件系统损坏。
- 没有运行真实模型、真实第三方下载、大规模真实用户数据库清理，也未重建并启动冻结安装包；打包配置仍使用 collect_submodules 收集 application 子包。
- 真实杀毒软件长期占用、权限变化、跨卷外置数据库及多个独立进程在每个文件操作间的竞争未作完整故障矩阵；原路径检查、构建租约、备份、日志及失败重试继续承担保护。
- engine.py 函数、BankCleanupService/SourceDeletion 的低层 Interface 继续用于兼容脚本和基础设施测试；普通桌面流程已经统一经过维护模块。它们没有变成跨文件系统/SQLite 的原子事务。
- 全量卸载属于独立 AppCleanup 协议，本步只检查既有门禁，未重写其日志或执行流程。
