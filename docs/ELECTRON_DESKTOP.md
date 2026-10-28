# Electron 桌面版

桌面界面使用 Electron + React + TypeScript + Vite，Python 继续处理代码监听、知识点匹配、AI 出题和本地学习记录。

## 从源码运行

需要 Windows、Python 3.10+ 和 Node.js 22.12+。在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
npm ci
npx install-electron --no
npm start
```

`npm start` 构建正式网页资源并启动 Electron；开发界面可用 `npm run dev`，仅在本机 127.0.0.1 开启 Vite。正式程序不启动 Vite，不要求用户安装 Node.js。

默认使用 `.venv-build/Scripts/python.exe`（存在时）或 PATH 中的 `python`。可通过 `BAGU_PYTHON` 指向已安装运行依赖的解释器。Python 依赖请安装到实际使用的解释器。

七个页面和独立答题窗口共用一个 Python 引擎。关闭答题窗口只隐藏；草稿保留到本次应用退出。关闭主窗口会等待已接收的作答写入、取消可取消任务并安全退出。模型请求可能持续到已配置的超时结束。

## 便携版

完整解压 Electron ZIP，运行 `BaguTrainer.exe`。保留旁边的 `resources`、DLL 和 `BaguCleanup.exe`，不要只复制主程序。包内包含 Python 引擎，用户无需安装 Python 或 Node.js。

个人数据与旧版共用 `%LOCALAPPDATA%/BaguTrainer/`，不会从程序目录自动导入旧数据。启动 Electron 之前正常退出 Tk 旧版，避免同时占用同一份学习数据。

首次使用在题库管理、模型配置、监听项目中完成准备。模型连接测试由用户主动触发，可能产生服务商费用；启动检查不发送模型请求。

应用设置支持登录自启动、打开数据目录和完整卸载。完整卸载复用现有独立清理工具，需要两阶段确认；会清除共用数据与本机凭据。升级请退出旧程序并完整解压新版，不要先卸载。

## 构建与检查

```powershell
npm run build
npm test
python -m pytest tests/test_desktop_service.py tests/test_electron_transport.py -q -W error
npm run test:desktop
npm run package:dir
npm run package:zip
npm run package:installer
```

后端构建使用 `.venv-build`，其中需安装 `requirements-build.txt`；可用 `BAGU_BUILD_PYTHON` 指定其他构建解释器。构建输出在 `output/electron/`。`BUILD_MANIFEST.json` 列出受管程序文件，供完整卸载核对身份。当前不执行代码签名。

安装器沿用项目原有 Inno Setup 生命周期与解包校验，需锁定版本的 ISCC 和 innounp，见 `WINDOWS_INSTALLER.md`。`package:installer` 只构建和解包审计，不执行安装、升级或卸载；旧安装器的生命周期实测记录不能直接作为 Electron 版本的通过结论。

`test:desktop` 使用自编合成题目、隔离数据与隔离 Chromium 目录，验证真实 Python 引擎与 Electron 窗口，不调用真实模型、不读取个人 Key。证据保存在 `validation/electron-<时间戳>/`。可用 `BAGU_ELECTRON_EXECUTABLE` 指定打包后的 EXE，执行同一套流程。

现有 Tk 入口保留兼容。Electron Windows 工作流验证源码与冻结后 EXE，原 Tk 工作流仅保留兼容检查且不发布 v0.5.x。旧版发布验收不能作为 Electron 版的通过依据。生产配置升级、真实模型、登录后启动与完整卸载必须另行实测，不能从合成数据回归推断。
