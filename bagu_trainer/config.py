# -*- coding: utf-8 -*-
"""配置加载：LOCALAPPDATA/BaguTrainer 下 config.json（可选）。
不存在时用默认值；LLM 未配置时合成出题不可用（仅题库已验证选择题可出），
其余知识点返回明确 unavailable（不静默、不出空题）。

隐私约束：llm.api_key 不再明文写入 config.json——新保存的 Key 进
Windows 凭据管理器（keyring），旧配置明文仅作兼容读取并提示迁移。
"""
import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .paths import RESOURCE_ROOT, USER_ROOT

PROJECT_ROOT = RESOURCE_ROOT  # Read-only resources; retained for callers using this name.
USER_DATA_ROOT = USER_ROOT
CONFIG_PATH = USER_DATA_ROOT / "config.json"
_CONFIG_LOCK = threading.RLock()


@contextmanager
def config_transaction():
    """Serialize in-process edits, from the latest strict read through commit.

    All live configuration editors share this lock. Keep credential/network
    operations outside it; atomic replacement still protects concurrent readers.
    """
    with _CONFIG_LOCK:
        yield


def data_dir():
    return Path(USER_DATA_ROOT) / "data"


def db_path(value=""):
    path = Path(value).expanduser() if value else Path("data") / "trainer.db"
    return path if path.is_absolute() else Path(USER_DATA_ROOT) / path

DEFAULTS = {
    "trae_home": str(Path.home() / ".trae-cn"),
    "project_roots": [],
    "max_quizzes_per_hit": 1,
    "session_recent_minutes": 180,
    "session_ingest": False,         # 会话摄取默认关闭：无法可靠判断项目归属时绝不读会话
    "context_char_limit": 4000,      # 进入 matcher/LLM 前的上下文总字符上限
    "llm": None,  # {"base_url": str, "api_key": str(旧配置兼容位，新保存恒为空), "model": str, "timeout_sec": int}
    "quiz_ttl_seconds": 3600,      # 待答题有效期（秒），超时确定性清理
    "max_pending_quizzes": 20,     # pending 队列容量上限，超量淘汰最旧
    "max_quizzes": 200,            # 内存保留 quiz 总数上限，超量淘汰最旧
    "seen_hash_capacity": 1000,    # 去重指纹容量上限，超量淘汰最旧指纹
    "db_path": "",                 # 空=用户目录/data/trainer.db；相对路径按用户数据目录解析
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in user.items() if v})
        except Exception as e:
            print(f"[config] config.json 解析失败，使用默认配置: {e}", file=sys.stderr)
    return cfg


def read_user_config() -> dict:
    """Strict read for editors: keep empty/false fields and reject broken files."""
    path = Path(CONFIG_PATH)
    if not path.exists():
        return {}
    user = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(user, dict):
        raise ValueError("config.json 必须是 JSON 对象")
    return user


def _write_config_atomic(cfg: dict) -> None:
    """在同目录写临时文件并原子替换，避免配置只写了一半。"""
    payload = json.dumps(cfg, ensure_ascii=False, indent=2)
    target = Path(CONFIG_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def update_llm(llm: dict, *, clear_api_key: bool = False) -> str:
    """合并写 config.json 的 llm 段（LLM 配置热更新入口，其余配置保留）。

    隐私改造：api_key 经 secrets.save_api_key 写入系统凭据管理器，
    config.json 仅存 api_key:""。空 api_key 不再隐式删除凭据；需要清空时
    必须显式传入 clear_api_key=True。返回存储位置标签供调用方转述。
    """
    from . import secrets  # 延迟导入避免循环依赖
    key = (llm.get("api_key") or "").strip()
    if key and clear_api_key:
        raise ValueError("api_key 与 clear_api_key=True 不能同时使用")
    # Saving a model must not invent watched projects or replace an explicit
    # empty/false setting with runtime defaults, especially during first run.
    read_user_config()  # Reject broken files before touching credentials.
    if key:
        stored = secrets.save_api_key(key)
        if stored == "fallback":
            # 凭据管理器不可用：绝不回退明文写盘，config.json 该字段置空，
            # 由调用方提示用户改用环境变量 BAGU_LLM_API_KEY
            print("[config] 警告：凭据管理器不可用，新 Key 未保存。"
                  "请改用环境变量 BAGU_LLM_API_KEY。", file=sys.stderr)
    elif clear_api_key:
        secrets.clear_api_key()
        stored = "cleared"
    else:
        stored = "unchanged"
    with config_transaction():
        cfg = read_user_config()
        cfg["llm"] = {**llm, "api_key": ""}
        _write_config_atomic(cfg)
    return stored
