# 0.4.0 真实 Setup 生命周期验收（历史记录）

> 版本范围：本文记录 0.4.0 Tk 时代的真实 Setup 生命周期验收，保留原始事实。安装器脚本现与 Electron 共用，但**没有**用 Electron 载荷执行这六项真实场景；0.5.0 的构建/验证边界见[项目仓库 docs/ELECTRON_DESKTOP.md](https://github.com/bugan9527-tech/bagu-trainer/blob/main/docs/ELECTRON_DESKTOP.md) 和[项目仓库 docs/ELECTRON_VALIDATION.md](https://github.com/bugan9527-tech/bagu-trainer/blob/main/docs/ELECTRON_VALIDATION.md)。

本流程复用 `scripts/acceptance/installed_release.py`，不新增安装/卸载实现。
`installer_lifecycle.py` 继续用于随机 AppId、重定向目录及合成 EXE 的隔离夹具，
两者共用环境构造、等待及快捷方式工具；夹具结果不能充当真实 Setup 通过。

**当前未执行六项真实安装场景。** 当前开发账户没有可用的可销毁 Windows 环境；
本地只完成脚本回归和现存包的只读预检。工作区证据在
`validation/installed-lifecycle-040-20261025/`，其中 `PLAN.json` 的 `passed=false`、
`production_installer_executed=false`，每项均为 `not_run`。这些本地证据未随仓库分发。

## 环境与保护边界

自动入口只允许真正的 GitHub 托管 Windows 一次性 runner，拒绝工作站和自托管 runner。
`--work` 必须是 `RUNNER_TEMP` 下的新目录，报告也必须使用新路径。
没有 `--force` 或本机绕过参数；不要通过伪造 `GITHUB_ACTIONS` 等环境变量在日常账户运行。
普通本地 VM/专用账户尚未接入自动环境认证，不能把该账户冒充托管 runner；
如需人工操作，必须在可销毁环境中按下表逐项留证，结果不能写入自动通过报告。

执行前检查该一次性账户无训练器安装注册、默认程序/数据目录、快捷方式、
卸载恢复日志及数据位置索引；检查没有持久化的 `BAGU_LLM_API_KEY` / `BAGU_USER_DATA_DIR` 配置，
默认及本次测试凭据槽为空，否则拒绝执行。仅在这些检查后创建合成数据、测试凭据和对应入口。
报告记录 OS、架构、账户 SID、实际安装与数据路径，不记录凭据值。

测试实际安装到该账户默认 `%LOCALAPPDATA%\Programs\BaguTrainer\`；
合成数据显式指向本次 `--work/synthetic-profile`。程序使用正式单实例身份和真实 WinCred 后端，
不用 `BAGU_TEST_INSTANCE`、内存凭据或空后端冒充凭据生命周期通过。
另外创建随机命名的无关测试凭据、无关文件和快捷方式，用于验证保护范围。
只对本次脚本启动的 PID 请求正常关闭；紧急终止也仅限该 PID，并将场景判为失败。

## 输入与只读预检

目标 0.4.0 和真实旧版本各准备一套：

- 实际 `BaguTrainer-<版本>-windows-x64-Setup.exe` 及同名 `.sha256`。
- 对应生产 ZIP 及同名 `.sha256`，完整解压得到 `BaguTrainer/`。
- 同一批次的 `INSTALLER_MANIFEST.json`，生产目录中保留原始 `BUILD_MANIFEST.json`。

版本、AppId、源文件指纹、清单摘要、逐文件内容、ZIP 和 Setup 摘要必须一致。
旧版本必须严格小于 0.4.0，不得用更改版本号或重新编译合成安装夹具替代真实旧 Setup。
默认还要求目标与当前源码一致，正式 CI 必须来自同一干净提交。
仅测试明确指定的历史 0.4.0 包时可加 `--allow-archived-source`，报告保留原始提交、脏状态和
`source_matches_checkout=false`，不会改写旧清单，也不把旧包称为当前源码构建。

开发机可安全执行以下只读计划，不运行 EXE、不访问注册、凭据或系统安装位置：

```powershell
python -B scripts/acceptance/installed_release.py `
  --plan-only --allow-archived-source `
  --setup output/BaguTrainer-0.4.0-final/BaguTrainer-0.4.0-windows-x64-Setup.exe `
  --output-dir output/BaguTrainer-0.4.0-final `
  --previous-setup dist/BaguTrainer-0.3.0-windows-x64-Setup.exe `
  --previous-bundle dist/BaguTrainer `
  --work validation/installed-lifecycle-next/unexecuted-work `
  --output validation/installed-lifecycle-next/PLAN.json
```

退出码 0 在 `--plan-only` 模式中只代表静态输入校验成功，**不代表真实安装通过**；
不会创建计划中的工作目录。缺输入或绑定关系不符会失败，不扫描“最新包”或自动回退旧包。

## 在一次性 Windows runner 执行

现有 `.github/workflows/windows-installer-diagnostic.yml` 保留为仅诊断的手动入口，
选择 **Isolated installer diagnostics (no release)**，填写真实旧 Release 的 `previous_tag`。
流程构建当前 0.4.0 生产包，下载同一仓库该旧 Release 的 Setup、ZIP、校验文件和安装清单，
然后在空托管账户运行现有安装脚本。缺少旧附件会失败，不回退到合成夹具。
本次未推送代码或触发该工作流。

该 runner 内的命令等价于（变量由工作流设置）：

```powershell
.\.venv-build\Scripts\python.exe -B scripts/acceptance/installed_release.py `
  --setup "dist/BaguTrainer-$version-windows-x64-Setup.exe" `
  --output-dir dist `
  --previous-setup "build/previous-production/BaguTrainer-$previousVersion-windows-x64-Setup.exe" `
  --previous-bundle build/previous-production/BaguTrainer `
  --work "$env:RUNNER_TEMP/bagu-installed-release" `
  --output validation/INSTALLED_ACCEPTANCE.json
```

默认只执行下表六项生命周期验收。可额外指定 `--run-frozen` 和现有 `--driver`，
在已安装 EXE 上运行旧的完整冻结 UI 驱动；它使用自己的合成配置和内存凭据，
不替代六项中真实 WinCred 的核对。冻结失败仍继续记录可执行的卸载场景，并使总结果失败。
正式发布工作流保持该额外门禁，不能因生命周期通过而忽略冻结失败。
目前完整冻结 UI 已知报 `No acceptance milestones completed`，本任务没有将其改为通过。

## 六项场景和通过条件

| 报告案例 | 实际操作与核对 | 当前执行状态 |
| --- | --- | --- |
| `first_install` | 实际安装目标 Setup；核对安装注册、默认路径、逐文件摘要、开始菜单/桌面入口、未擅自启用自启动；启动真实 EXE，检查 0.4.0 版本及未配置工作台，保存截图；验证运行中拒绝升级/卸载 | 未执行 |
| `cross_version_upgrade` | 移除本次初装，安装真实旧 Setup；写入合成配置、题库版本、真实应用表结构的作答与学习状态及测试凭据，先启动旧 EXE 确认旧版能加载这些配置，再执行 0.4.0 Setup；核对配置/题库摘要、数据库逻辑内容、凭据、已安装载荷和恢复就绪状态 | 未执行 |
| `same_version_repair` | 执行同一个 0.4.0 Setup 修复，核对配置/题库/作答及学习状态、凭据保留、已有自启动入口修复，重启并核对就绪 | 未执行 |
| `cleanup_failure_retry` | 用真实 Win32 文件句柄禁止删除合成 `trainer.db`；主动卸载必须返回失败并保留程序、卸载注册及包含该文件失败原因的恢复日志；关闭句柄后复用原日志重试，不重置或重造数据 | 未执行 |
| `complete_uninstall` | 核对所有已记录受管文件、测试凭据、注册、对应快捷方式和恢复日志消失；无关文件、无关快捷方式及无关测试凭据保持不变 | 未执行 |
| `reinstall_pristine` | 先核对再退役本次放入程序目录的无关文件哨兵，避免非空目录阻止新安装；重装实际 Setup，检查未预设配置/题库/历史/Key、三个准备条件均为 false；最后卸载本次测试安装并退役剩余无关测试凭据/快捷方式 | 未执行 |

数据库按真实记录与调度状态的逻辑摘要比较，不因合法 SQLite 文件布局变化误判。
截图仅为现场证据，仍需人工检查，截图成功不代表视觉验收通过。
文件句柄故障针对数据删除，并非只检查应用运行锁；失败期间可能已经删除部分数据或测试凭据，
**阻止卸载不代表清理操作全部回滚**。不能对真实学习数据执行此测试。

## 报告、日志和失败处理

`INSTALLED_ACCEPTANCE.json` 使用 schema 2。每项先写 `running`，完成后写 `passed` 或 `failed`；
失败后依赖它的项目保持 `not_run` 并说明原因，不用空值、跳过或模拟结果计为通过。
报告保存目标与旧 Setup/ZIP/清单/源指纹摘要、测试脚本摘要、实际环境、案例结果和日志摘要。
工作目录保留每次 Inno 安装/卸载日志、运行时诊断 JSON、截图、应用日志副本和清理失败恢复日志副本。
工作流用 `if: always()` 保留这些证据；原始路径/SID 仅留诊断附件，公开报告沿用原白名单脱敏流程。

真实执行模式退出码 0 及六项均 `passed=true` 才算生命周期通过；若请求完整冻结验收，还必须该项通过。
发布门禁拒绝旧版“卸载保留数据”报告，以及缺案例、仍在运行、未执行或使用真实用户凭据的报告。
正式发布仍受已有源码、双产物、隐私及完整冻结门禁约束。

失败时保留此一次性环境及证据，不对其进行不明范围清理或自动重跑。先下载诊断附件，
根据失败日志修复后使用新的 runner、工作目录和报告；不把失败结果覆盖成通过。
在销毁 VM 前，若残留只属于本次测试，可使用保留的系统卸载入口或清理工具手动复验重试，
另存结果；不要在日常账户重放其中的路径、凭据操作或故障注入。
