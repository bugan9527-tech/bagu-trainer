# AI 八股训练器

把日常代码修改变成与当前任务相关的面试练习：监听选定项目的代码变化，匹配题库知识点，
结合来源正文和工作上下文生成四选一情景题，作答后查看解析并记录掌握度与复习计划。

这是一个独立的 Windows 桌面程序：文件监听、弹题和作答都在桌面完成。

**首次使用需要先构建题库并配置模型。** GitHub 源码版不附带第三方完整题库或人工验证
离线回退题；本地构建和来源说明见 [题库发布与构建](QUESTION_BANK.md)。

## 功能与边界

- 通过主窗口添加监听项目；支持 Git 差异和无 Git 项目的文件变化检测。
- 文件稳定约 15 秒后匹配知识点，生成题目并弹窗；人工修改也可能触发。
- 在窗口内选择 A–D、提交答案、查看解析，作答及复习计划保存到本地 SQLite。
- 题目可暂时关闭，通过“继续答题”恢复；同一道题不会因重复检查而反复推送。

程序不保证每次改动都有题。未匹配知识点、缺少可靠来源、模型接口不可用或上下文重复时，
可能没有题目。代码改动稳定只是触发时机的近似判断，不能精确识别 AI 任务何时结束。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 桌面及当前用户登录自启动 |
| Python | 3.10 或更高，需包含 Tcl/Tk；当前本地验证环境为 Python 3.10.11 |
| Git / 终端 | Git、Windows PowerShell，用于下载题库来源及执行脚本 |
| 模型接口 | 支持 `/chat/completions`、Bearer Key 认证和文本 JSON 输出的兼容接口 |
| 网络 | 首次安装依赖、下载来源及调用远程模型时需要连接相应服务 |

主程序依赖见 [requirements.txt](requirements.txt)。Redis 只用于根目录 `interface.py`
中的独立示例，不是训练器运行依赖；不需要为桌面程序安装 Redis。

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
git --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

后续命令直接使用虚拟环境解释器，无需激活环境。若使用已有环境，请统一替换解释器路径，
保证安装依赖和启动桌面使用同一个 Python。

### 2. 选择来源并在本地构建题库

先阅读 [来源清单](interview-banks/README.md) 中对应上游的条款，选择适合自己用途的来源。
下面以 JavaGuide 展示构建流程；源码版不会默认下载所有来源。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\clone_banks.ps1 -Repositories Snailclimb/JavaGuide
.\.venv\Scripts\python.exe scripts/dedupe_questions.py --repos JavaGuide
.\.venv\Scripts\python.exe scripts/import_questions.py
.\.venv\Scripts\python.exe scripts/tag_question_knowledge.py
.\.venv\Scripts\python.exe scripts/build_concept_profiles.py
```

完成后应生成 `data/questions.json`、`data/question_concepts.json` 及概念报告。
下载参数使用 `owner/repository`，去重参数使用本地目录名。脚本默认不抓取 GitHub Issues。
多来源、代理、旧题库备份及发布范围见 [QUESTION_BANK.md](QUESTION_BANK.md)。

已有本地题库的用户可跳过此步骤。切换来源集合可能改变题目 ID，应在独立目录试构建，
不要覆盖正在使用的题库和学习记录。

### 3. 配置出题模型

```powershell
.\.venv\Scripts\python.exe scripts/config_window.py
```

在窗口填写并保存：

| 字段 | 填写方式 |
| --- | --- |
| `base_url` | 服务商提供的 API 基础地址，不包含末尾的 `/chat/completions` |
| `api_key` | 该接口的访问 Key，保存时写入 Windows 凭据管理器 |
| `model` | 服务商为你的账户提供的实际模型标识 |
| `timeout_sec` | 单次请求超时秒数 |

程序会在 `base_url` 后追加 `/chat/completions`。“测试连接”会发送一个实际请求，
可能产生服务商计费；测试成功后仍需点“保存”。连接成功也不保证模型一定能输出有效题目。

无需先复制 `config.example.json`。配置窗口会使用当前安装路径的默认值并创建本机
`config.json`。`config.example.json` 中的空 `trae_home` 表示使用当前用户的 `.trae-cn`，
空 `project_roots` 表示默认监听当前训练器安装目录；相对 `db_path` 按安装目录解析，
与终端所在目录无关。其他项目可通过主窗口添加。
如已有环境变量 `BAGU_LLM_API_KEY`，它优先于凭据管理器中的 Key；排查旧 Key 或迁移时
参见 [KEY_MIGRATION.md](KEY_MIGRATION.md)。

### 4. 启动桌面程序

```powershell
.\.venv\Scripts\python.exe -B scripts/start_desktop.py
```

1. 点击“添加监听项目”，选择你要练习的代码项目文件夹。
2. 在该项目中进行与题库知识点相关的源码修改并保存，等待文件稳定约 15 秒。
3. 命中且生成成功后，在题目窗口作答并查看解析。

添加项目或启动程序时会先建立基线，旧改动不会立即当作新任务。模型请求和扫描还会增加等待时间。
主窗口可暂停监听，也可重新打开配置窗口；放下题目后用“继续答题”恢复。
快捷键：主窗口 `Ctrl+R` 继续答题，题目窗口 `A`–`D` 选项、`Ctrl+Enter` 提交。
退出时关闭主窗口并确认停止。再次运行启动命令会打开已有实例。

## 可选：登录后自动启动

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
不带本机配置时，程序默认使用新安装目录及当前用户目录；若保留旧 `config.json`，
需要按新机器的位置核对已经添加的项目绝对路径，并重新安装登录自启动快捷方式。题库和学习数据库若自行迁移，应在程序退出后复制。

## 数据与隐私

- 模型出题会向你配置的接口发送经过筛选和脱敏的项目上下文片段及题库来源正文。
  选择服务商时需要考虑自己项目的数据使用要求。
- 文件采集受项目白名单及敏感文件排除规则限制；会话摄取默认关闭。
- 新保存的 API Key 进入系统凭据管理器，本机配置不保存新 Key 明文。
- 学习记录默认位于 `data/trainer.db`，桌面进程运行文件位于 `data/.desktop-runtime/`。
- 本机配置、学习数据库、运行认证信息及完整题库均已通过 `.gitignore` 排除。
  手工制作发布附件时仍需遵守 [题库发布范围](QUESTION_BANK.md)。

## 常见问题

| 现象 | 排查方式 |
| --- | --- |
| 找不到 `python` 或 `git` | 确认对应程序已安装且位于 PATH，重新打开终端后检查版本 |
| 缺少 `keyring` | 用启动程序的同一解释器执行 `-m pip install -r requirements.txt` |
| `tkinter`、Tcl/Tk 初始化失败 | 为当前 Python 安装 Tcl/Tk 支持；用 `.\.venv\Scripts\python.exe -B scripts/start_desktop.py --check-runtime` 检查窗口环境 |
| 提示题库不存在 | 先完成题库构建；`data/mock/` 不能替代 `data/questions.json` |
| 来源下载失败 | 检查 Git 和网络；需要代理时给下载脚本显式传 `-Proxy`，已有目录不会自动更新 |
| HTTP 401 / 404 / 429 | 分别检查 Key/授权、基础地址与模型路径、额度及请求限制；环境变量中的旧 Key 可能覆盖新保存的 Key |
| 模型连接超时或题目生成失败 | 检查接口网络、超时设置、模型标识，以及是否支持要求的 JSON 输出 |
| 修改代码后没有弹题 | 确认已添加正确项目、监听未暂停、修改发生在基线建立之后；检查知识点匹配、来源正文及模型状态 |
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
`tests/` 下的完整历史回归还依赖本地题库快照和回退题，不能用新构建的小题库替代这些测试数据；
前提详见 [QUESTION_BANK.md](QUESTION_BANK.md)。

| 路径 | 用途 |
| --- | --- |
| `bagu_trainer/` | 桌面、匹配、出题和学习记录逻辑 |
| `scripts/` | 桌面入口、自启动和本地题库构建脚本 |
| `data/` | 模拟输入，以及本地生成的题库与学习数据 |
| `interview-banks/` | 可发布的来源清单，以及被忽略的上游下载副本 |
| `release_tests/` | 源码发布与首次构建测试 |
| `tests/` | 完整功能与历史题库回归 |

## 许可与第三方内容

本项目原创源码及文档采用 [MIT License](LICENSE)。允许使用、修改、分发及商用，
包括在闭源产品中使用；分发代码或其重要部分时须保留版权声明和许可证文本。
上游题库的许可证和版权声明独立适用，详见 [来源清单](interview-banks/README.md)。
源码发布方案不包含第三方完整题库；本地下载、使用或后续再分发时需核对相应条款。
