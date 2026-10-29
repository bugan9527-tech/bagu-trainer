# 无用代码排查

日期：2026-10-29。范围：当前工作区中的 bagu_trainer、scripts、electron、frontend/src，并核对 tests、release_tests、打包配置及 CI 引用。

**执行状态：已清理。** 下文保留清理前的排查证据和原行号。8 处优先清理项、3 个测试专属旧入口及旧向导/独立配置窗口实现已移除；有效模型和窗口测试已迁到当前嵌入面板与准备接口，路径归一化已合并到 infrastructure.paths。验证记录见 `validation/dead-code-cleanup-20261029/REPORT.md`。仍有实际用途的 Tk 主窗口、卸载、题库及兼容接口保留。

本轮代码净减少 1,433 行。最终单元测试 322 passed、31 skipped，完整源码发布验收 836 passed，Electron 冒烟 19 项通过；旧便携二进制的 5 项检查因版本不匹配未纳入。清理前工作区快照及首次 GUI 失败/复跑记录保存在上述验证目录。

采用 Python AST 检索定义、调用/属性读取/导入、只写属性、恒定条件及重复实现，再用仓库搜索检查动态入口、字符串引用、测试和打包。静态无引用不等于框架不会调用，因此下表已人工排除可见的框架回调和兼容导出。

## 优先清理的 8 处

以下判断针对仓库内已登记的调用方，不承诺未登记的外部反射访问。清理时保持右侧所述行为。

| 位置 | 证据 | 建议 |
| --- | --- | --- |
| bagu_trainer/application/learning/__init__.py:67，Learning.normalize_project_roots | 源码、脚本和测试均只出现定义；不属于特殊框架方法 | 删除未调用方法，保留 apply_project_scope 所用的路径归一化 |
| bagu_trainer/domain/matching.py:523，self._avgdl | 只有一次赋值，无任何读取；实际计算使用 kind_avg | 删除这一平均值计算，不改 BM25 字段归一化 |
| bagu_trainer/desktop/practice_page.py:17、77，self._pending | 只初始化并复制 items，没有读取 | 删除这两处冗余状态；保留 _pending_ids 和列表更新逻辑 |
| bagu_trainer/desktop/practice_controller.py:38、147、159、212、269，self.submitted_answer | 五处写入，没有属性读取；apply_grade 的显示使用局部参数 submitted_answer | 删除字段写入，保留方法参数、答案文案和提交流程 |
| bagu_trainer/desktop/task_controller.py:55，self.ui_events | 构造时保存或创建 Queue，后续从不读取；真实构建进度使用 BankTask.events | 可先删未用字段；随后迁移构造参数与调用方并修正文档，不能删除 BankTask.events |
| bagu_trainer/desktop/settings_page.py:24，self.on_status | 保存后没有任何调用/读取 | 先删未用字段；若移除 on_status 参数，应同时更新所有构造调用方 |
| bagu_trainer/desktop/settings_window.py:182，ModelSettingsWindow._destroyed | 空 pass 方法，本文件无绑定/调用；实际 Destroy 事件绑定 _panel_destroyed | 删除未使用的空方法，保留 _panel_destroyed 及其关闭保护 |
| bagu_trainer/desktop/tk_app.py:1184，if True | 条件恒真，没有其他分支 | 去除外壳并将退出处理退一层缩进；不能删除内部安全退出逻辑 |

## 测试在使用、正常产品路径未使用的历史接口

这些不是“全仓零引用”，应迁移有价值的测试后再删除，不应先删实现、保留断言失效的测试。

- application/learning/engine.py:181 的 _startup_log：只由 tests/test_content_chain.py 的启动文案测试调用；当前实际启动流程不调用它。可删除旧日志函数与仅验证该死入口的测试，或将政策检查迁到真实启动路径。
- application/learning/engine.py:368 的 _poll_once：只由 tests/test_pending_flow.py 调用；实际后台走 DesktopMonitor.poll。迁移测试到当前学习/监听 Interface 后可删除旧刷新包装。
- application/learning/generator.py:90 的 test_llm：产品模型测试使用 application/settings/verification.py 的 test_connection。当前仅在超时、安全测试中被调用；应先把这些安全断言迁到实际模型测试/HTTP 路径，再考虑删除重复连接测试入口及导出。
- bank_inventory.delete_source_cache、source_extractor 的简版包装、ModelSettingsWindow.build_window、DesktopMonitor.pending_count 等也存在仅测试引用；它们可能承载兼容或安全契约，未列为可直接删除项。

## 大体量历史代码：需明确退役范围

- desktop/first_use.py：569 行。正常桌面启动已走工作台；但 scripts/check_ui_display.py 和多项 GUI 测试仍使用 FirstUseWizard。
- desktop/settings_window.py：194 行。正常 --configure 导航到主窗口模型页，但视觉及桌面验收脚本仍使用独立 ModelSettingsWindow。
- desktop/initialization.py：2 行，是旧向导别名，目前还有测试调用。

这组三文件共 765 行，其中可直接删除项已单列。若决定退役旧 GUI 验收场景，可以连同 WizardSnapshot、旧向导专属状态和测试成组清理；不能把本组全部计作当前可直接删除的代码。整个 Tk 桌面仍有受支持源码/便携入口，卸载工具也依赖 Tk 资源。

## 重复实现与误报排除

- application/learning/__init__.py:17 的 _canonical_project_root 与 application/projects/__init__.py:15 的 canonical_root 函数体相同；engine.py 的 _normalize_project_root 也实现相同路径语义。可统一路径归一化，但要保留 Windows 大小写、真实路径、空路径和异常语义。这是重复实现，不是零引用死代码。
- infrastructure/fallback_bank.py 不能因“禁止离线出题”而整体删除：题库 importer 仍取统计，概念档案 CLI 仍可取人工回退解析，旧 create_quiz 也使用其查询路径。
- infrastructure/model_http.py 的 http_open/https_open 由 urllib 按协议调用；Stream.isatty 属于流协议。静态查不到显式调用不能删。
- ctypes argtypes/restype、SQLite row_factory、SSL sslsocket_class、HTTP connection 的 _create_connection、logger.propagate 由库/运行时读取，不是无用字段。
- engine.py 兼容模块仍有已登记的验收调用方；不能因为根文件很短或只是转发而直接移除。
- Widget 创建、after 定时任务等即使返回值字段未被读取，调用仍有副作用；不能据此删除控件或调度语句。

## 检查结果和清理顺序

- python -B scripts/check_unused_imports.py：121 个文件通过，3 个有说明的兼容/副作用例外。
- node_modules/.bin/tsc.cmd --noEmit：通过，已启用 noUnusedLocals/noUnusedParameters。未据此宣称所有跨模块导出或 CSS 都不存在无用代码。
- 本轮未执行运行测试，因为没有修改业务实现；上一轮通过的回归不替代未来实际删除后的验证。

建议先清理 8 处小残留，再迁移测试专属入口，最后单独处理旧向导/独立配置窗口退役。历史实现退役需要同步检查打包收集、排除名单、验收脚本和文档。
