# 0.3.1 本地集成与便携包验证

验证日期：2026-10-24，Windows x64 / CPython 3.10.11 / PyInstaller 6.22.3。

结论：已生成可供验证的 **0.3.1 Windows x64 便携包**。最终冻结验收及实际 EXE 检查通过；最终完整发布回归存在一次原生焦点失败，不能宣称全量全绿。未替换当前安装，未读取或清理真实用户题库与学习记录。

## 产物与来源

- 便携 ZIP：`dist/verification-0.3.1/BaguTrainer-0.3.1-windows-x64.zip`
- 校验文件：同目录下同名 `.zip.sha256`
- 可执行目录：`dist/verification-0.3.1/BaguTrainer/`，需保留 EXE 旁完整 `_internal/`
- 独立冻结验收驱动：`dist/verification-0.3.1/BaguTrainerAcceptance-windows-x64.zip`，不包含在生产便携包中
- ZIP 大小：19,694,158 字节
- ZIP SHA-256：`4f7c898d2fe62456b0ae002a9e6ebaddea725ffee820ecf3d50bbadeda41aa88`
- 来源提交：`3642869889ce9a94b695451a3c91ffd7c3f691d0`，`source_dirty=true`
- 源码指纹：`b50435ab2141489e1f406befcfa238bcdafee71521f2bcbbf4b9e3a3c4c78381`

包内 `BUILD_MANIFEST.json` 记录版本、依赖、1115 个载荷文件的大小和 SHA-256，以及包含未提交新增模块的源码指纹。最终生产包、冻结驱动及当前运行/构建源码指纹一致。此为未签名工作区验证包，未运行草稿 Release 上传流程，也未生成或试装 Setup。

`dist/integration-0.3.1/` 是修复短暂文件占用前的初次构建，**不作为本轮交付包**。初次冻结失败证据保留在 `validation/integration-031/frozen/ERROR.json`。

## 修改清单

1. 来源缓存按仓库标识与完整 commit 持久化；复用前校验完成记录、大小、SHA-256、ZIP 路径和 CRC。重建与强制下载分开，构建失败保留已校验缓存，旧来源快照不直接视为可复用缓存。
2. 批量任务分别记录每个来源状态、阶段、重试和错误原因；单项失败继续，支持仅重试失败来源；所选来源未全部准备完成时不激活部分题库。保留锁、取消与校验后原子切换。
3. 练习与历史详情支持只读拖选、键盘/右键复制和两个复制按钮；当前题答案与解析受作答状态控制，保留作答快捷键、换行和滚动。
4. 题库页区分本地缓存、当前任务和当前使用状态；后台统计来源缓存、当前构建、历史与临时文件，去重共享文件，未知远端大小明确显示未知。
5. 来源删除与历史清理分开确认；使用中的来源先重建、校验、切换运行引擎再清理；最后来源进入明确的无题库状态。保护路径、任务锁、共享文件、学习数据库和历史快照，逐对象报告失败并刷新。
6. 保留监听项目选择事件修复，补充项目文件确实存在且移除/保存前后内容不变的回归断言，验证未保存不应用、保存后调用应用回调。
7. 本轮新增两进程连续生命周期验收，复用于源码测试与独立冻结驱动；实际便携 EXE 升级测试新增 UUID 旧版本结构和可选旧版本二进制输入。
8. 集成验收发现 Windows 短暂未允许删除共享时，原子移动缓存目录会失败。新增真实 Windows 共享句柄回归；后台清理对 WinError 5/32/33 最多等待 6 秒重试。持续占用或权限错误仍保留文件并报告，不改权限、不强制关闭句柄。

## 测试结果分层

| 层次 | 最终结果 | 证据 |
| --- | --- | --- |
| 源码单元回归 | 164 通过，31 跳过 | `validation/integration-031/unit-final.xml` |
| 完整发布回归（包含下述 5 个实际便携测试） | 540 通过，1 失败，0 跳过 | `validation/integration-031/release-final.xml` |
| 上述回归中的实际便携 ZIP / EXE | 5 通过 | `validation/integration-031/release-final-modules/test_portable_bundle.xml` |
| 清理、空间、连续流程补充 | 30 通过；也包含于完整发布回归，不重复累计 | `validation/integration-031/cleanup-fixed.xml` |
| 实际旧 0.3.0 → 新 0.3.1 便携启动 | 2 通过（平铺、UUID 两种旧题库） | `validation/integration-031/cross-version.xml` |
| 独立冻结驱动与生产 EXE | `passed=true`、`frozen=true` | `validation/integration-031/frozen-final/ACCEPTANCE.json` |
| 源码与包绑定、ZIP 摘要 | 一致 | `validation/integration-031/artifacts-final.json` |
| 差异格式检查 | `git diff --check` 通过 | 仅现有 LF/CRLF 提示，无差异错误 |

完整回归的 1 个失败是 `test_sidebar_keyboard_and_real_pages_at_four_tk_scales[1.5]`：发送 Home 后 `focus_get()` 为 `None`，预期为工作台侧栏按钮。原报告未覆盖、未删断言、未改成跳过。原用例在三个独立进程中诊断复跑均通过，分别见 `focus-repro-1.xml`、`focus-repro-2.xml`、`focus-repro-3.xml`。尚不能确认失焦根因，因此保留为未解决的原生焦点验证限制，**不把复跑计入全量通过**。

31 个跳过项需要 `BAGU_TEST_BANK_ROOT` 指定外部只读历史题库基准；本轮未提供该基准，也未为补齐这些测试下载真实完整题库。

## 连续流程和数据保留证据

`scripts/acceptance/bank_lifecycle.py` 在源码与冻结驱动中运行同一条连续流程：

1. 首次只下载一个来源；全选时只请求其余缺失来源。
2. 强制刷新时中间项模拟 HTTP 404；继续处理后项，当前题库指针保持原值。
3. 显式仅重试失败的强制刷新；请求列表只含失败来源。普通重试允许直接复用仍有效的旧缓存。
4. 同完整 commit 再构建零请求；结束进程。
5. 第二个进程读取原 UUID 题库、配置、真实 SQLite 学习记录和中文多行历史快照；重建仍零请求。
6. 真实 Tk 练习页复制题目与选项；提交前复制解析被阻止，提交后中文多行解析一致。
7. 删除使用中来源，保留来源由本地缓存重建并安全切换，之后移除旧缓存和旧当前版本。
8. 单独清理历史版本，当前题库、学习数据库、快照和项目文件保持可读，历史空间统计归零。

最终冻结报告中的 `bank_prepare` 与 `bank_resume` 分别记录 5 个里程碑、不同进程 ID，且均为 `frozen=true`。本轮完整发布回归另覆盖首项/中间项失败、单请求与总超时、取消、构建失败重试、并发锁、损坏缓存、commit 变化、权限错误、共享缓存、最后来源和无题库状态，以及复制快捷键与历史详情。

原首次使用冻结流程还实际创建向导、主窗口和历史页，经 localhost HTTP 完成下载、生成、审核、作答、历史重练与重启；新增历史复制按钮的剪贴板断言。所有模型响应为固定模拟值，凭据使用内存夹具。

生产 `BaguTrainer.exe` 的实测范围是 ZIP 逐文件/CRC/摘要、中文空格路径、不可写程序目录、无外部工具 PATH、原生临时快捷方式、单实例、启动/重启、运行库与主题图标检查、迁移和保留学习记录。跨版本测试复制仓库保留的 `dist/BaguTrainer` 0.3.0 验证包与新包到临时目录，并先核对旧包清单和摘要。

**完整业务链由源码测试和独立冻结驱动执行。没有为生产 EXE 增加测试后门，也没有把所有模拟业务步骤描述成对生产 EXE 的人工点击验证。**跨版本测试使用旧结构合成数据与旧便携二进制，未操作用户现有真实数据或真实安装注册。

## 复验入口

构建沿用项目现有流程，在独立目录输出：

```powershell
$env:BAGU_USER_DATA_DIR = "$PWD/output/integration-031-build-user"
.\.venv-build\Scripts\python.exe scripts/build_portable.py --output-dir dist/verification-0.3.1
.\.venv-build\Scripts\python.exe scripts/build_acceptance.py --output-dir dist/verification-0.3.1
```

完整发布测试使用 `scripts/run_release_tests.py` 的逐模块新进程模式，环境变量 `BAGU_PORTABLE_BUNDLE`、`BAGU_PORTABLE_ARCHIVE` 指向本轮最终目录。跨版本补充测试另设 `BAGU_PREVIOUS_BUNDLE` 指向旧 0.3.0 包，然后运行 `release_tests/test_portable_bundle.py::test_exe_migration_and_upgrade_keep_learning_history`。所有清理测试只操作临时或专门生成且带归属标记的隔离目录。

手动验证时，完整解压 ZIP，在解压后的 `BaguTrainer` 目录打开 PowerShell，用新建隔离数据目录启动：

```powershell
$env:BAGU_USER_DATA_DIR = Join-Path $env:TEMP ("BaguTrainer-0.3.1-manual-" + [guid]::NewGuid().ToString("N"))
.\BaguTrainer.exe
```

直接双击默认会读取当前 Windows 用户已有的数据目录，并可能唤起当前运行的旧实例；验证新版时应使用上述隔离方式。本轮没有自动替用户执行安装替换，也不表示用户当前已安装的 EXE 已更新。

## 剩余限制

- 完整回归有上述一次未能稳定复现的原生焦点失败，尚不满足全部全绿的正式发布门禁。
- 外部历史题库基准的 31 项未执行；真实完整题库下载、真实模型服务及真实凭据写入未测。
- 未生成/试装新版 Setup，未验证安装注册覆盖、Windows 11/干净虚拟机、安全软件组合、代码签名或系统重启登录。
- 高 DPI 回归包含 Tk 模拟缩放，不代表所有物理显示器/DPI 组合均已人工验收；截图只作证据，不自动视为视觉验收通过。
- 空间为去重后的文件逻辑字节，不等于文件系统簇占用；本轮所有删除均为隔离数据，未自动清理真实数据。
