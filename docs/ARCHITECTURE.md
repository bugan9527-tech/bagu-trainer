# 后端架构与兼容范围

更新：2026-10-29，分层重构第 8 步。当前入口、数据和并发约束以本文及 [第 8 步验证记录](BACKEND_REFACTOR_STEP8.md) 为准；[最初基线](BACKEND_REFACTOR_BASELINE.md) 和 CORE_REFACTOR.md 保留历史事实。

同日完成无用代码清理：旧四步向导、InitializationApp 别名、独立模型配置窗口及向导专属快照已退役。`--configure` 继续导航工作台内的 ModelPanel；模型验证凭证与 onboarding.json 格式保留。旧窗口相关的有效测试、视觉诊断和桌面验收改用实际页面，历史阶段记录中的窗口路径不再代表当前入口。项目作用域归一化集中到 `infrastructure.paths.canonical_project_root`。

## 目录与依赖方向

```text
bagu_trainer/
  bootstrap.py                   显式创建独立 Engine；懒装配应用默认实例
  engine.py                      无状态的旧导入兼容入口
  desktop/
    service.py                   Electron 进程任务通道、快照发布、启动/退出
    commands.py / presenter.py    协议校验分发；展示快照与文案
    startup.py / runtime.py       就绪探测、启动恢复、日志和运行入口
    tk_app.py                    旧 Tk 主窗口及串行 worker
    *_page.py / *_panel.py ...    原页面、控制器、主题与布局
    cleanup_cli.py               原完整卸载的 CLI/Tk 交互入口
  application/
    learning/                    生成、交付、作答、历史重练、会话和只读历史
      engine.py / session.py     实例实现及学习会话；旧函数在此绑定实例
      generator.py               生成/独立审核流程，使用 HTTP adapter 与纯校验规则
    projects/                    监听范围、文件基线、扫描、防抖、活动数据
    monitor.py                   项目活动 → 学习流程；监听失败和显式重试协调
    settings/                    模型测试/取消、验证凭证、保存与普通配置事务
    banks/                       构建/取消/重试任务状态；串行应用协调
    maintenance/                 清理批准、失效、恢复、重载和完成通知
    events.py                    工作流公开事件的数据序列化
  domain/
    grading.py / review.py        判分和复习计算
    quizzes.py / excerpts.py      准入、校验、过期及正文质量
    matching.py                  内存题库的匹配规则
    knowledge_taxonomy.py         确定性分类
    title_quality.py             标题清洗规则
  infrastructure/
    store.py                     SQLite、SQL、只读快照、事务和备份/检查点
    config.py / secrets.py        配置文件、凭据和脱敏
    paths.py                     资源/安装/用户目录定位
    knowledge_base.py / matcher.py 题库文件与概念档案加载；调用纯匹配规则
    model_http.py                模型 HTTP、认证、重定向、截止时间和响应大小
    bank_build/                  下载、缓存、提取、校验和不可变版本发布
    bank_cleanup.py / source_deletion.py 具体文件删除、替代库及持久恢复日志
    ...                          原迁移、盘点、文件差异、来源及操作系统 adapter
```

方向为 `desktop → application → domain`，desktop/application 可以调用 infrastructure；infrastructure 可以使用 domain。domain 只依赖标准库，不读配置、凭据、文件或网络。infrastructure 不导入 application、desktop、bootstrap 或旧 engine；维护中的引擎能力通过参数注入。

bootstrap 是装配点，接收/创建配置、题库、匹配器、生成器、判分器及 session，交给 Engine。`create_engine(...)` 可创建隔离实例；`get_engine()` 提供应用默认实例。导入模块不装配题库、不创建学习会话、不打开数据库。

Engine 的 `load_config()` 是实例有效配置读取入口：以 `cfg` 为基础，由显式 `config_loader()` 返回值按顶层字段覆盖；loader 返回 `None` 或空字典时保留基础配置，显式 `{"llm": None}` 则关闭模型。没有 loader 时只使用该实例的 cfg；`create_engine()` 未传 cfg 时只在装配时读取一次用户配置。默认应用 `get_engine()` 显式绑定动态用户配置 loader，所以模型保存后下一次生成立即生效。

默认 QuizGenerator 注入绑定到该 Engine 的配置读取方法；这个方法直接合并实例配置，不通过 ContextVar 或默认引擎查找配置。调用方显式提供 generator 时不替换其实现。每次生成只读取并复制一份模型设置，按原来的环境变量 → 凭据管理器 → 旧配置值优先级解析 Key，生成和独立审核共用该份设置与一个截止时间；生成中的配置变更只对下一次生成生效。

独立调用 `QuizGenerator()` 或历史形式 `QuizGenerator(config, urlopen)` 保留已有兼容行为：未传关键字 `config_loader` 时忽略历史 config 参数，每次生成动态读取用户配置。需要独立隔离配置的调用方应使用 `QuizGenerator(config_loader=读取函数)`。这项兼容回退只用于独立调用，bootstrap 创建的默认生成器始终显式注入实例读取方法。

保留一个明确的过渡例外：`application.learning.engine._engine()` 在没有显式绑定实例时，懒调用 bootstrap 获取兼容入口的默认实例。正常实例方法通过 contextvars 绑定已有 Engine；不经此路径重新装配。该例外服务于仍受支持的旧函数和验收脚本，不是新的全局学习状态。

Tk 启动方法保留在 desktop；清理 CLI 的窗口交互也在 desktop。BankWorkflow 使用 application.events，不再反向导入 presenter。架构回归检查上述层方向、SQLite 出口及 Electron 的无 Tk 导入行为。

## 主要 Interface 与状态所有权

| 所有者 | 状态和业务操作 |
| --- | --- |
| Engine / bootstrap | 一个应用实例的 cfg、kb、matcher、generator、scorer、Learning。模块级兼容读写别名指向这一个实例。 |
| Learning / EngineSession | generation reservation、revision、待答快照、交付状态、已见指纹、允许项目、恢复阻断标记、SQLite 写连接及生命周期。公开操作包括 generate_for_activity、pending_questions、enter_question、submit_answer、resume_practice、repractice_history、history。 |
| ProjectWatch | 真实项目根路径、文件读取器、基线、扫描状态、15 秒稳定等待、暂停和扫描 epoch。产出 Activity，不管理待答和题目交付。 |
| DesktopMonitor | 协调 Activity 与 Learning、应用项目范围、显式重试和失效回调；页面切换不重建监听。 |
| SettingsWorkflow / verification | 测试取消事件、正在验证的模型设置、匹配设置的验证凭证和配置事务。凭据不进入桌面快照。 |
| BankWorkflow | 构建进度、失败来源、取消和 applying/applied 状态。完成后由普通命令 lane 调用维护模块应用。 |
| 旧 Tk TaskController | 保留页面之间的任务跟踪和取消状态；配置验证调用 application.settings.verification，内容应用/清理经过同一维护模块。它与 Electron BankWorkflow 属于各自启动入口的任务 adapter，不创建第二套学习引擎。 |
| ContentMaintenance | 一次性批准 token、原始清理计划、维护串行锁、是否需重载、失败和完成协调。原日志和具体文件/SQLite 操作由 infrastructure 执行。 |
| Store | 原 schema、写事务、只读查询连接和 SQL；不引入每表 Repository 或通用 CRUD。 |
| Electron 主进程 / Tk PracticeController | 当前展示题、按 quiz_id 保存的草稿、提交忙碌、成绩展示和窗口状态。权威作答及待答仍在 Learning/Store。 |

Tk 页面使用学习公开操作，仍保留少量 Engine 的公开历史/配置方法作为过渡；不读取 engine.kb、engine.cfg、session 私有状态。只读历史可以在题库/模型未就绪时独立访问。

## 并发与一致性

- Electron：普通写操作在 `bagu-commands` 单线程队列；历史/盘点使用两个 reader；扫描/生成使用独立单线程 generation executor；模型测试和题库构建使用现有两个后台任务线程。transport 的 history_gate 继续协调清理、读取和退出。
- Tk：主线程处理 UI，数据 worker 处理 jobs；扫描/生成使用独立 executor，历史读线程保持原实现。worker 上下文只带数据、队列及工作流，不持有 Tk App。
- monitor 锁 → session 锁 → Store 锁；session 锁 → 构建租约。维护自己的锁串行维护操作；先 reset monitor 并释放 monitor 锁，再进入 session/构建锁；持 session 锁期间不回调 UI/monitor。题库任务状态忙碌检查在维护锁外，避免锁反向嵌套。
- 模型 HTTP 在 session/monitor 锁外。返回后检查 session identity、revision、题库对象、reservation、项目允许范围、过期和冷却。题库切换、清理、范围变化和关闭后，迟到结果不能入库/交付。
- 提交答案通过原 `record_attempt(..., once=True)` 事务原子更新 attempt、concept_state 和 answered；重复提交不重复计数。历史使用独立只读连接和一致性读事务，不触发水合/过期写入。
- 清理与学习写入串行。先提交的作答使旧批准失效；先清理时，旧题提交被拒绝。失败维护在释放学习锁前阻断新学习，恢复/重载成功才解除。题库重载在学习锁内读取活动版本，避免预读旧文件后装回旧内容。
- 文件系统和 SQLite 不构成共同原子事务。来源删除继续使用 schema 1 日志：preparing/switching 恢复回滚，cleaning 恢复继续完成；部分失败保留日志，重复恢复幂等。
- 退出停止新工作，排空已接受的作答和维护写入、等待/取消已有后台任务，再关闭维护入口及学习连接。恢复/重载不调用 initialize，不会重开已关闭会话；显式重新初始化先执行启动恢复。

## 兼容范围

- Electron 命令名、请求/响应/事件格式及协议版本 **1** 不变；快照和待答不暴露答案/解析。题库构建仍发送原事件，不额外发送会清空草稿的删除事件。
- config.json 字段、默认值、凭据引用、用户目录及外置 DB 路径语义不变；移动 paths.py 后仍以项目资源根定位脚本和来源清单，冻结模式仍使用 sys._MEIPASS。
- SQLite schema 与最初 Git 基线的 SCHEMA 逐字哈希一致；原备份、水合、迁移和事务规则不变。没有新增 schema 版本迁移。
- 题库 questions/profiles/sources、active-bank 指针、来源缓存和维护日志格式不变；旧 flat 题库与 UUID 版本均由既有 adapter 读取。匹配规则和固定合成基准输出不变。
- `bagu_trainer.engine` 与 `bagu_trainer.application.learning.engine` 是同一个模块对象；旧函数、可变依赖别名、bootstrap、Electron 和 Tk 共享同一应用实例。仍有真实调用方：`scripts/acceptance/bank_lifecycle.py`、`scripts/check_ui_redesign.py`。未删除这些函数；新代码使用实例和工作流 Interface。
- `bagu_trainer.desktop.DesktopApp/main` 保留懒兼容导出，诊断脚本的 os/time patch 目标保留。旧 scorer/scheduler/quiz_validation 根导入在仓内调用方全部迁移到 domain 后移除。其他移动模块已更新仓内导入和脚本；不承诺未登记的外部源码 import 路径。
- PyInstaller 三个 spec 使用递归收集；Electron 排除名单更新为实际 desktop 子模块名，卸载所需 Tk 资源仍保留。源码与冻结后端的实际验证详见第 8 步记录；没有自动提交、发布或覆盖旧发行目录。
