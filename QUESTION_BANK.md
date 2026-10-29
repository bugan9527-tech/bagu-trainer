# 题库发布与本地构建

源码和 Windows 便携版默认将运行数据写入 `%LOCALAPPDATA%\BaguTrainer\`。
本文本地构建的 `data/` 和下载副本均在用户目录中；自动化测试使用临时合成夹具。
便携版仅附来源清单和声明，程序资源位于只读 `_internal`，详见 [使用说明](PORTABLE_README.md)。

## 本次发布方案

GitHub 仓库发布训练器源码、导入/去重/标签/概念构建脚本、合成测试及来源清单。
第三方正文及其衍生题库由使用者选择来源后在本地构建，不随源码仓库、Release
附件、安装包、容器镜像或演示文档一起发布。源码的许可证也不替代上游内容许可。

| 文件或目录 | 发布方式 |
| --- | --- |
| `bagu_trainer/`、`scripts/`、`tests/` | 随源码发布 |
| `interview-banks/sources.json`、`interview-banks/README.md` | 发布来源地址、检查版本及许可证据 |
| `interview-banks/repos/`、`interview-banks/deduped/` | 本地下载、提取，不提交 |
| `data/questions.json` | 本地构建，不提交 |
| `data/banks/`、`data/.bank-builds/`、`data/active-bank.json` | 桌面下载、版本题库、临时数据和当前指针，均不提交、不打入发布包 |
| `data/question_concepts.json`、`data/concept_profile_report.json` | 含第三方衍生信息，本地构建，不提交 |
| `data/fallback_quizzes.json` | 当前版本关联第三方来源且尚未完成再分发核对，不提交 |
| 题库备份、清洗报告、学习数据库、题库目录 Word 文档 | 留在本地，不随发布包提供 |

本地现有数据无需删除。`.gitignore` 控制 Git 的收录范围，不会自动过滤手工压缩包，
也不会移除已经提交到 Git 历史的文件；发布附件时同样遵守上表。

## 来源核对结果

2026-10-20 检查的本地主库有 5,443 条记录，`source_paths` 指向 15 个上游仓库，
`source_excerpt` 保存了正文片段；概念摘要也从这些片段及回退题解析中提取。
24 道离线回退题关联 CS-Notes、JavaGuide 和 llm_interview_note。
这些文件不能仅因去重、打标签或改写字段就统一视为本项目原创数据。

已观察到 Apache、MIT、GPL、CC BY-SA、CC BY-NC-SA、CC BY-NC-ND、
自定义版权声明，以及未确认许可的来源。详见 [来源清单](interview-banks/README.md)。
清单基于本地版本，不是逐篇内容授权结论；仓库中的引用文章、书籍节选及 Issue
投稿还需要单独核对。公开可访问并不自动提供任意再分发权限，参见
[GitHub 的许可证说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)。

## 新用户首次构建

0.5.0 Electron：按 [docs/ELECTRON_DESKTOP.md](docs/ELECTRON_DESKTOP.md) 启动源码或安装版，在工作台“题库管理”页选择来源并阅读声明，
点击“下载并构建”；完成后在“模型配置”“监听项目”页完成其余准备。0.4.x Tk 兼容版仍可使用
`python -B scripts/start_desktop.py` 并从首次向导第 1 步进入，历史说明见 [FIRST_USE.md](FIRST_USE.md)（已归档）。下载使用 GitHub HTTPS ZIP 归档接口，锁定
`sources.json` 中的完整 `audited_commit`，随后安全解压、提取去重、导入、标签和概念构建。
整条桌面流程在进程内调用 Python 函数，不依赖 Git、PowerShell 或 Python 子进程。

窗口显示当前阶段和可用的条目/字节进度，提供总超时设置、取消和失败重试。
临时网络错误最多自动重试两次，404 等永久错误直接显示失败。所有阶段在独立目录执行，
成功后才原子切换 `data/active-bank.json`，指向 `data/banks/<版本>/`。
旧式题库文件、旧版本目录和学习数据库均保留；下载或构建失败不会切换当前题库。
详细限制、API 和验证见 [BANK_BUILD.md](BANK_BUILD.md)。

## 可选：保留的命令行工具

以下是旧版 Tk/命令行开发工作流，仍需要 Git 和 PowerShell；0.5.0 Electron 桌面初始化不调用这些外部命令。
先阅读清单中的上游条款，选择适合自己用途的来源。下面以 JavaGuide 说明操作步骤，
示例不构成对其所有内容及所有用途的授权判断。

```powershell
python -m pip install -r requirements.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\clone_banks.ps1 -Repositories Snailclimb/JavaGuide
python scripts/dedupe_questions.py --repos JavaGuide
python scripts/import_questions.py
python scripts/tag_question_knowledge.py
python scripts/build_concept_profiles.py
python -B scripts/start_desktop.py
```

每步成功后再执行下一步。下载脚本的参数是 `owner/repository`，去重脚本的参数是
本地仓库目录名。选择多个来源时，在 PowerShell 中用
`& .\scripts\clone_banks.ps1 -Repositories @("owner/repo1", "owner/repo2")`
的形式传参；来源必须存在于 `sources.json`。
去重脚本只读取 `--repos` 中指定的目录，即使本地还有其他仓库也不会混入。

旧 `clone_banks.ps1` 使用当前上游默认分支，已有副本不会自动更新。
桌面 ZIP 流程使用来源清单的 `audited_commit` 锁定版本，两种入口不应混为同一份构建。
若需要代理，显式传 `-Proxy http://127.0.0.1:端口`；默认不强制指定本机代理。
正常提取只读取本地文件。只有显式添加 `--fetch-issues` 且选中了
`Daily-Interview-Question` 时才通过 GitHub CLI 下载 Issue 标题，且需自行准备 `gh` 登录。

源码版没有附带的人工验证离线回退题。导入主题库后，按
[桌面使用说明](DESKTOP_MODE.md) 配置模型，再生成情景题；未配置模型或来源不足时
允许返回不可出题。测试夹具不会替代实际题库、模型或内容验证。

## 已有用户与测试

本地已经有题库的用户不需要重新构建。使用旧命令行工具重建前退出桌面程序，备份题库及学习数据库；
来源集合变化可能改变题目 ID，旧学习记录不一定能正确对应新 ID。
导入脚本会备份旧题库，并在题量减少超过 5% 时中止写入；这是数据保护，
切换到较小来源集合时不要为绕过检查直接覆盖旧数据，应使用独立目录构建。

桌面模块已使用独立版本目录构建，并完整保留旧数据，因此可以切换到较小的所选来源集合。
新流程使用来源路径和题干生成稳定 ID，避免把旧顺序 ID 的学习记录错误关联到其他题目。
旧学习记录仍在原 SQLite 中；旧顺序 ID 不会自动迁移到新 ID。
旧脚本仍操作旧式 `data/*.json`；已有版本指针时，桌面优先使用指针指定的题库。

现有完整题库回归测试依赖历史的 5,443 条数据、概念报告与 24 道回退题。
从新来源构建的小题库不会自动满足这组快照断言；源码发布版不宣称在缺少这些
本地数据时整套历史回归都能运行通过。发布流程相关测试可独立运行：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest release_tests/test_question_bank_release.py
```

## 将来单独发布题库

先按条目保留来源文件、作者/版权声明、上游版本、许可及修改说明，确认引用内容和
投稿的权利范围。把许可不明确或不适合目标用途的条目排除，完成所需署名与许可文件后，
再决定独立数据包的发布方式。当前来源清单和本地构建流程不替代这些步骤。
