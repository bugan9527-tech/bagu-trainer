# 后端分层重构第 6 步

日期：2026-10-28。基于当前工作树及 `BACKEND_REFACTOR_BASELINE.md` 实施；保留原有未提交修改，没有自动提交。未发现适用的 AGENTS.md。按 codebase-design 的 Interface 与 Implementation 分工，分两批独立验证。

## 第一批：集中数据访问

继续使用单个 Store 模块，不新增逐表 Repository 或通用 CRUD：

- `Store.dashboard_stats(path, now, expire_pending=...)`：独立只读连接，同一个读事务返回作答、正确率、到期复习及待答计数。不创建数据库，不水合 session，也不修改过期状态。
- `Store.history_page(...)`：保留筛选、关键词字面查询、20 条分页、计数、项目选项和排序语义。计数、分页及项目选项共用读快照。
- `Store.history_detail(...)`：作答详情和继承的来源关联改为同一个独立只读连接、同一个事务快照，保留旧 JSON 和旧 schema 的兼容行为。
- `Store.source_manifest_at(...)`：来源删除的旧 schema 检查只在内存副本中执行，应用层不再自行构造 Store 私有连接。
- `Store.backup_database(...)`、`checkpoint_database(...)`、`is_learning_database(...)`：集中迁移、来源清理及数据库所有权检查所需的 SQLite 操作。

LearningHistory、SourceDeletion、migration 和 cleanup_manifest 只使用这些公开方法。扫描生产源码确认，SQLite 连接创建、SQL 执行和 Store 私有连接访问全部位于 store.py；正文质量规则中的 SQL 关键词正则不是数据库访问。

Store 的写连接、WAL、session → Store 锁顺序和 `record_attempt(..., once=True)` 原子事务保持原实现。SCHEMA 常量与重构前逐字一致。

## 第二批：纯规则与基础设施分工

`domain/` 只导入标准库，函数接收数据、时间和策略参数：

- `grading.Scorer.grade`：原答案归一化、判分及结果字段。
- `review.next_state(correct, prev, now, policy=...)`：原复习公式；默认 ReviewPolicy 保持原数值。`review_candidates` 负责待答排除及偏好排序。
- `quizzes`：题目 JSON 校验、引用与审核校验、审核凭证、AI 题目准入、历史重练准入、有限过期时间、显式重试/预留/待答/复习冷却判断。QuizPolicy 默认题长仍为 5–500。
- `excerpts.assess_excerpt`：原正文长度、链接、代码、推广和题目列表质量计算。
- `matching.Matcher(questions, concept_profiles=..., ...)`：原匹配核心；接收内存数据，不读取题库文件或全局配置。阈值、索引、排序和分数字段保持原实现。

`KnowledgeBase` 继续加载题库及来源 JSON；原 `matcher.Matcher(kb, ...)` 是加载概念档案的兼容 adapter，然后调用匹配核心。原 scorer、scheduler、quiz_validation 导入路径以及 `QuizGenerator._parse_quiz` 兼容访问保留。

`model_http.ChatModelClient` 负责 HTTP 请求、认证头、响应大小限制、响应封装解析和脱敏；QuizGenerator 负责生成/独立审核的流程及错误分类，使用 domain 的内容规则。两次请求仍共享一个截止时间，并向生成流程与 HTTP client 注入同一时钟；没有增加模型自动重试。

合成匹配样本的 5 个固定查询与重构前 matcher 完整输出逐项比较一致（4 个有命中、1 个按原规则为空）。固定结果保存在 `tests/fixtures/matching_rule_baseline.json`，公开 `rank` 行为测试覆盖 id、分数、置信度、证据及排序。纯规则测试还阻止文件读取、配置读取和凭据解析。

## 实际验证

使用现有系统 Python 3.10 / pytest。测试使用合成题库、临时数据库、假模型响应及本地 HTTP 测试服务器，没有访问真实模型或修改真实用户数据。

第一批独立命令：

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest tests/test_store.py release_tests/test_answer_history.py release_tests/test_bank_cleanup.py release_tests/test_feature_deletion.py release_tests/test_migration.py tests/test_desktop_service.py -q
```

结果：**79 passed，0 failed，0 skipped**。首次没有显式 PYTHONPATH 的组合运行是 78 passed、1 failed：前面的 Tk 用例改变工作目录，来源删除测试的子进程因此报 ModuleNotFoundError，未能启动。属于测试启动环境问题；没有改测试期望来绕过它。

第二批独立命令：

```powershell
python -m pytest tests/test_domain_rules.py tests/test_store.py tests/test_learning_loop.py tests/test_matcher.py tests/test_matcher_concepts.py tests/test_matcher_heldout.py tests/test_quiz_generator.py tests/test_source_extractor.py tests/test_generation_concurrency.py release_tests/test_quiz_grounding.py release_tests/test_grounding_mock_transport.py release_tests/test_model_deadline.py -q
```

结果：**133 passed，0 failed，31 skipped**。本批第一次组合运行有 2 项真实回归（131 passed、2 failed、31 skipped）：HTTP 抽取后时钟来源不一致，使共享超时预算测试失败。通过注入同一个时钟修正后，整批重跑通过。

最终组合命令：

```powershell
$env:PYTHONPATH = (Get-Location).Path
python -m pytest tests/test_store.py tests/test_domain_rules.py tests/test_learning_loop.py tests/test_engine_session.py tests/test_engine_instances.py tests/test_generation_concurrency.py tests/test_pending_flow.py tests/test_pending_retention.py tests/test_source_revision.py tests/test_content_chain.py tests/test_ai_only.py tests/test_privacy.py tests/test_matcher.py tests/test_matcher_concepts.py tests/test_matcher_heldout.py tests/test_quiz_generator.py tests/test_source_extractor.py tests/test_desktop_service.py tests/test_electron_transport.py release_tests/test_answer_history.py release_tests/test_quiz_grounding.py release_tests/test_grounding_mock_transport.py release_tests/test_model_deadline.py release_tests/test_bank_build.py release_tests/test_bank_cleanup.py release_tests/test_feature_deletion.py release_tests/test_migration.py -q
```

结果：**364 passed，0 failed，31 skipped**，用时 72.90 秒。31 个跳过项需要未提供的 BAGU_TEST_BANK_ROOT 外部匹配语料；它们不计作通过，完整外部题库的质量和性能评测仍未执行。

`python -m compileall -q bagu_trainer` 通过；`git diff --check` 无空白错误，工作树中的 CRLF/LF 转换警告仍存在。

## 保留的基础设施依赖

- Store 保留 SQLite、连接生命周期、只读快照和事务；应用层保留文件替换、删除恢复日志及锁协调。
- KnowledgeBase、concepts 和 source_extractor 保留文件加载、路径检查与来源提取；匹配和正文质量计算独立于这些读取。
- QuizGenerator 仍实时读取模型设置并解析凭据，model_http 处理 HTTP/截止时间/脱敏；domain 不参与这些操作。
- 学习模块继续拥有 session、预留、代次、锁和 Store 生命周期。是否接纳迟到结果仍需在锁内复核；不能用纯计算替代并发状态检查。
- 可选语义检索继续由调用方注入，默认关闭；纯匹配核心本身不发起文件或网络请求。
