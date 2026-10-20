# Windows 便携版验证记录

本轮日期：**2026-10-21（Asia/Shanghai）**。目标为 Windows x64 onedir、手动 GitHub Actions
构建及草稿 Release，以及首次使用到学习记录恢复的验收。下方 2026-10-20 段落仅为旧源码布局的历史记录。

## 本轮实际环境

| 项目 | 实际情况 |
| --- | --- |
| 宿主系统 | Windows 10 专业版 x64，10.0.19045 |
| 构建工具 | CPython 3.10.11 x64、PyInstaller 6.22.3、keyring 25.7.0 |
| 冻结运行库 | Tcl/Tk 8.6、SQLite、TLS、Windows WinVaultKeyring 均实际加载 |
| 开发工具 | 宿主已安装 Python/Git；被测 EXE 的 PATH 清空，不继承 PYTHONHOME/PYTHONPATH 或真实模型 Key |
| MCP | 不配置、启动或调用 MCP；源码测试禁止协议 SDK 导入，生产包无此运行依赖 |
| 数据和凭据 | 临时中文/空格目录，独立用户数据及隔离 Windows 凭据项，测试后清理测试 Key；日常配置和凭据未用于验收 |
| 模型 | 仅 127.0.0.1 HTTP 固定响应，无真实模型、真实 Key 或计费服务 |
| 题库输入 | 自行编写的 10 个概念构成合成 ZIP，不下载或发布第三方完整题库 |
| Sandbox | 当前宿主未启用，未实际运行；已生成 `.wsb` 和干净系统引导脚本 |
| GitHub runner | 工作流指定 windows-2022/x64；云端实际结果以 Actions run 为准，不能以配置或本机结果代替 |

**本机结果不等同于“未安装 Python/Git 的全新 Windows 已验证”。**
这一条件仍需按 [干净 Windows 验收流程](CLEAN_WINDOWS_ACCEPTANCE.md) 在新的 Sandbox/虚拟机补验。
报告保留 `clean_machine_claim: false`，不会因为 PATH 为空就声称干净环境通过。

## 本轮自动测试及 EXE 验收

```powershell
python -m pytest release_tests -q -W error --junitxml=validation/release-tests.xml
python -m pytest tests -q
.\.venv-build\Scripts\python.exe scripts/build_portable.py
.\.venv-build\Scripts\python.exe scripts/build_acceptance.py --verify --work build/native-acceptance
git diff --check
```

发布流程 **160 项**、历史功能回归 **171 项**，合计 **331 项**。历史回归使用本地快照，
快照不进入发布附件。另有独立冻结验收报告 `build/<验收目录>/ACCEPTANCE.json`，发布时复制为
`validation/ACCEPTANCE.json`。生产包和冻结报告记录构建提交与脏工作树状态，上传时拒绝不匹配结果。

| 场景 | 实际结果和证据 |
| --- | --- |
| 解压启动 | 实际 ZIP 解压到中文和空格目录，生产 EXE 创建首次窗口，Tcl/Tk 与资源定位正确 |
| 程序目录不可写 | ACL 拒绝文件及属性写入，写入探针失败；向导/配置窗口仍创建，程序 hash 不变 |
| 题库初始化 | 冻结验收调用真实 Tk 来源面板及构建服务：合成 ZIP 下载、安全解压、提取、导入、标签、概念和原子启用 |
| 下载失败重试 | 前 3 次 HTTP 请求返回 503，自动重试后提示失败，未启用部分题库；手动重试第 4 次成功 |
| 错误配置 | 非法地址在联网前报错；不存在的模型返回 404，并保留可重试状态；修正后测试成功 |
| 模型保存 | 测试通过才保存、清空 Key 输入框、配置无 Key 明文；本机冻结验收实际写入/回读隔离的原生 Windows 凭据，CI 明确使用内存凭据 |
| 监听和弹题 | 真实项目基线、`.py` 文件变化、正常 15 秒防抖、未修改阈值的匹配器、HTTP 出题器与 Tk 四选项弹题 |
| 判分和记录 | B 判错、正确答案 A、解析及复习间隔；重复提交不重复记分。SQLite 有 1 次作答、1 道已答题和掌握度/复习时间 |
| 重启恢复 | 新进程直达主窗口，不重复弹已答题，学习字段和题库 hash 保留，不再次调用模型 |
| 生产 EXE 升级 | 同一份完成的用户数据先供生产 EXE 使用，再供第二个干净程序目录使用，均进入 running，学习字段相同 |
| 单实例 | 初始化和主窗口阶段重复启动保留原管道凭据；跨程序目录不创建第二个引擎 |
| 迁移 | SQLite backup、默认/相对/绝对 DB 路径、取消、损坏 DB、凭据失败、启用失败回滚、冲突保护、旧实例运行及重试 |
| 自启动 | 临时 `.lnk` 指向 EXE 和 `--background`，删除成功；未修改真实 Startup 文件夹 |
| 发布清单 | ZIP/manifest/SHA-256 一致，不含完整第三方题库、配置、日志、数据库、源码脚本或测试 |
| 发布保护 | 错误校验和/版本/提交、脏构建、失败或跳过测试、缺失冻结验收均拒绝上传；正式 Release 拒绝覆盖；同提交草稿可重试 |

冻结验收网络计数为 **4 次下载、1 次有效连接测试、1 次出题、1 次模型 404**。
注入的只有来源传输和模型 HTTP 模拟，监听器、匹配器、学习引擎、判分器和 SQLite 均为真实代码。
指定生产 EXE 后，还会检查生产 EXE 和新程序目录消费相同数据；这不等同于生产 EXE 全流程鼠标操作录像。

## 工作流及未覆盖项

工作流支持 `workflow_dispatch`；PR 只构建/测试。手动运行且所有检查通过后，独立 job 才有
Release 写权限并上传草稿。官方 `actionlint 1.7.12` 本地检查通过；云端 run 和草稿链接以实际创建结果为准。
首次 windows-2022 执行发现西文代码页下的编码问题：内部提取器打印中文路径会抛出
`UnicodeEncodeError`，旧 `WScript.Shell` 快捷方式读写不能可靠往返中文路径。
已将内部构建输出改为日志，源码及 EXE 统一使用原生 `IShellLinkW`，测试也改为 Unicode 读取；
构建中断用例显式使用 `cp1252:strict` 重现非中文环境。修复后的云端结果以对应 run 为准。
手动按钮需要工作流进入默认分支，依据 [GitHub 官方规则](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)。

- **尚未在未安装 Python/Git 的全新 Windows 上执行。** 已提供 `.wsb` 和无需额外开发工具的引导/验收包，待具备 Sandbox 或干净 VM 的机器执行。
- 本机 computer-use 读到窗口标题和控件树，但截图连续返回 `FrameArrived timed out` / `window capture timed out`。
  已停止盲目点击；完整生产 EXE 的人工向导复验仍待补。
- 本轮没有下载真实完整上游 ZIP，不证明用户网络、代理或真实来源始终可用。先前单文件构建记录不替代此次完整来源验收。
- 未验证真实模型服务商、计费、授权和题目质量、Windows 11、长期运行、安全软件兼容、系统重启登录或自动更新；程序尚未代码签名。
- 升级为第二个干净目录运行同版本程序的替换模拟，未涉及未来数据库 schema 迁移。
- 冻结验收程序、PowerShell 模拟器作为诊断工具单独提供，不进入生产 ZIP。

使用见 [README.md](README.md)，发布和重试见 [WINDOWS_PORTABLE.md](WINDOWS_PORTABLE.md)，
干净系统步骤见 [CLEAN_WINDOWS_ACCEPTANCE.md](CLEAN_WINDOWS_ACCEPTANCE.md)。

---

# 2026-10-20 历史源码版验证（旧数据目录布局）

验证日期：2026-10-20。当前应用使用独立的桌面学习引擎，不注册或启动 MCP 服务。

## 功能回归

在 Windows 10、Python 3.10.11 环境执行：

```powershell
python -m pytest tests release_tests -q
```

**179 项通过**。覆盖匹配、来源校验、隐私脱敏、项目隔离、去重、防抖、作答、
复习计划、SQLite 持久化、真实 Tk 控件和窗口单实例机制。
完整历史回归使用本地历史题库；源码下载版的独立回归入口仍为 `release_tests/`。

移除了协议工具表和转发测试，新增桌面及配置窗口的启动验证：即使禁止导入协议 SDK，
窗口仍可创建、监听并正常退出。窗口管道仅允许显示主窗口和查询窗口状态；
出题、作答及其他学习操作不能通过该管道调用。

## 干净环境验证

另行将待发布文件复制到含中文和空格的新目录，不带本机配置、题库、学习数据库或虚拟环境。
新建不启用系统 site-packages 的 `.venv`，仅安装当前运行依赖及开发依赖。

| 检查 | 结果 |
| --- | --- |
| 依赖安装及 `pip check` | 通过 |
| 协议 SDK 安装状态 | `find_spec('mcp')` 返回 `None`，未安装该包 |
| `python -m pytest release_tests -q` | 8 项通过 |
| 从真实来源文件提取、导入、标签、构建概念档案 | 通过，生成 5 条题目及 5 份档案 |
| Tcl/Tk 运行环境 | 真实初始化通过 |
| 模型配置窗口 | 实际创建、更新并关闭成功 |
| 桌面主入口 | 实际创建主窗口、运行监测线程和单实例窗口管道后受控退出 |
| 数据位置 | 数据库创建于独立副本的 `data/` 目录 |
| 本地 HTTP 模拟出题 | 请求、解析、判分、作答持久化通过，重复提交被拒绝 |

来源构建检查使用 JavaGuide 的 `docs/database/mysql/mysql-index.md` 单个文件，
用于验证没有旧题库时的完整构建流程，没有在本次重构中重新下载所有来源或核定全量题目数量。
干净环境安装得到 `keyring 25.7.0`、`pytest 9.1.1`、`packaging 26.3`。
运行依赖仅声明 `keyring`，其传递依赖由包管理器正常安装。

## 范围与限制

- 真实模型服务商、计费、凭据和题目质量未验证；HTTP 检查使用回环地址上的固定响应接口。
- 子进程不继承真实模型 Key，并使用空凭据后端，不读写用户的系统凭据。
- 自启动测试只在临时目录创建快捷方式，没有更改实际登录自启动。
- 以上是在同一台 Windows 上的隔离验证，不代表已经验证其他系统或长时间监听稳定性。
- 旧客户端中的协议服务配置已不适用于此版本；升级后应退出旧进程，再启动桌面入口。

安装与日常使用见 [README.md](README.md)，题库及历史测试前提见
[QUESTION_BANK.md](QUESTION_BANK.md)。
