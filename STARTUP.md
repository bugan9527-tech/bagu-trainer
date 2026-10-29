# 单一正式主窗口启动（0.4.x Tk 兼容版，归档）

> 版本范围：本文仅适用于 0.4.x Tk 兼容版，保留旧版 Tk 启动协调、准备检查和单实例说明。0.5.0 Electron 当前桌面说明见[项目仓库 docs/ELECTRON_DESKTOP.md](https://github.com/bugan9527-tech/bagu-trainer/blob/main/docs/ELECTRON_DESKTOP.md)；本文的 `scripts/start_desktop.py`、`--configure`、`--migrate-from` 和 Tk 窗口行为不代表当前 Electron。

安装版从开始菜单或安装目录的 `BaguTrainer.exe` 启动；便携版在完整解压目录中运行 `BaguTrainer.exe`；
源码版先安装 `requirements.txt`，再在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -B scripts/start_desktop.py
```

三种方式均立即进入唯一正式工作台，显示完整七项侧栏。安装版和便携版默认程序目录不同，
个人数据默认同为 `%LOCALAPPDATA%\BaguTrainer\`，源码版也使用该目录。
具体准备步骤见 [首次使用与修复](FIRST_USE.md)。

启动先获取当前用户桌面会话的全局单实例身份，立即创建唯一的 `DesktopApp` / `AppShell` 和 Tk 根。后台执行本地就绪检查及删除事务恢复，再按条件延迟加载真实 engine。主窗口构造不依赖 engine。缺项、配置损坏或引擎失败均留在原工作台，并可导航到题库、模型、项目页修复。

`StartupCoordinator.prepare()` 保留本地只读检查和引擎延迟加载边界；`run()` 不再创建 `InitializationApp`。模型保存、项目保存和题库构建后排队重新检查。工作台提供显式重试。启动不下载题库，也不自动测试模型。

对应页面操作是“题库管理 → 构建 / 重新构建”“模型配置 → 测试连接 → 保存”及
“监听项目 → 添加项目文件夹 → 保存并应用”。缺项时按提示修复，在工作台“重新检查 / 加载引擎”重试，
不需要离开主窗口。配置 JSON 损坏或权限错误需先备份并修复原文件，不会用默认配置覆盖。
删除最后一个题库来源后仍在原窗口，重新构建来源后恢复监听与练习；清理失败使用题库页“重试未完成清理”。

能力控制：题库、模型及项目就绪后启用监听和练习；历史通过 `LearningHistory` / Store 的只读连接独立访问。不存在假引擎。正式单实例与 EXE 目录和数据 profile 无关；重复启动通过受限管道唤起现有窗口。不同 profile 不会静默切换，状态中包含当前数据目录。隔离测试需设置 `BAGU_TEST_INSTANCE`。

管道只允许 `_show_dashboard`、`_desktop_status`、`_navigate`。导航参数必须在固定七页白名单中。`--configure` 和旧配置脚本导航到当前窗口的模型页。维护互斥期间拒绝新实例，卸载不通过进程名强杀其他副本。

`BaguTrainer.exe --configure` 与源码
`.\.venv\Scripts\python.exe -B scripts/config_window.py` 都导航到“模型配置”，不产生独立配置窗口。
`--background` 只把同一主窗口最小化；有缺项时可从任务栏恢复后修复，不会打开向导。
普通重复启动唤起已有实例；已有实例运行时 `--migrate-from` 不会在其后静默执行，迁移前需退出新旧程序。
迁移仅通过启动入口的 `--migrate-from "旧项目目录"` 显式复制，拒绝覆盖已有目标，详见 [便携迁移步骤](PORTABLE_README.md#从旧版迁移)。

Tk 控件、变量、导航和错误展示都在 UI 主线程执行。下载、引擎、历史和清理任务只接收普通数据，通过队列回传。页面缓存和 TaskController 维持任务生命周期。关闭取消下载及模型测试，等待已开始的提交、模型保存、历史读句柄和清理任务安全收尾。

删除事务在引擎串行维护边界和构建锁内执行；启动发现 `data/source-deletion.json` 时先恢复，再开放练习。内容修订保护迟到的手动出题；历史查询 token 和页面缓存失效保护旧快照。失败详情和删除确认在原窗口内展示；系统目录选择器和系统安装/卸载工具为独立维护流程。

关闭主窗口是安全退出，不是卸载。升级/修复保留用户数据；应用设置的“卸载应用并清除全部数据…”
是另一项显式维护操作，会清除受管数据和本机凭据，交接流程及失败重试见 [安装与卸载](WINDOWS_INSTALLER.md)。

验证使用 `scripts/run_release_tests.py --output-dir <本次构建输出> --output <新报告路径>`，
每个 release 测试模块在独立进程执行。构建与验收共用同一个输出目录，旧版本、源码指纹或附件不符先失败。
受限沙箱无法初始化本机 Tcl 的情况须与业务失败分开记录，不通过跳过或修改 Tcl 判定绕过。
本文描述 0.4.0 实现，不代表完整冻结验收已通过；当前结果及历史版本边界见 [验证状态](README.md#发布与验证状态)。
