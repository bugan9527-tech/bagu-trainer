"""UI-independent model verification and resumable first-use state.

Only public progress and a keyed verification receipt are persisted here. API
keys remain in memory and in the existing system credential store.
"""
import hashlib
import hmac
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import config, secrets
from .bank_storage import atomic_json


class SetupError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_sec: float = 15

    def public(self):
        return {"base_url": self.base_url, "model": self.model,
                "timeout_sec": self.timeout_sec, "api_key": ""}


@dataclass(frozen=True)
class VerifiedModel:
    settings: ModelSettings
    receipt: str = field(repr=False)


def state_path():
    return config.data_dir() / "onboarding.json"


def read_state():
    path = state_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("invalid state")
        return value
    except (OSError, ValueError) as error:
        raise SetupError("state", "无法读取向导进度，请修复 data/onboarding.json 的格式或文件权限后重试。") from error


def begin():
    state = read_state()
    if not state:
        try:
            atomic_json(state_path(), {"schema_version": 1})
        except OSError as error:
            raise SetupError("state", "无法保存向导进度，请检查 data 目录的写入权限后重试。") from error


def validate_settings(base_url, model, api_key, timeout_sec=15):
    try:
        base_url, model, api_key = base_url.strip().rstrip("/"), model.strip(), api_key.strip()
        address = urlsplit(base_url)
        port = address.port
        if (address.scheme not in {"http", "https"} or not address.hostname
                or address.username or address.password or address.query or address.fragment
                or any(c.isspace() for c in base_url) or port == 0):
            raise ValueError("invalid URL")
    except (ValueError, AttributeError, TypeError):
        raise SetupError("address", "请输入有效的模型基础地址（http:// 或 https://），不要包含账号、Key、查询参数或 /chat/completions。") from None
    if base_url.endswith("/chat/completions"):
        raise SetupError("address", "基础地址请去掉末尾 /chat/completions，通常填写服务商提供的 /v1 地址。")
    if not model:
        raise SetupError("model", "请填写服务商提供的模型名称。")
    if not api_key or any(c in api_key for c in "\r\n"):
        raise SetupError("key", "请填写有效的 API Key；已有已保存 Key 时可留空沿用。")
    if (not isinstance(timeout_sec, (int, float)) or isinstance(timeout_sec, bool)
            or not math.isfinite(timeout_sec) or not 0 < timeout_sec <= 120):
        raise SetupError("timeout", "连接超时须为 0 到 120 秒之间的正数。")
    return ModelSettings(base_url, model, api_key, timeout_sec)


def effective_settings(base_url, model, entered_key, timeout_sec=15):
    """Do not test a typed key that runtime's environment priority would override."""
    environment = os.environ.get(secrets.ENV_KEY, "").strip()
    entered_key = entered_key.strip()
    if environment and entered_key and environment != entered_key:
        raise SetupError("key_override", "环境变量 BAGU_LLM_API_KEY 会覆盖所填 Key；请修改或移除该环境变量，再重新测试。")
    current = config.read_user_config().get("llm")
    current = current if isinstance(current, dict) else {}
    try:
        key = entered_key or secrets.resolve_api_key(current)
    except (ValueError, TypeError, AttributeError):
        key = entered_key
    return validate_settings(base_url, model, key, timeout_sec)


def _receipt(settings):
    public = json.dumps(settings.public(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hmac.new(settings.api_key.encode("utf-8"), public, hashlib.sha256).hexdigest()


def saved_model_verified(configuration=None):
    try:
        state = read_state()
        configuration = config.read_user_config() if configuration is None else configuration
        llm = configuration.get("llm")
        if not isinstance(llm, dict):
            return False
        settings = validate_settings(llm.get("base_url", ""), llm.get("model", ""),
            secrets.resolve_api_key(llm), llm.get("timeout_sec", 15))
        receipt = state.get("model_verification")
        return isinstance(receipt, str) and hmac.compare_digest(receipt, _receipt(settings))
    except (SetupError, OSError, ValueError, TypeError, AttributeError):
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # Authorization must never be forwarded to a redirect target.
        return None


def test_connection(settings, *, cancel=None, opener=None):
    """One explicit, bounded chat request; response content is never displayed/logged."""
    settings = validate_settings(settings.base_url, settings.model, settings.api_key, settings.timeout_sec)
    cancel = cancel if cancel is not None else threading.Event()
    deadline = time.monotonic() + settings.timeout_sec
    def check():
        if cancel.is_set():
            raise SetupError("cancelled", "连接测试已取消，可修改配置后重新测试。")
        if time.monotonic() >= deadline:
            raise SetupError("timeout", "连接测试超时，请检查网络、代理或增加超时后重试。")
    check()
    request = urllib.request.Request(settings.base_url + "/chat/completions",
        data=json.dumps({"model": settings.model, "max_tokens": 8,
            "messages": [{"role": "user", "content": "请回复 OK"}]}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + settings.api_key})
    opener = opener or urllib.request.build_opener(_NoRedirect()).open
    try:
        with opener(request, timeout=settings.timeout_sec) as response:
            parts, size = [], 0
            read = getattr(response, "read1", response.read)
            while True:
                check()
                chunk = read(4096)
                check()
                if not chunk:
                    break
                size += len(chunk)
                if size > 65536:
                    raise SetupError("response", "接口返回内容过大，请确认基础地址指向聊天 API 后重试。")
                parts.append(chunk)
        value = json.loads(b"".join(parts).decode("utf-8"))
        choices = value.get("choices") if isinstance(value, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise SetupError("response", "接口未返回兼容的聊天响应，请检查基础地址、模型名称及接口格式后重试。")
        check()
        return VerifiedModel(settings, _receipt(settings))
    except urllib.error.HTTPError as error:
        error.close()
        messages = {401: "Key 无效或未授权，请核对 Key 后重试。", 403: "接口拒绝访问，请检查模型权限。",
                    404: "接口或模型不存在，请核对基础地址和模型名称。",
                    429: "请求受限，请检查额度或稍后重试。"}
        raise SetupError("http", f"HTTP {error.code}：" + messages.get(error.code, "接口暂不可用，请稍后重试；若为重定向，请填写最终基础地址。")) from None
    except (TimeoutError, urllib.error.URLError):
        check()
        raise SetupError("network", "连接失败或超时，请检查基础地址、网络和代理后重试。") from None
    except (ValueError, UnicodeError, TypeError):
        raise SetupError("response", "接口响应不是有效的聊天 JSON，请核对基础地址和模型后重试。") from None
    except SetupError:
        raise
    except Exception:
        raise SetupError("network", "测试未完成，请检查基础地址、网络和模型配置后重试。") from None


def save_verified_model(verified):
    """Persist only a tested model, requiring credential-store readback."""
    settings = verified.settings
    if not hmac.compare_digest(verified.receipt, _receipt(settings)):
        raise SetupError("changed", "模型配置已变化，请重新测试连接后保存。")
    environment = os.environ.get(secrets.ENV_KEY, "").strip()
    if environment and environment != settings.api_key:
        raise SetupError("changed", "生效的环境变量 Key 已变化，请重新测试连接。")
    try:
        configuration = config.read_user_config()
        # Establish the gate before writing model config, so an interrupted
        # receipt write cannot make a first-use save look like a legacy install.
        begin()
        state = read_state()
    except (OSError, ValueError):
        raise SetupError("config", "无法读取原配置，请修复 config.json 的格式或权限后再保存。") from None
    if secrets.save_api_key(settings.api_key) != "keyring" or not secrets._keyring_matches(settings.api_key):
        raise SetupError("credentials", "无法确认 Key 已存入 Windows 凭据管理器；请检查 keyring 和系统凭据服务，再点击保存重试。")
    configuration["llm"] = settings.public()
    state["model_verification"] = verified.receipt
    try:
        config._write_config_atomic(configuration)
        atomic_json(state_path(), state)
    except OSError:
        raise SetupError("save", "Key 已在凭据管理器中，但配置或进度保存失败；请检查文件写入权限后再次保存。") from None


@dataclass(frozen=True)
class WizardSnapshot:
    bank_ready: bool
    model_ready: bool
    projects_ready: bool
    base_url: str
    model: str
    timeout_sec: float
    has_key: bool
    projects: tuple

    @property
    def step(self):
        return "bank" if not self.bank_ready else "model" if not self.model_ready else "projects" if not self.projects_ready else "finish"


def snapshot(report):
    configuration = config.read_user_config()
    llm = configuration.get("llm")
    llm = llm if isinstance(llm, dict) else {}
    try:
        has_key = bool(secrets.resolve_api_key(llm))
    except (TypeError, AttributeError):
        has_key = False
    roots = configuration.get("project_roots")
    roots = tuple(root for root in roots if isinstance(root, str)) if isinstance(roots, list) else ()
    checks = {item.name: item.ready for item in report.checks}
    return WizardSnapshot(checks.get("bank", False), saved_model_verified(configuration),
        checks.get("projects", False), str(llm.get("base_url") or ""), str(llm.get("model") or ""),
        llm.get("timeout_sec") or 15, has_key, roots)
