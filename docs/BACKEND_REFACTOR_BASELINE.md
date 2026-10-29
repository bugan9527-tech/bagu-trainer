# 后端分层重构基线

日期：2026-10-28
基线对象：本次检查时的当前工作树（`main`，含既有未提交修改）。本文仅记录现状和必须保持的行为；没有修改生产代码。

## 工作区与测试边界

开始时 `git status --short` 显示已有未提交修改，覆盖文档、Electron 前端、`bagu_trainer` 后端、脚本及测试；另有未跟踪的 `scripts/check_unused_imports.py`。这些修改均保留。仓库及其父目录未发现 `AGENTS.md`。

测试夹具 `tests/conftest.py` 将用户数据目录和数据库重定向到临时目录，写入合成题库，注入假文件读取器、匹配器和生成器，并禁用真实密钥来源。Electron transport 测试另建临时 profile 和合成答题行；连接测试由测试替身拦截。因此本次成功测试不访问真实模型接口或真实用户数据。

项目内打包虚拟环境没有安装 pytest。实际执行及结果：

```text
命令：& .\.venv-build\Scripts\python.exe -m pytest tests/test_desktop_service.py tests/test_engine_session.py tests/test_generation_concurrency.py tests/test_electron_transport.py tests/test_store.py -q
结果：未启动，No module named pytest（环境问题，不计作测试失败）

命令：python -m pytest tests/test_desktop_service.py tests/test_engine_session.py tests/test_generation_concurrency.py tests/test_electron_transport.py tests/test_store.py -q
解释器：Python 3.10.11（python）；pytest 9.1.1
结果：31 passed in 2.64s；0 failed；0 skipped
```

通过数按文件：`test_desktop_service.py` 6，`test_engine_session.py` 9，`test_generation_concurrency.py` 9，`test_electron_transport.py` 2，`test_store.py` 5。没有把环境问题或测试失败标记为通过。

## 当前入口与接口

### Electron

`electron/main.cjs` 只允许 renderer 调用策略白名单中的后端方法，并通过 `electron/backend.cjs` 发送 JSON Lines 请求。Python 入口为 `scripts/start_electron_backend.py`，服务实现是 `DesktopService`（`bagu_trainer/desktop_service.py`）。主要后端命令包括：

- 生命周期/设置：`snapshot`、`prepare`、`projects.save`、`settings.save`、`monitor.pause`、`monitor.retry`、`shutdown`。
- 练习/历史：`practice.enter`、`practice.resume`、`practice.next`、`practice.submit`、`history.list`、`history.detail`、`history.repractice`。
- 模型/题库：`model.test`、`model.cancel`、`model.save`、`bank.build`、`bank.cancel`、`bank.inventory`、`bank.plan`、`bank.clean`。
- 启动与卸载：`startup.status`、`startup.set`、`uninstall.preview`、`uninstall.launch`。

renderer 的练习窗口状态（当前题、答案草稿、已显示判分、busy/error）由 Electron 主进程持有；它检查发送窗口、主 frame、来源 URL、方法和 payload，并阻止重复提交/对已切换题提交。权威题目、待答、答题历史和评分仍由 Python `DesktopService` → `DesktopMonitor` → `engine`/`EngineSession` 持有。服务快照剔除答案和解析，提交后的持久化只认后端结果。

后端命令分三条执行路径：Python 主命令单线程 `bagu-commands` 队列；历史列表/详情和题库盘点读操作走两线程 reader 池；`DesktopService` 内部另有双线程一般任务池（模型连通性测试、题库构建）及单线程 generation 池（监控扫描/生成、重试）。题库构建在任务池完成构建后发 `bank_built`，再由主命令队列串行应用；Electron 收到该事件后调用 `prepare`。Python transport 使用 `history_gate`（RLock）保护部分历史、盘点、清理及关闭路径。

### 旧 Tk 入口

`scripts/start_desktop.py` → `runtime.run()` → `desktop.main()` → `StartupCoordinator.run()` → `DesktopApp`（`bagu_trainer/desktop.py`）。这是另一套 UI adapter：Tk 主线程只处理 UI，动作进入 `WorkQueue`，由单个 `bagu-desktop-monitor` worker 依次执行。该 worker 使用单线程 generation executor 跑扫描/生成；`TaskController` 管理模型/题库任务，历史页面另有自己的读线程。练习提交进入同一 worker 的 `submit` 工作项，并按 quiz id 回传结果。退出时 worker 丢弃新的扫描/普通动作，但排空已经接受的提交和清理写操作；UI 等相关线程结束后销毁窗口，`StartupCoordinator.run()` 的 `finally` 关闭 session。

旧 Tk 使用后端能力而非 `DesktopService` RPC：`StartupCoordinator.prepare()` 加载 `bagu_trainer.engine`，页面/worker 直接调用 `DesktopMonitor` 和 engine session，包括 `list_pending`、`enter_quiz`、`resume_quiz`、`repractice_attempt`、`submit_answer`、`generate_activity`、历史查询等。自动监控只更新待答列表，不自动抢占当前练习窗口；打开、继续和下一题由明确的练习动作触发。

## 状态所有者和存储边界

- `DesktopService` 持有 RPC 快照/任务状态、验证过的模型配置引用、临时清理计划、退出标志及执行器；不是学习记录的权威存储。
- `DesktopMonitor` 持有单进程监控元数据：监听 roots/别名、文件观察与稳定等待、失败重试、暂停状态和活动题目提示。`_state_lock` 保护其运行时元数据；`_epoch` 让暂停、换项目、重置等状态转换淘汰旧扫描回调。
- `engine.EngineSession` 是单个学习会话的权威内存状态和 SQLite 连接所有者：题目快照、ready/delivered 队列、seen 指纹、生成 reservation、diff reader、generation revision 和 Store 生命周期。模块级 `engine.session` 是兼容旧入口的默认会话；`initialize()` 在终止关闭后创建新会话。数据库水合延迟到首次 session 操作。
- `Store` 是 SQLite 持久层；`quizzes` 保存题目快照/项目归属/状态/有效期，`attempts` 保存作答，`concept_state` 保存按知识点共享的掌握度/复习计划，`quiz_sources` 保存来源关联。未作答题的作用域按规范化 `project_root` 隔离；已答历史及知识点复习状态可跨项目共享。
- `LearningHistory`/`Store.history_page` 使用只读 SQLite 连接供历史浏览；该路径不水合待答、不触发过期清理，也不必等待 EngineSession generation 锁。

## 并发、锁顺序和去重

可观察到的锁嵌套关系如下，重构应维持同一顺序，避免反向获取：

1. `DesktopMonitor._state_lock` → `EngineSession._lock`：如监听范围应用、生成 revision 检查；session 读取器状态在 session 锁内再到 `_readers_lock`。
2. `EngineSession._lock` → `Store._lock`：EngineSession 操作 SQLite 时保持 session 锁；Store 自身用 RLock 串行化其 `check_same_thread=False` 共享连接。
3. 文件系统扫描和模型 I/O 不应持有 EngineSession 或 monitor 元数据锁。generation 先在 session 锁内登记 `(project_root, kp_id)` reservation 和 revision，释放锁后调用模型，再重新获取锁检查状态后才写入。
4. Store 历史只读连接采用独立只读连接与一致性读事务；不获取 EngineSession 锁。

生成任务携带 `(session identity, revision)`。题库切换、内容失效、会话关闭/替换、移除项目或白名单变化都会使旧任务结果失效；返回的迟到模型结果不得落库、发布待答，也不得误删新任务的 reservation。相同项目知识点的 active reservation 阻止重复/显式重试重复计费；模型返回后重新核验项目范围、待答冷却和复习状态。不同项目有独立未答冷却，但学习历史按知识点共享。

## SQLite 写入、事务和恢复

- `Store` 使用一个共享 SQLite 连接，外层 `EngineSession._lock`、内层 `Store._lock` 串行化读写。初始化启用 WAL、外键并建 schema。
- `create_quiz`、`set_status` 是单项写并显式 commit；ready 题首次交付时内存状态和数据库状态同步为 delivered。
- `record_attempt(..., once=True)` 在一个 SQLite 事务中检查 quiz 是否已有 attempt，然后插入 attempt、更新 `concept_state` 并将 quiz 标为 answered；重复提交拒绝且不重复计数。EngineSession 只在持有 session 锁并完成该事务后同步内存态。
- `history_page` 在只读连接中 `BEGIN`，让总数、分页和项目筛选共享同一数据库快照。
- session 首次访问通过 `load_pending()` 恢复尚未过期的 ready/delivered 行；配置 TTL 大于零时将过期记录持久标记 expired。恢复失败时关闭临时 Store 并清除半水合内存状态，使后续重试可重新水合。
- 关机关闭连接前 commit，并保留未答快照和历史；重新创建 EngineSession 后从数据库恢复题目。已答 quiz 的历史记录保留，重新答题仍会由数据库幂等保护拒绝。

## 关闭流程

Electron `before-quit` 先广播 closing 并拒绝 renderer 新请求，再请求 `Backend.close()`：发送 `shutdown`、关闭 stdin、等待服务响应和进程退出。Python 输入循环停止 timer，等待 reader 与主命令队列完成已接受工作，再调用 `DesktopService.close()`：设置 stop、停止 monitor、取消模型/题库任务事件、等待任务池和 generation 池退出，最后关闭 session/SQLite；随后关闭窗口 broker。EOF 和显式 shutdown 都须排空已接受的答题写入（由 `test_electron_transport.py` 覆盖）。

Tk 退出时设置 stop、唤醒 worker、请求取消可协作的题库构建并阻止新工作；worker 对待处理队列只保留已接受的提交/清理写入和历史屏障，完成线程后才销毁 UI；`StartupCoordinator.run()` 再关闭 EngineSession。

## 重构必须保持的行为

- [ ] **项目隔离**：所有活动上下文先经 `project_roots` 白名单和真实路径归一化校验；pending 查询、交付、冷却和生成按项目隔离。项目 A 的未答题不能被项目 B 看到、交付或用于冷却；移除监听项目只移除监控状态，不删除答题历史。跨项目复习只可从已作答 AI 题目快照重建。
- [ ] **重复提交保护**：同一个 quiz 最多记录一个 attempt；attempt、掌握状态、复习时间和 answered 状态必须原子写入。界面重复点击、并发调用和重启后再次提交都不能重复记账。
- [ ] **迟到结果丢弃**：暂停/项目范围变化、题库 reload、内容维护、session close/replacement 后返回的旧生成结果不得写库或发布；老任务清理 reservation 时不得移除新 revision 的 replacement reservation。
- [ ] **待答恢复**：ready/delivered 的未过期题在重启后恢复；ready 在用户显式进入时转为 delivered；浏览列表不改变 delivery 状态；过期题按 TTL 持久标记。关闭或恢复失败不得丢掉可恢复数据库行。
- [ ] **退出前写入**：已被接受的答案及清理/内容应用写入在关闭 session 前完成；关库前提交，Electron 的 EOF 和显式 shutdown 均排空命令，Tk 等待必要 worker 完成。
- [ ] **暂停语义**：暂停停止监控/自动生成，但不隐藏已有待答题，也不禁止明确的继续、选题和提交。
- [ ] **隐私边界**：公开待答/快照不暴露答案和解析，renderer 不能越权调用；代码上下文和凭证不得进入普通日志或 transport 错误，持久化来源摘要不包含原始代码上下文。
- [ ] **隔离读历史**：历史只读分页不触发待答水合/过期清理，不阻塞学习写入；分页计数、项目过滤和项目选项来自同一快照。
- [ ] **可取消与可串行任务**：模型测试和题库构建在独立任务 lane，不阻塞作答；题库构建后的活动题库应用与普通写路径串行；shutdown 时停止接受新任务。

## 风险与基线限制

- 两套入口目前有不同的任务编排、UI 状态副本、请求去重和关闭实现；共享的核心是 EngineSession/Store，但接口仍以模块级函数和对象方法为主，职责变化需要同时检查 Electron 与 Tk 路径。
- 锁嵌套跨 monitor、session、readers 和 Store 多个对象；当前指定测试覆盖生成竞态、session 水合和提交事务，但不是全面的锁顺序证明，也没有替代针对 Tk/Electron 整体生命周期的 GUI/打包验收。
- 本次测试确认的是当前工作树，不是干净的 HEAD；原有未提交差异可能影响基线。未来比较前应保留这些差异或明确选择同一源码状态。
- `.venv-build` 中缺少 pytest。虽然系统 Python 运行结果全部通过，但需在正式 CI/打包基线记录中明确使用的解释器，避免误以为打包环境具备测试依赖。
