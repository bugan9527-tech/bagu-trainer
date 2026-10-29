# -*- coding: utf-8 -*-
"""密钥与脱敏模块（隐私最小化改造核心）。

密钥解析优先级（不向项目目录写明文）：
  1. 环境变量 BAGU_LLM_API_KEY（最高优先，便于部署与轮换）
  2. Windows 凭据管理器（keyring，keyring 包不可用时自动跳过）
  3. 旧 config.json 的 llm.api_key（仅兼容读取并打印一次性迁移提示；
     显式 migrate 在 keyring 回读校验成功后才会清空该字段）

统一脱敏 redact_text：在任何上下文进入 matcher / LLM 之前调用，
替换：当前已解析 Key、sk-* 前缀令牌、Bearer token、api_key=/token=/secret=
等赋值式明文、PEM 私钥块。

敏感文件名判定 is_sensitive_path：config.json、.env*、*.key/*.pem/*.p12 等
私钥/凭据文件一律不得进入文件监听上下文。

可用小命令（不打印任何密钥值）：
  python -m bagu_trainer.infrastructure.secrets check    查看当前 Key 来源（env/keyring/文件/无）
  python -m bagu_trainer.infrastructure.secrets migrate  把旧 config.json 明文 Key 迁入凭据管理器
                                          （成功后才清空文件内字段）
"""
import os
import hashlib
import re
import sys
from collections import deque
from pathlib import Path

REDACTED = "[REDACTED]"

ENV_KEY = "BAGU_LLM_API_KEY"
def credential_service():
    # An explicitly isolated data profile must not read/overwrite the main user's
    # credential. Default installations keep their existing Windows vault entry.
    profile = os.environ.get("BAGU_USER_DATA_DIR")
    if not profile:
        return "bagu-trainer"
    normalized = os.path.normcase(str(Path(profile).expanduser().resolve()))
    return "bagu-trainer.profile." + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


KEYRING_SERVICE = credential_service()
KEYRING_USER = "llm_api_key"
_KNOWN_KEYS = deque(maxlen=16)


def known_keys():
    return tuple(_KNOWN_KEYS)


def _remember(key):
    if key and key not in _KNOWN_KEYS:
        _KNOWN_KEYS.append(key)
    return key

SENSITIVE_EXACT = {".env", "config.json"}
SENSITIVE_EXT = {".key", ".pem", ".p12", ".pfx", ".ppk", ".keystore", ".jks"}
SENSITIVE_PARTS = (
    "credential", "token", "secret", "password", "passwd",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "private", "api_key", "apikey", "access_key",
)

# ---- 跳过一次-多次的迁移提示 ----
_HINTED = {"legacy": False, "fallback": False}

_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}\b")
_ASSIGN_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|access[_-]?key|secret|token|password|passwd)"
    r"(\s*['\"]?\s*(?:=|:)\s*['\"]?)[^'\",;&\s]{6,}")
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")


def _warn_once(tag: str, msg: str) -> None:
    if not _HINTED.get(tag):
        _HINTED[tag] = True
        print(f"[secrets] {msg}", file=sys.stderr)


def _load_keyring():
    """加载 keyring；未安装/后端异常时返回 None（不阻断主流程）"""
    try:
        import keyring
        return keyring
    except Exception:
        return None


def _resolve_api_key_with_source(llm_cfg: dict = None):
    """内部返回 Key 与固定来源类型，沿用 env > keyring > 旧配置优先级。

    旧配置文件兜底仅作兼容读取，并打印一次性迁移提示；不删除旧 Key。
    """
    llm = llm_cfg or {}
    env_key = os.environ.get(ENV_KEY, "").strip()
    if env_key:
        return _remember(env_key), "env"
    if not llm:
        from bagu_trainer.infrastructure.config import load_config  # 延迟导入避免循环依赖
        llm = load_config().get("llm") or {}
    keyring = _load_keyring()
    if keyring is not None:
        try:
            stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
            if stored.strip():
                return _remember(stored.strip()), "keyring"
        except Exception:
            pass  # 凭据库故障时继续走旧配置兜底
    legacy = (llm.get("api_key") or "").strip()
    if legacy:
        _warn_once("legacy",
                   "检测到 config.json 中的明文 api_key（旧配置）。"
                   "建议迁移：设置环境变量 BAGU_LLM_API_KEY，或运行 "
                   "`python -m bagu_trainer.infrastructure.secrets migrate` 迁入凭据管理器；"
                   "迁移写入并回读校验成功后会安全清空 config.json 的该字段。")
        return _remember(legacy), "legacy"
    return "", "none"


def resolve_api_key(llm_cfg: dict = None) -> str:
    """Resolve using the existing env > Windows vault > legacy priority."""
    return _resolve_api_key_with_source(llm_cfg)[0]


def api_key_source(llm_cfg: dict = None) -> str:
    """Return only a fixed source category; never return the credential to UI."""
    return _resolve_api_key_with_source(llm_cfg)[1]


def save_api_key(key: str) -> str:
    """把新 Key 写入凭据管理器（不写项目目录）。返回存储位置标签：
    "keyring" / "cleared" / "fallback"（keyring 不可用，由调用方决定兜底策略）。
    """
    from bagu_trainer.infrastructure.app_cleanup import maintenance_active
    if maintenance_active():
        raise RuntimeError("应用正在完整卸载，禁止保存凭据。")
    key = (key or "").strip()
    _remember(key)
    keyring = _load_keyring()
    if not key:
        if keyring is not None:
            try:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except Exception:
                pass
        return "cleared"
    if keyring is None:
        return "fallback"
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
        return "keyring"
    except Exception as e:
        print(f"[secrets] 凭据管理器写入失败({e.__class__.__name__})，"
              f"请改用环境变量 {ENV_KEY}", file=sys.stderr)
        return "fallback"


def clear_api_key() -> None:
    """清空凭据管理器中的 Key（保留 config.json 其他字段）"""
    keyring = _load_keyring()
    if keyring is not None:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass


def remove_credential(service, backend=None):
    """Uninstall-only strict deletion: absence and backend failure are distinct."""
    if not (service == "bagu-trainer" or re.fullmatch(r"bagu-trainer\.profile\.[0-9a-f]{24}", service)):
        raise ValueError("非本应用凭据命名空间")
    if backend is None:
        if os.name == "nt":
            # Uninstall must inspect the real Windows vault even if normal
            # runtime resolution was configured to use a null/fallback backend.
            from keyring.backends.Windows import WinVaultKeyring
            backend = WinVaultKeyring()
        else:
            backend = _load_keyring()
    if backend is None:
        raise RuntimeError("Windows 凭据后端不可用，无法核验清理")
    if backend.get_password(service, KEYRING_USER) is None:
        return "absent"
    backend.delete_password(service, KEYRING_USER)
    if backend.get_password(service, KEYRING_USER) is not None:
        raise RuntimeError("凭据删除后仍存在")
    return "deleted"


# ---- 统一脱敏 ----
def redact_text(text: str, extra_secrets=None) -> str:
    """替换文本中的秘密为 [REDACTED]。

    extra_secrets: 额外需要整值替换的秘密列表（如当前已解析 API Key）。
    顺序：先替换整值秘密（避免被后续正则部分命中留下残片），再替换
    正则模式（sk-、Bearer、赋值式、PEM 私钥块）。
    """
    if not text:
        return text
    for s in (extra_secrets or []):
        s = (s or "").strip()
        if len(s) >= 5 and s in text:
            text = text.replace(s, REDACTED)
    text = _PEM_RE.sub(REDACTED, text)
    text = _SK_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub("Bearer " + REDACTED, text)
    text = _ASSIGN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return text


# ---- 敏感文件名判定 ----
def is_sensitive_path(rel_path) -> bool:
    """判断相对路径是否为凭据/私钥类文件，禁止进入监听上下文。

    覆盖：config.json（精确名）、.env*、私钥扩展名 (.key/.pem/.p12/.pfx/
    .ppk/.keystore/.jks)、文件名含 credential/token/secret/password/
    id_rsa/private/api_key 等片段（大小写不敏感）。
    """
    name = Path(str(rel_path)).name
    low = name.lower()
    if low in SENSITIVE_EXACT:
        return True
    if low.startswith(".env"):
        return True
    if Path(low).suffix in SENSITIVE_EXT:
        return True
    return any(p in low for p in SENSITIVE_PARTS)


# ---- 小命令 ----
def _main(argv: list) -> int:
    cmd = argv[0] if argv else "check"
    from bagu_trainer.infrastructure import config as config_mod
    if cmd == "check":
        if os.environ.get(ENV_KEY, "").strip():
            print(f"Key 来源: 环境变量 {ENV_KEY}")
        elif _keyring_has():
            print("Key 来源: 凭据管理器(keyring)")
        elif (config_mod.load_config().get("llm") or {}).get("api_key", "").strip():
            print("Key 来源: config.json 明文(旧配置)")
        else:
            print("Key 来源: 无（未配置）")
        return 0
    if cmd == "migrate":
        cfg = config_mod.load_config()
        llm = cfg.get("llm") or {}
        legacy = (llm.get("api_key") or "").strip()
        if not legacy:
            if _keyring_has():
                print("迁移状态：已完成，无明文 Key；凭据管理器中的 Key 保持不变。")
            else:
                print("config.json 中无明文 api_key，凭据管理器中也没有 Key，无需迁移。"
                      f"如需配置，请设置环境变量 {ENV_KEY}。")
            return 0
        if _load_keyring() is None:
            print("迁移失败：keyring 不可用，config.json 未修改。"
                  f"请改用环境变量 {ENV_KEY}，确认可用后再手动清空 config.json 的 api_key 字段。",
                  file=sys.stderr)
            return 1
        stored = save_api_key(legacy)
        if stored != "keyring" or not _keyring_matches(legacy):
            print("迁移失败：无法确认 Key 已写入凭据管理器，config.json 未修改。"
                  f"请改用环境变量 {ENV_KEY}，确认可用后再手动清空 config.json 的 api_key 字段。",
                  file=sys.stderr)
            return 1
        try:
            # 显式只更新配置字段；不能再用空字符串隐式删除刚写入的凭据。
            config_mod.update_llm({**llm, "api_key": ""}, clear_api_key=False)
        except Exception as e:
            print(f"迁移失败：配置写入异常({e.__class__.__name__})，config.json 未修改。"
                  f"Key 已保存在凭据管理器，可安全重试；或改用环境变量 {ENV_KEY}。",
                  file=sys.stderr)
            return 1
        remaining = ((config_mod.load_config().get("llm") or {})
                     .get("api_key") or "").strip()
        if remaining:
            print("迁移失败：配置校验发现明文 Key 仍存在。"
                  f"Key 已保存在凭据管理器，可安全重试；或改用环境变量 {ENV_KEY}。",
                  file=sys.stderr)
            return 1
        if not _keyring_matches(legacy):
            print("迁移失败：清空配置后无法从凭据管理器回读 Key。"
                  f"请改用环境变量 {ENV_KEY}。", file=sys.stderr)
            return 1
        print("迁移完成：Key 已写入凭据管理器，config.json 的 api_key 字段已置空。")
        return 0
    print(f"未知命令 {cmd}；可用: check / migrate", file=sys.stderr)
    return 2


def _keyring_has() -> bool:
    return bool(_keyring_value())


def _keyring_value() -> str:
    keyring = _load_keyring()
    if keyring is None:
        return ""
    try:
        return (keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or "").strip()
    except Exception:
        return ""


def _keyring_matches(expected: str) -> bool:
    return _keyring_value() == (expected or "").strip()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
