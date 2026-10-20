# AI 八股训练器 · Windows x64 便携版

将整个压缩包解压到任意文件夹，双击 `BaguTrainer.exe`。
无需另装 Python、Git 或 PowerShell；请保留 EXE 旁的 `_internal` 文件夹。
首次使用按中文向导完成题库下载、模型连接测试和监听项目选择。

程序文件与用户数据分开保存。配置、题库、学习数据库和日志位于：

`%LOCALAPPDATA%\BaguTrainer\`

- `config.json`：本机配置；API Key 存入现有 Windows 凭据管理器，不写入明文配置。
- `data\`：题库、版本、向导进度及 `trainer.db` 学习数据库。
- `logs\desktop.log`：滚动日志。
- `runtime\`：当前用户的单实例锁与窗口通信凭据。

升级时退出程序，用新版本的完整目录替换旧程序目录，或解压到另一个新目录。
保持上述用户数据目录即可继续使用原学习记录；不要用压缩包覆盖或删除该目录。
重复启动会唤起已运行的实例，程序目录可设为只读。

## 从旧版迁移

首次向导点击“迁移旧数据…”，选择含 `config.json` 或 `data` 的旧项目目录。
请先关闭旧版。程序复制、校验题库，以 SQLite 备份接口快照学习数据库，旧目录保留。
旧明文 Key 必须写入凭据管理器并回读确认后才启用新配置。
当前用户目录已有配置或题库时拒绝覆盖；不要删除唯一一份学习记录来绕过此检查。

也可在终端运行：

```powershell
.\BaguTrainer.exe --migrate-from "D:\旧目录\bagu trainer"
```

## 登录自启动和配置

主窗口“登录自启动”菜单可启用或取消；快捷方式指向当前 EXE，并带 `--background`。
移动程序目录后重新启用自启动即可更新路径。也可运行：

```powershell
.\BaguTrainer.exe --install-startup
.\BaguTrainer.exe --uninstall-startup
.\BaguTrainer.exe --configure
```

单独配置窗口和源码运行方式继续支持。源码启动命令仍为 `python -B scripts/start_desktop.py`，
与便携版共用用户数据目录，不再自动读取源码旁的旧配置或题库；旧数据使用上述方式显式迁移。
高级隔离测试可设置绝对路径环境变量 `BAGU_USER_DATA_DIR`，默认不需要设置。
显式隔离目录也使用独立 Windows 凭据项，避免测试或多个配置互相覆盖 Key；
默认用户目录继续使用原来的 `bagu-trainer/llm_api_key` 凭据项。

发布包只含必要程序、运行库、许可文件和来源元信息，不附带第三方完整题库、Key 或个人记录。
首次下载仍需网络，模型接口由用户选择。程序没有自动更新或上传个人数据的功能。
