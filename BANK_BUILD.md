# 桌面题库初始化模块

中文首次使用向导第 1 步的内嵌面板提供来源多选、固定版本显示、声明链接、阶段进度、
总超时、取消和重试。未选择来源不会下载。完整第三方题库仍由用户首次使用时下载到本机，
源码发布包只包含程序和来源元信息。

默认写入根目录为 `%LOCALAPPDATA%\BaguTrainer\`，本文 `data/` 均相对该用户目录；
来源清单独立从只读源码目录或便携版 `_internal/interview-banks/sources.json` 加载。
显式传入 `project_root` 可为开发和测试指定构建目标，不改变默认资源目录。

## 流程和数据切换

1. 从 `interview-banks/sources.json` 解析所选来源，默认锁定 `audited_commit`。
2. 使用 Python 标准库发起 HTTPS 请求，通过 [GitHub ZIP 归档接口](https://docs.github.com/en/rest/repos/contents#download-a-repository-archive-zip)
   下载完整 commit 对应的 ZIP；重定向仅允许 GitHub 的 HTTPS 归档域名。
3. 逐条验证并解压到独立临时目录。不使用 `extractall()`，拒绝路径越界、绝对路径、
   Windows 设备名/ADS、大小写碰撞、符号链接、特殊文件和加密条目，读取时校验 ZIP CRC。
4. 在进程内依次调用提取去重、来源正文导入、知识标签和概念构建函数；不抓取 Issues。
5. 校验题库非空、ID 唯一及概念覆盖，原子写入各文件并回读核对。
6. 将完整目录移入 `data/banks/<UUID>/`，最后原子替换 `data/active-bank.json`。

| 路径 | 用途 |
| --- | --- |
| `data/.bank-builds/<UUID>/` | 每次独立尝试的 ZIP、解压与构建工作目录；正常失败/取消时清理 |
| `data/banks/<UUID>/questions.json` | 完整新题库 |
| 同目录的 `question_concepts.json` 和报告 | 与该题库一致的概念档案、标签/清洗报告 |
| 同目录的 `repos/`、`extracted/`、`sources.json` | 来源快照、提取结果及 commit/下载 SHA256/声明元信息 |
| 同目录的 `previous-pointer.json` | 切换前指针的原始副本（若存在），也保留损坏指针以便诊断 |
| `data/active-bank.json` | 当前完整版本指针；不包含学习数据库配置 |
| 原 `data/questions.json`、概念文件和旧版本目录 | 保留原样；无指针时兼容读取旧式题库 |

`KnowledgeBase` 和匹配器从同一版本读取题目与概念，概念缓存按真实版本路径区分。
学习数据库、配置及原目录不被构建服务改写。新流程的来源稳定 ID 避免顺序 ID 在不同来源
集合中被复用为其他题目。旧顺序 ID 的记录保留在 SQLite 中，不自动推断映射到新 ID。

切换前出现任何错误，当前指针都保持原样；切换仅发生在完整目录校验成功后。
若在目录移入后、指针提交前中断，可能留下完整但未启用的目录，旧版本继续生效。
强制结束进程可能留下临时目录；新尝试使用新的 UUID，不会恢复或发布半份数据。
构建锁由操作系统持有，进程结束后释放，重新启动可以重试。当前不会自动删除旧版本。

## 取消、超时和重试

- 扫描、去重、每题处理、ZIP 成员及网络块读取均检查取消和总时限。
- 默认总超时 900 秒、单次 socket 超时 5 秒；桌面允许调整总超时。
- 取消为协作式，通常在当前读取结束或 socket 超时后响应；Python 线程无法强制中断
  系统 DNS 等底层阻塞。最终指针替换是很短的提交操作，提交完成后返回成功。
- 网络错误、连接超时、HTTP 408/429/5xx 最多自动重试两次，使用短暂退避；404 等直接失败。
  用户可保留来源选择直接重新尝试。手动重试从新的临时目录开始，不复用残缺下载。
- 默认每个 ZIP 最大 512 MiB、解压总量 2 GiB、单文件 64 MiB、100,000 个条目、压缩比 1000。
  内部调用可通过 `ArchiveLimits` 调整；超限会失败并保留当前题库。
- 初始化程序构建期间阻止加载 engine；关闭窗口会先取消，等待工作线程结束后再退出。

## 可调用 API

```python
import threading
from bagu_trainer.bank_build.service import BankBuildService, load_sources

sources = load_sources()  # repository -> BankSource，包含 commit、directory、声明元信息
cancel = threading.Event()
service = BankBuildService()
result = service.build(
    ["Snailclimb/JavaGuide"],
    cancel=cancel,
    timeout=900,
    download_retries=2,
    request_timeout=5,
    progress=lambda event: ui_queue.put(event),
)
# 其他线程可调用 cancel.set()
```

`BuildProgress` 包含 `stage/message/completed/total/attempt`。`total=0` 表示当前阶段未知总量，
界面使用活动指示。阶段包括 `prepare/download/retry/unpack/extract/import/tag/concepts/validate/activate/done`，
终止事件为 `failed/cancelled/timeout`。进度回调在工作线程执行，调用方应投递到 UI 队列。

成功返回 `BuildResult(generation, directory, questions, previous_directory)`。
取消、超时、失败分别抛出 `BuildCancelled`、`BuildTimeout`、`BuildError`；错误包含 `stage`。
`versions={repository: full_commit}` 可覆盖固定版本，仅接受完整 40 位 commit。
`opener` 可注入用于离线 HTTP 测试，生产默认使用受约束的标准库 HTTPS opener。

纯构建核心位于 `bagu_trainer/bank_build/`：

| 函数 | 职责 |
| --- | --- |
| `extraction.extract_sources(selected, repos_dir, out_dir, checkpoint=...)` | 本地提取与去重，返回导出记录 |
| `importer.build_bank(export, repos_root, excerpt_limit, checkpoint=..., stable_ids=...)` | 导入并提取来源正文，返回题库和统计 |
| `tagging.tag_questions(questions, checkpoint=...)` | 在调用方题目记录上构建标签，返回报告 |
| `profiles.build_profiles(questions, checkpoint=..., use_fallback=False)` | 在内存构建并验证概念档案及报告 |

原四个 `scripts/*.py` 入口仍可用，作为内部模块的薄适配器。旧命令行路径继续操作旧式
`data/*.json`；它不会覆盖已启用的版本目录。旧下载 PowerShell 与可选 `gh --fetch-issues`
仅保留给显式命令行使用，桌面服务不导入或执行它们。

后续可在服务之上添加历史版本选择/回退、磁盘清理和来源版本维护；不要让这些功能修改学习
数据库。若将构建入口开放到运行中的主窗口，需要明确在重启时启用，或另行实现引擎一致性重载。
当前桌面入口只在 engine 加载前提供，服务本身切换磁盘指针，不替换已在内存中的引擎对象。

## 验证

测试不下载真实第三方内容，使用本地 HTTP 服务及合成 ZIP。覆盖成功流水线、断流、HTTP 404/503、
自动/手动重试、socket 和总超时、每阶段取消、非法 ZIP、链接和资源限制、构建异常、指针提交失败、
旧数据保留、进程强制中断后的新尝试，以及真实 Tk 来源选择、取消和重试。
另在没有 `scripts` 目录、PATH 为空且禁止创建子进程的环境中验证完整内部构建。

```powershell
python -m pytest release_tests -q
python -m pytest tests -q
```

本次结果：发布流程 96 项通过（包含新增题库构建 45 项），现有功能回归 171 项通过，
共 267 项。`git diff --check` 通过。未对真实第三方来源执行下载或覆盖本机题库。
