# 0.4.0 核心渐进重构记录

## 所有权与实际调用关系

| 状态/资源 | 当前所有者 | 调用与约束 |
| --- | --- | --- |
| 配置文件、凭据引用 | `config` / `secrets` | 模型页、项目页经配置事务读改写；`_CONFIG_LOCK` 保护原子提交。模型配置动态读取；`engine.cfg` 仍保留导入时的出题容量配置快照。 |
| 激活题库、匹配器 | `engine.kb` / `engine.matcher` | `BankBuildService` 在构建锁内发布题库版本；`TaskController` 报告 built 后，桌面串行工作线程执行 `reload_bank()`。此批保留模块级装配。 |
| 待答题、完整答题快照、去重指纹 | `engine.session: EngineSession` | 列表读取、主动选题、自动交付、继续练习共用实例状态；未作答题与冷却按项目隔离，人工历史重练沿用既有例外。 |
| 学习数据库连接 | `EngineSession` | 首次使用打开并水合；`Store` 自己的锁保护 SQLite 操作。作答、复习计划、来源关联格式不变；历史读取仍使用独立只读连接，避免被出题网络请求阻塞。 |
| 引擎状态锁、内容代次 | `EngineSession` | 可重入锁覆盖采集、匹配、生成、清理和学习写入；手动出题在锁外等待网络结果，用会话身份及内容代次拒绝迟到结果。 |
| 文件差异读取器 | `EngineSession` | `observe_project()` 安装桌面 `ActivityDiffReader` 并采集脱敏上下文；移除项目只删除其基线，不删除历史。 |
| 防抖、待检查上下文、出题失败重试、暂停、当前自动交付题号 | `DesktopMonitor` | 既有串行线程调用；暂停/浏览控制自动交付，不禁止用户主动练习。监测层不再操作队列、连接或状态锁。 |
| 桌面 jobs/events、退出等待 | `DesktopApp` | Tk 主线程只更新页面；数据工作线程执行监测、作答、题库重载、来源删除。退出先完成已排队写入并等待后台任务，再关闭会话。 |
| 题库构建任务 | `TaskController` / `BankTask` | 取消事件、构建进度、built/applied 状态随应用存活，切换页面不取消构建。继续复用现有控制器。 |
| 作答草稿、提交状态、迟到判分、延迟显示队列 | `PracticeController` | 以 quiz_id 关联结果；页面切换保留选择，提交与切题互斥，迟到判分不覆盖另一题。继续复用现有练习页面。 |
| 页面可见性、查询结果 | 现有页面 / 桌面 shell | 历史页独立读取学习历史；页面实例及状态继续缓存。删除后仍由既有 `bank_cleaned` 事件清理页面与练习缓存。 |

主要调用链：

```text
StartupCoordinator.prepare -> engine.initialize (仅重建已关闭的会话)
DesktopApp.worker -> DesktopMonitor.poll -> engine.observe_project
                                       -> engine.generate_activity
DesktopMonitor -> session.list_pending / take_quiz / enter_quiz / resume_quiz
PracticeController -> jobs.submit -> engine.submit_answer -> Store.record_attempt
TaskController.built -> jobs.reload_bank -> engine.reload_bank
jobs.bank_cleanup -> SourceDeletion -> engine.content_maintenance -> 构建锁 -> 文件/数据库替换
DesktopApp 安全退出 -> StartupCoordinator.finally -> session.close
```

锁顺序保持为引擎会话锁 → 构建锁/Store 锁。配置事务仍独立管理；本批没有移动模型请求到 Tk 线程，也没有改变现有构建线程或取消规则。

## 第一批：待答状态与会话生命周期

选择依据是原 `DesktopMonitor` 直接遍历 `_state`、持 `_STATE_LOCK`、打开 `_store`、写入 delivered 状态及拼装到期复习，而桌面项目切换和退出又直接删除读取器、关闭连接。先迁移这些真实共享的职责，比拆分页面或按文件长度切割更能减少调用方需要了解的规则。

这批将上述运行态收拢为 `EngineSession`。实例真正实现水合、去重、过期/容量清理、交付和到期复习；它不是转发到原模块全局状态的包装。模块内生成、判分和人工重练暂时直接使用同一实例的内部状态，下一批可沿已有业务入口迁移，无须重做页面。

桌面调用的实例接口：

- `list_pending()`：清理过期项后返回公开快照，读取不交付；返回的嵌套选项也不能修改内部题目。
- `take_quiz()`：当前配置项目中最早的 ready 题，只交付一次；自动推送的暂停和当前显示题号仍由 Monitor 控制。
- `enter_quiz(quiz_id)`：用户显式选题，可再次进入 delivered 题；不绕过作用域及人工重练政策。
- `resume_quiz()`：优先恢复 delivered/ready，之后从已作答 AI 快照重建到期复习；不采集上下文、不调用模型。
- `forget_projects(roots)`：只移除指定项目的文件基线。
- `close()`：幂等、终止会话，释放连接及内存，不删除持久化学习数据；再次启动由 `initialize()` 创建新实例。

维护流程使用 `content_maintenance()`；正常模型出题使用 `generate_activity()`，将锁、去重、容量清理留在引擎内，而不是分散到桌面层。

生命周期语义：

| 操作 | 待答/历史 | 文件基线与缓存 | 锁外迟到出题 |
| --- | --- | --- | --- |
| 新会话 | 首次使用从现有库水合 | 新建 | 仅接受本会话结果 |
| `reload_bank()` | 保留 | 保留现有基线 | 旧代次结果丢弃 |
| `invalidate_content()` | 清空内存、释放连接；持久化删除仍由来源删除事务负责；下次只水合库中仍存在的记录 | 清空基线及内容缓存 | 旧代次结果丢弃 |
| `close()` | 保留磁盘记录；旧实例禁止重新开库 | 清空 | 丢弃，不能重开旧实例或复活内容 |

## 剩余耦合与后续范围

- `cfg/kb/matcher/generator/scorer` 仍由引擎模块装配；会话的范围、容量、AI 入场检查仍复用模块配置和纯规则。此实例尚不是可独立配置的完整引擎。
- 引擎模块内的出题、判分、人工重练仍访问会话内部状态；旧单测及验收夹具中还有白盒构造。新生命周期回归只通过业务接口断言状态。
- 桌面仍读 `kb.explicit_empty`、题库数量、`load_config()`、项目根目录及私有归一化函数。下一批可将配置/题库读取合成清晰快照，并迁移模块内的写入职责。
- 数据库维护仍需持会话锁跨越文件替换；这是真实原子性要求，不能只把锁移到一个转发对象中。
- `PracticeController`、各页面、`TaskController` 已有明确职责，本批继续复用；没有新增页面、控制器或改变界面流程。

## 回归与证据

新增 `tests/test_engine_session.py` 覆盖只读列表、状态迁移、重复提交、关闭恢复、独立连接、并发交付、暂停和切换/失效/关闭后的迟到结果。已有项目隔离、AI 政策、来源删除及恢复、历史浏览、页面切换与提交竞态回归继续执行。

最终核心组 84 项通过（含新增 9 项业务回归）；包含 Tk 的完整相关组 263 项通过、1 项删除确认按钮可见性检查失败。重构前独立快照同组为 255 项通过、同一项失败。没有放宽断言或将完整验收标记为全部通过。

本次命令、最终数量与 JUnit 记录见 `validation/core-refactor-20261025/REPORT.md`。初次沙箱内 Tk 未能加载 `init.tcl`，该结果不会作为界面验收通过；相关检查已另行在临时合成数据下执行。此处没有运行安装包生命周期或覆盖历史验收结论。
