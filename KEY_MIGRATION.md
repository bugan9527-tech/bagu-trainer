# API Key 轮换与迁移说明（bagu-trainer）

> 适用版本：接入隐私最小化改造后的 bagu-trainer。
> 原则：**api_key 不落项目目录**。新保存的 Key 只进 Windows 凭据管理器（keyring），
> 旧字段仅作兼容读取并提示迁移；显式执行迁移且 keyring 回读校验成功后，命令会
> 用原子替换方式清空 `config.json` 中的明文 Key。

## Key 来源优先级

1. 环境变量 `BAGU_LLM_API_KEY`（最高，推荐用于部署）
2. Windows 凭据管理器（keyring，条目 service=`bagu-trainer`，user=`llm_api_key`）
3. `config.json` 的 `llm.api_key`（旧配置兼容位，读取时打印一次性迁移提示）

查看当前来源（不打印 Key）：

```powershell
python -m bagu_trainer.secrets check
```

## 从旧 config.json 明文 Key 迁移

安装版、便携版和 Electron 源码运行当前默认配置为 `%LOCALAPPDATA%\BaguTrainer\config.json`。
0.5.0 Electron **没有**“迁移旧数据”按钮或 `--migrate-from` 命令；旧配置在项目旁时，先退出所有副本，
备份 `%LOCALAPPDATA%\BaguTrainer` 后手工复制配置，并在“模型配置”重新保存 API Key（Windows 凭据不随目录复制）。
0.4.x Tk 兼容版仍保留首次向导“迁移旧数据…”和 `--migrate-from "旧项目目录"`（历史说明见 [FIRST_USE.md](FIRST_USE.md)）。
下方模块命令只适用于源码 Python 环境；安装版/便携版请在“模型配置”页重新测试并保存 Key，不提供该命令行入口。

```powershell
python -m bagu_trainer.secrets migrate
```

- Key 写入凭据管理器，`config.json` 的 `api_key` 字段置空；
- 执行顺序是“写入 keyring → 回读校验 → 原子清空配置字段”，清空配置不会删除
  刚保存的凭据；
- 若 keyring 不可用或写入失败：命令返回非零退出码，`config.json` 保持原样。
  请改用环境变量 `BAGU_LLM_API_KEY`，确认可用后再手动将
  `config.json` 的 `api_key` 改为 `""`；
- 命令幂等：成功后重复执行不会丢 Key，只会报告已完成状态；
- 命令和错误信息均不会打印 Key 或其片段。

配置写入使用同目录临时文件，完整写入并 `fsync` 后用 `os.replace` 原子替换；
中途失败不会留下只写了一半的 `config.json`。

## Key 轮换（换新 Key）

1. 在服务商控制台创建新 Key，并吊销旧 Key；
2. 在主窗口点击「配置出题模型」，输入新 Key 并保存到凭据管理器，
   config.json 不保存明文；
3. 或直接设置/更新环境变量 `BAGU_LLM_API_KEY` 后重启桌面程序；
4. 若曾存在旧明文：确认 `config.json` 的 `llm.api_key` 为 `""`。

## 隐私边界（相关配置）

- `project_roots`：只有白名单内的目录可作为 `project_root` 被采集；白名单外
  与路径穿越一律拒绝。
- 文件监听：仅扩展名白名单文件内容可进入出题上下文；`config.json`、`.env*`、
  `*.key`、`*.pem`、`*.p12`、`id_rsa`、含 `credential/token/secret/password`
  等文件名的文件永不进入监听上下文。
- 上下文脱敏：进入匹配器/LLM 前，当前 Key、`sk-*`、`Bearer token`、
  `api_key=*` 等统一替换为 `[REDACTED]`。
- 会话摄取：默认关闭（`session_ingest: false`）。显式开启时也只读取与当前项目
  路径唯一匹配的会话目录，无法可靠判断归属即不读，绝不跨项目读取。
- `context_char_limit`：进入 matcher/LLM 的上下文总字符上限（默认 4000；
  发送给 LLM 的片段另有 2000 字符上限）。
