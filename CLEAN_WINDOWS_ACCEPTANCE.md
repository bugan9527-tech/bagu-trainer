# 干净 Windows 验收

目标：Windows x64，不安装 Python、Git，不配置 MCP，解压生产 ZIP 后可以使用。
验证模型接口统一使用本地固定响应，不需要真实 Key，也不产生模型费用。

## 自动验收包

`BaguTrainerAcceptance-windows-x64.zip` 是单独的诊断附件，**不在生产 ZIP 内**。
它包含冻结的验收入口和与生产包同一提交的应用模块，在进程内通过现有依赖注入接口使用
合成来源 ZIP、回环 HTTP 模型和 Tk 回调。监听器、匹配器、出题器、判分器和 SQLite 均为真实实现，
保留正常 15 秒防抖，不通过放宽匹配阈值或替换 engine 来强制出题。

在干净 Windows 的两个目录分别解压生产包和验收包，然后运行：

先在构建机使用同一 `--output-dir` 生成两个包，再执行
`python scripts/portable_artifacts.py --output-dir <构建输出> --with-driver --report <新报告路径>`。
将两个原始 ZIP 及各自 `.sha256` 保留在解压目录的父目录（与两个 onedir 并列）；
冻结入口在启动任何 GUI/凭据夹具前检查版本、完整源码指纹、清单及附件对应关系。
目录分开放置时通过 `--archive` 指定生产 ZIP，驱动 ZIP 仍放在驱动 onedir 旁。

```powershell
$job = Start-Process -FilePath 'C:\验收程序\BaguTrainerAcceptance\BaguTrainerAcceptance.exe' `
  -ArgumentList '--work "C:\验收结果\自动 验收" --production-exe "C:\生产程序\BaguTrainer\BaguTrainer.exe"' `
  -WindowStyle Hidden -Wait -PassThru
$job.ExitCode
```

`--work` 必须是尚不存在的目录；退出码 0 且 `ACCEPTANCE.json` 的 `passed`、`frozen` 为 true
才代表通过。失败时读取 `ERROR.json` / `failure.json`，不要只看进程是否打开。
默认使用隔离配置对应的原生 Windows 凭据项，完成后删除该测试凭据，不读写日常凭据。
GitHub runner 使用 `--memory-credentials`，报告会明确标为内存凭据模拟。

自动覆盖：首次向导、503 下载失败及自动/手动重试、安全解压/提取/导入/标签/概念、
非法地址/404 模型配置及恢复、主动测试与保存、项目选择、真实文件变化、监听防抖、
本地模型出题、Tk 弹题、错误答案判分/解析、防重复提交、SQLite 和复习计划保存、
关联审核首次拒绝后不入库/不弹窗、用户显式重试并审核通过后出题、
在配置窗口修改模型、禁止未测试保存、重新测试并保存新验证凭证，
新进程直接进入主窗口及恢复记录，且不会再请求模型。提供生产 EXE 后还会用生产 EXE 及其第二个干净目录
启动同一份学习数据，检查升级后记录保留。

验收结果会记录本地模型名及请求计数，并尝试将向导、配置、判分、重启和生产 EXE 的真实窗口保存到 `screenshots/`。
审核响应也是固定测试数据，只验证协议与拒绝/恢复链路，不代表真实模型的审核准确率。
截图失败会单独记录原因；文件成功生成不是自动视觉通过结论，仍需打开实际图片检查。

这不是对生产 EXE 进行鼠标点击的自动化录像。它验证相同应用代码的真实调用链，
生产 EXE 的按钮操作仍需下面的人工复验；模拟 ZIP 不证明真实 GitHub 来源在用户网络中可达。
工具的 PATH 被清空只能证明未使用外部命令，不能证明宿主操作系统未安装开发工具。

## Windows Sandbox / 干净虚拟机

在已支持并启用 Windows Sandbox 的宿主上执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\acceptance\create-sandbox.ps1 `
  -BuildOutputDirectory $buildOutput -OutputDirectory "$evidence/sandbox"
```

生成的 `.wsb` 仅映射两个 ZIP、校验文件和验收引导脚本，不映射整个仓库或个人数据。
宿主先执行统一只读预检；`-BuildOutputDirectory` 对应构建的 `--output-dir`，未指定时读取 `BAGU_PORTABLE_*`，不扫描历史 ZIP。
打开该文件后自动在新的 Windows Sandbox 内执行冻结验收，结果写回所选输出下的 `clean-windows-results/`。
同一输出目录不重复使用，避免混入上一次的输入或结果。
脚本不会安装 Python/Git、配置 MCP、启用 Sandbox 功能或重启宿主。宿主缺少 Sandbox 时，
请使用独立 Windows x64 虚拟机，按上方命令运行，并保存系统版本和开发工具未安装的证据。

只有 `ENVIRONMENT.json` 中干净系统检查成立、验收程序退出 0 且 `ACCEPTANCE.json` 全部通过，
才能把这次记录标为“干净 Windows 自动验收通过”。不要把 GitHub runner 或开发机记录改名充数。

## 生产 EXE 人工复验

在干净系统中使用 `scripts/acceptance/launch-clean.ps1`（依赖相邻的 `mock-model.ps1`）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\acceptance\launch-clean.ps1 `
  -Archive 'C:\Downloads\BaguTrainer-0.4.0-windows-x64.zip' `
  -TargetReport 'C:\Downloads\PORTABLE_TARGET.json' -OutputDirectory 'C:\人工验收'
```

`PORTABLE_TARGET.json` 为构建机统一预检成功的报告，随对应 ZIP 及校验附件转移。
此脚本核对报告绑定的版本、源码/清单摘要、ZIP SHA-256 与解压后的文件清单，记录实际包路径、版本及摘要，
再启动仅监听 127.0.0.1 的模型模拟器和生产 EXE；已有解压目录拒绝复用。
默认发现 Python/Git 命令会拒绝；`-AllowDeveloperMachine` 仅用于补充验证，报告仍标为非干净机器。
模拟接口参数和测试项目路径在 `SESSION.json`，固定 Key 是公开测试数据，不能用于真实服务。

按顺序记录界面和结果：

1. 首次向导选择一个真实来源构建题库。可先加 `-FailDownload` 令代理指向未监听的本地端口，
   验证自动重试和失败提示；退出，再不带此开关启动，同一来源可以重新构建。
2. 模型地址填 `SESSION.json` 的 `baseUrl`，先用 `missing-model` 测试 404，再改成 `local-model`。
   使用 `fixtureKey`，主动测试成功后保存；输入框应清空，配置和日志中不出现 Key。
3. 添加 `watchedProject`，进入主窗口。等监听基线建立后，在 `query.py` 增加与已下载知识点相关的
   有效代码改动，等待稳定约 15 秒和一次扫描周期，确认练习中心待答徽标更新而当前页不被抢走，再进入该题。
4. 在内嵌练习中心选择 B，确认判为错误、正确答案 A、显示解析和复习间隔。重复提交不能产生第二条作答；在答题记录查看快照并重练后，应新增且仅新增一次记录，原作答保留。
5. 正常退出并重启，直接进入主窗口，记录及待复习状态保留。换一个干净程序目录重复启动，
   用户数据继续使用原目录。已有自动验收也会检查真实 SQLite，而不是只确认文件存在。
6. 记录系统版本、压缩包 SHA-256、日期、模拟接口计数、通过/失败项和未覆盖项。

模拟器只服务于验收；完成后退出测试 EXE，并按 `SESSION.json` 中已记录的 mock PID 停止模拟器。
Sandbox 关闭后测试系统、临时凭据和本地配置会被丢弃。

当前实测情况及实际未覆盖项以 [VALIDATION.md](VALIDATION.md) 为准。
