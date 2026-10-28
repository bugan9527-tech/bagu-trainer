# Electron 迁移验证记录

日期：2026-10-28。本记录针对 v0.5.0 的 Electron + React + TypeScript + Vite 迁移，不继承历史 Tk 发布的验收结论。

## 已完成的验证

- TypeScript 严格检查与 Vite 生产构建通过。主界面脚本约 270 kB，Markdown/高亮约 331 kB，阅读模块按需加载。
- `npm test`：4 项通过，覆盖窗口来源、开发地址、外部链接协议与桌面能力白名单。
- Python 核心与安装包规则组：107 项通过，包含新桌面任务层、真实管道协议、已接收答案在 EOF/退出时落库、会话生命周期、并发生成、学习闭环、隐私与安装包校验。
- 在隔离数据及正确窗口权限下，Tk 兼容、启动、凭据迁移和来源清理组：80 项通过。沙箱内最初的 Tcl/Tk 错误不计为通过，沙箱外已重跑。
- 源码 Electron 和冻结后 EXE 均通过真实窗口验收：七页导航、配置表单草稿、输入时 Ctrl+R 不切换、只读待答列表、作答前隐藏答案、单答题窗口、关闭后恢复草稿、提交持久化、重复提交拒绝、历史详情与重新练习。
- 对 800、1440 和 375 像素视口进行页面水平溢出检查，并检查减少动态效果模式。截图已人工查看。
- 全新隔离数据目录能进入同一七页工作台，缺题库/模型/项目时保持可配置，不读取已有学习数据。
- 冻结 Python 的管道编码显式设置为 UTF-8，打包后的中文路径通过验收。

主要桌面证据目录：`validation/electron-1793120357885/`（迁移试构建）及 `validation/electron-1793157212607/`（v0.5.0 源码），其中 `RESULTS.json`、`FIRST_USE.json` 和截图使用自编合成题目及隔离用户数据，不含真实模型调用和个人代码。正式打包的结果另记入发布附件，不复用旧版二进制。

## 交付范围

本机程序目录：`output/electron/win-unpacked/`，入口 `BaguTrainer.exe`。同目录的 Python 引擎、完整卸载工具、`_internal` 和 `resources` 必须一同保留。

正式版本的便携 ZIP：`output/electron/BaguTrainer-0.5.0-electron-windows-x64.zip`；安装器：`output/electron/installer/BaguTrainer-0.5.0-windows-x64-Setup.exe`。发布时提供同名 SHA-256 文件；确切摘要和大小以发布附件为准。

安装器使用项目缓存中通过哈希锁定的 Inno Setup 6.7.3，沿用既有安装生命周期脚本。编译后使用 innounp 解包，1,178 个文件逐项大小和 SHA-256 全部一致；`INSTALLER_MANIFEST.json` 的 `application_executed` 为 false，`lifecycle_acceptance` 明确记为未运行。日志见 `validation/electron-installer.log`。

## 尚未覆盖

- 没有用个人模型账号执行付费连接测试或真实 AI 出题。
- 没有执行真实 Windows 安装、覆盖升级、登录后自启动或完整卸载；构建/解包审计不能替代这些生命周期验收。
- 没有测量与 Tk 版的内存、启动耗时对比。新桌面运行库增加程序体积，不能把界面迁移表述为性能指标已改善。
- 产物未签名。Tk 入口保留兼容回退；正式发布状态以 GitHub Release 为准。
