# AI 八股训练器

把日常代码修改变成与当前任务相关的面试练习：监听选定项目的代码变化，匹配题库知识点，
结合来源正文和工作上下文生成四选一情景题，作答后查看解析并记录掌握度与复习计划。

所有新题必须由 AI 生成；模型不可用、正文不足或输出不合格时提示失败与重试，不展示离线回退题。
复习可复用此前 AI 生成的题目；旧离线题和来源不明快照不再交付，已有作答记录与统计保留。
新题还须通过上下文/来源引用校验及单独的 AI 关联审核。正常生成通常需要两次模型请求，
共用配置的超时预算；服务商可能分别计费，失败不会自动循环调用。流程和限制见 [AI 出题规则](AI_QUIZ_POLICY.md)。

这是一个独立的 Windows 桌面程序：常驻侧栏与右侧工作区承载工作台、练习中心、答题记录、监听项目、题库管理、模型配置和应用设置七个页面。

**Windows x64 便携版：解压完整目录，双击 `BaguTrainer.exe`。** 无需安装 Python、Git 或
PowerShell；保留旁边的 `_internal`。使用、旧数据迁移和升级见 [便携版说明](PORTABLE_README.md)，
构建与实际 EXE 验证见 [Windows 发布](WINDOWS_PORTABLE.md)。下方命令适用于源码运行。

**首次使用需要先构建题库并配置模型。** 发布包不附带第三方完整题库或人工验证
离线回退题；本地构建和来源说明见 [题库发布与构建](QUESTION_BANK.md)。

## 下载便携版

打开 [Release 页面](https://github.com/bugan9527-tech/bagu-trainer/releases)，在 Assets 下载
`BaguTrainer-<版本>-windows-x64.zip` 和同名 `.sha256`，完整解压后双击 EXE。
草稿仅仓库有权限的用户可见；正式发布前请从草稿 Assets 下载，公开固定链接此时尚不可用。
首次向导依次完成来源选择及构建、模型连接测试和保存、监听项目选择。无需配置 MCP。
配置、题库和学习记录位于 `%LOCALAPPDATA%\BaguTrainer\`，升级时保留该目录。

维护者可在 Actions 运行 **Windows x64 portable draft**，填写与源码版本一致的 tag。
工作流构建、测试、生成 SHA-256，并上传草稿 Release；发布说明提供下载入口和中文首次使用步骤。
工作流进入默认分支后才有手动按钮；PR 自动检查只构建和测试，不创建 Release。
流程、权限及失败重跑见 [发布说明](WINDOWS_PORTABLE.md)。
v0.2.0 整合七页桌面工作区、切题与提交互斥、暂停期间继续练习，以及项目与发布内容清理。
功能状态见 [改版进度](UI_REDESIGN_PROGRESS.md)，每次发布附带对应提交的测试与冻结验收结果。

[验证记录](VALIDATION.md) 区分实际 EXE/冻结调用链测试和干净系统验证；当前开发机已装 Python/Git，
禁用 PATH 不代表已完成全新系统验收。另附 [干净 Windows 验收包和流程](CLEAN_WINDOWS_ACCEPTANCE.md)。

## 功能与边界

- 通过主窗口添加监听项目；比较监听开始后的文件快照，即使改动已自动提交到 Git 也能检测。
- 文件稳定约 15 秒后匹配知识点，新题更新练习中心待答徽标，不抢走当前阅读或输入页面；人工修改也可能触发。
- 在内嵌练习中心选择 A–D、提交答案、查看解析，作答及复习计划保存到本地 SQLite。
- 切页保留当前题目、未提交选择和任务状态；Ctrl+R 进入练习中心（表单输入时不拦截）。待答列表只读，明确进入某题时才交付该题。
- 主窗口“答题记录”按每次作答回顾历史，支持正误、项目和知识点/题干关键词筛选，每页 20 条；已移除项目及旧题库的题目仍从快照查看。时间按本机时区显示。
- 历史详情可“重新练习本题”：复用合格的 AI 快照，不调用模型，进入练习中心；同一来源作答未完成的重练复用题号，提交后新增一次记录。旧快照缺失字段或不符合准入规则时解释原因并禁用重练。
- 普通切页不停止监听、不重建基线；项目实际保存后串行应用，未变化项目基线保留。题库构建、模型测试和保存不因隐藏页面取消。

程序不保证每次改动都有题。未匹配知识点、缺少可靠来源、模型接口不可用或上下文重复时，
可能没有题目。代码改动稳定只是触发时机的近似判断，不能精确识别 AI 任务何时结束。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 桌面及当前用户登录自启动 |
| Python | 仅源码运行需要 3.10 或更高及 Tcl/Tk；便携版已内置运行库 |
| 下载题库 | 桌面内置 HTTPS ZIP 下载和构建，无需 Git 或外部构建命令 |
| 模型接口 | 支持 `/chat/completions`、Bearer Key 认证和文本 JSON 输出的兼容接口 |
| 网络 | 首次安装依赖、下载来源及调用远程模型时需要连接相应服务 |

运行时依赖见 [requirements.txt](requirements.txt)，测试依赖见
[requirements-dev.txt](requirements-dev.txt)，便携包构建依赖见
[requirements-build.txt](requirements-build.txt)。

历史 MCP 配置、演示和重复生成物的清理范围及保留理由见 [清理记录](CLEANUP_REPORT.md)。

## 快速开始

### 1. 下载源码并安装依赖

克隆本仓库，或在 GitHub 的 Code 菜单下载 ZIP 并解压。在包含 `requirements.txt` 的
项目根目录打开 PowerShell。以下命令均在该目录执行，每步成功后再进行下一步。

```powershell
git clone https://github.com/bugan9527-tech/bagu-trainer.git
cd bagu-trainer
```

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

后续命令直接使用虚拟环境解释器，无需激活环境。若使用已有环境，请统一替换解释器路径，
保证安装依赖和启动桌面使用同一个 Python。

### 2. 选择来源并在本地构建题库

安装依赖后直接启动桌面程序：

```powershell
.\.venv\Scripts\python.exe -B scripts/start_desktop.py
```

首次使用会打开中文分步向导。在第 1 步从 [来源清单](interview-banks/README.md) 选择来源并阅读声明，
点击“下载并构建 / 重试”。
程序通过 HTTPS 下载清单固定 commit 的 ZIP，安全解压后依次完成提取、导入、标签和概念构建。
窗口显示阶段进度，可取消、调整总超时或失败后重试；不抓取 GitHub Issues。

源码版和便携版都将本机文件放入 `%LOCALAPPDATA%\BaguTrainer\`；下文运行数据相对该目录。
成功后使用 `data/banks/<版本>/` 下的完整题库，通过原子指针切换；已有 `data/questions.json`
和学习数据库保持原样。已有可用题库的用户可跳过下载。流程、接口及发布范围见
[QUESTION_BANK.md](QUESTION_BANK.md) 和 [BANK_BUILD.md](BANK_BUILD.md)。

### 3. 配置出题模型

题库构建成功后，向导自动进入第 2 步，填写：

| 字段 | 填写方式 |
| --- | --- |
| `base_url` | 服务商提供的 API 基础地址，不包含末尾的 `/chat/completions` |
| `api_key` | 该接口的访问 Key，保存时写入 Windows 凭据管理器 |
| `model` | 服务商为你的账户提供的实际模型标识 |
| `timeout_sec` | 单次请求超时秒数 |

程序会在 `base_url` 后追加 `/chat/completions`。主动点击“测试连接”会在后台发送一个小请求，
可能产生服务商计费；测试成功后点击“保存并继续”。测试失败时可按提示修改后重试，也可取消测试。
Key 写入现有 Windows 凭据管理器并回读确认，配置文件不保存 Key 明文。连接成功不保证模型
一定能生成符合要求的题目。修改地址、模型或 Key 后需要重新测试。

无需先复制 `config.example.json`。配置窗口会使用当前用户的默认值并在用户数据目录创建
`config.json`。`config.example.json` 中的空 `trae_home` 表示使用当前用户的 `.trae-cn`，
空 `project_roots` 在桌面启动时会进入初始化界面，要求显式选择监听项目；相对 `db_path`
按用户数据目录解析，与安装目录和终端所在目录无关。其他项目也可通过主窗口添加。
如已有环境变量 `BAGU_LLM_API_KEY`，它优先于凭据管理器中的 Key；排查旧 Key 或迁移时
参见 [KEY_MIGRATION.md](KEY_MIGRATION.md)。

### 4. 选择监听项目并进入主窗口

```powershell
.\.venv\Scripts\python.exe -B scripts/start_desktop.py
```

1. 在向导第 3 步添加要监听的代码项目文件夹，点击“保存项目并继续”，最后点击“进入主窗口”。
   向导已打开时无需重复启动程序。退出后重开会恢复已完成步骤；旧用户配置完整时仍直接进入主窗口。
2. 在该项目中进行与题库知识点相关的源码修改并保存，等待文件稳定约 15 秒。
3. 命中且生成成功后，在题目窗口作答并查看解析。

添加项目或启动程序时会先建立基线，旧改动不会立即当作新任务。模型请求和扫描还会增加等待时间。
监听不再依赖未提交的 Git 差异，避免自动提交或过大的历史差异掩盖新修改。
未命中知识点时，窗口会显示题库数量及“未调用 AI”；日志记录匹配、重复跳过和生成结果的数量，不记录代码正文。
题库构建同时导入问句和普通知识点标题；早期仅导入问句而题量很少的本机题库，需要重新构建。
主窗口可暂停监听，也可重新打开配置窗口；放下题目后用“继续答题”恢复。
快捷键：主窗口 `Ctrl+R` 继续答题，题目窗口 `A`–`D` 选项、`Ctrl+Enter` 提交。
退出时关闭主窗口并确认停止。再次运行启动命令会打开已有实例。

启动检查不会调用模型、重建题库或修改学习数据库。配置损坏、目录失效或引擎初始化失败时，
可留在向导中修复后重试；登录后台启动遇到缺项也会显示向导。连接测试仅在用户点击时执行，
重开不会自动请求模型。说明见 [首次使用向导](FIRST_USE.md) 和 [启动协调层](STARTUP.md)。

## 可选：登录后自动启动

主窗口“登录自启动”菜单可直接启用或取消。便携版快捷方式指向当前 EXE；移动程序后重新启用即可。
也可执行 `BaguTrainer.exe --install-startup` / `--uninstall-startup`。

先完成题库、模型配置并确认能正常打开桌面窗口。安装脚本优先使用项目中的
`.venv/Scripts/python.exe`，不存在时才使用 PATH 中的 `python`：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-startup.ps1
```

它会在当前用户的 Startup 文件夹创建快捷方式，并立即最小化启动程序。
这是用户登录后的启动，不是系统服务。取消自启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-startup.ps1 -Uninstall
```

若使用其他虚拟环境，可给安装命令加 `-PythonExe "你的环境目录\Scripts\python.exe"`。
取消后仍在运行的桌面程序需自行退出；学习记录保留。移动项目或 Python 环境后需要重新安装快捷方式。
更多说明见 [DESKTOP_MODE.md](DESKTOP_MODE.md)。

### 换目录或换电脑

在新位置重新创建 `.venv` 并安装依赖，不要直接复制旧虚拟环境。
源码和便携版在同一 Windows 用户下共用 `%LOCALAPPDATA%\BaguTrainer\`，换程序目录后学习记录仍在。
旧版曾将配置和数据放在项目旁，使用首次向导“迁移旧数据…”或 `--migrate-from "旧项目目录"`
显式复制，旧目录保留且不会覆盖已有用户数据。换电脑需要在退出程序后另行备份用户数据，
核对监听项目绝对路径、重新保存 Windows 凭据并更新自启动；凭据不随目录复制。

## 数据与隐私

- 模型出题会向你配置的接口发送经过筛选和脱敏的项目上下文片段及题库来源正文。
  选择服务商时需要考虑自己项目的数据使用要求。
- 文件采集受项目白名单及敏感文件排除规则限制；会话摄取默认关闭。
- 新保存的 API Key 进入系统凭据管理器，本机配置不保存新 Key 明文。
- 用户数据根目录为 `%LOCALAPPDATA%\BaguTrainer\`；学习记录默认位于其 `data/trainer.db`，
  进程运行文件在 `runtime/`，滚动日志在 `logs/`。程序目录可只读。
- 本机配置、学习数据库、运行认证信息及完整题库均已通过 `.gitignore` 排除。
  手工制作发布附件时仍需遵守 [题库发布范围](QUESTION_BANK.md)。

## 常见问题

| 现象 | 排查方式 |
| --- | --- |
| 找不到 `python` 或 `git` | 确认对应程序已安装且位于 PATH，重新打开终端后检查版本 |
| 缺少 `keyring` | 用启动程序的同一解释器执行 `-m pip install -r requirements.txt` |
| `tkinter`、Tcl/Tk 初始化失败 | 为当前 Python 安装 Tcl/Tk 支持；用 `.\.venv\Scripts\python.exe -B scripts/start_desktop.py --check-runtime` 检查窗口环境 |
| 提示题库不存在 | 在初始化界面“下载并构建题库”；测试合成数据不能替代完整题库 |
| 来源下载失败 | 检查 HTTPS 网络及系统代理；瞬时错误会自动重试，也可在窗口重新尝试 |
| HTTP 401 / 404 / 429 | 分别检查 Key/授权、基础地址与模型路径、额度及请求限制；环境变量中的旧 Key 可能覆盖新保存的 Key |
| 模型连接超时或题目生成失败 | 检查接口网络、超时设置、模型标识，以及是否支持要求的 JSON 输出 |
| 修改代码后没有待答题 | 查看练习中心徽标；确认监听未暂停、修改发生在基线建立之后，检查项目范围、来源正文及模型状态 |
| 导入提示题量减少超过 5% | 数据保护已生效；在独立目录构建新来源，保留原题库和学习数据 |

## 开发与验证

本次干净目录安装、实际来源下载、桌面安装与运行结果见
[全新安装流程验证记录](VALIDATION.md)，其中列明了实际环境及未验证范围。

安装开发依赖，执行不依赖本机完整题库的发布流程测试：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest release_tests -q
```

这些测试覆盖来源选择、默认不联网抓取 Issues、缺失题库提示、无旧数据的首次构建，
以及中文和空格目录中的配置定位与自启动解释器选择。自启动测试只写临时快捷方式，不安装实际登录自启动。
`tests/` 中匹配器的历史评估通过 `BAGU_TEST_BANK_ROOT` 显式指定项目外只读基准；普通单元测试使用临时合成题库，详见 [验证范围](VALIDATION.md)。内容链路的归档读取/AI-only 测试现使用自编合成夹具，不再依赖未分发的离线归档。
完整检查包括 `python -m pytest release_tests tests -q -W error`；便携包需先构建后再运行，以免 EXE 用例因缺包跳过。指定验收包可设置 `BAGU_PORTABLE_BUNDLE` 和 `BAGU_PORTABLE_ARCHIVE`。验证前将 `BAGU_USER_DATA_DIR` 指向独立目录；测试只写临时配置和数据库。

| 路径 | 用途 |
| --- | --- |
| `bagu_trainer/` | 桌面、匹配、出题和学习记录逻辑 |
| `scripts/` | 桌面入口、自启动和本地题库构建脚本 |
| `data/` | 本机旧版数据可能保留于此；新运行数据默认在用户目录，不随源码发布 |
| `interview-banks/` | 可发布的来源清单；新下载的上游副本在用户目录 |
| `release_tests/` | 源码发布与首次构建测试 |
| `tests/` | 完整功能与历史题库回归 |

## 许可与第三方内容

本项目原创源码及文档采用 [MIT License](LICENSE)。允许使用、修改、分发及商用，
包括在闭源产品中使用；分发代码或其重要部分时须保留版权声明和许可证文本。
上游题库的许可证和版权声明独立适用，详见 [来源清单](interview-banks/README.md)。
源码发布方案不包含第三方完整题库；本地下载、使用或后续再分发时需核对相应条款。

## 桌面视觉规范

统一主题、中文字体、间距和按钮规范见 [UI_DESIGN.md](UI_DESIGN.md)，新增运行组件声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。源码运行前执行 `python -m pip install -r requirements.txt`；便携包已包含主题依赖。
窗口缩放、长文本、键盘和实际显示覆盖范围见 [UI_DISPLAY.md](UI_DISPLAY.md)。可用 `python scripts/check_ui_display.py` 生成隔离窗口截图供人工检查；Tk 模拟缩放不代表原生 Windows 对应比例的验收。
