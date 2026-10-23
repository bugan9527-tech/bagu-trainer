# -*- coding: utf-8 -*-
"""隐私最小化改造验收测试。

覆盖验收标准：
  1. fake secret 不出现在任何待发送 payload / 日志 / 引擎返回值中；
  2. config.json/.env/私钥类文件永不进入文件监听上下文；
  3. 无项目归属的会话默认不参与出题；
  4. 白名单外 project_root（含路径穿越）必须被拒绝；
  5. 密钥不再明文写入 config.json。

全部 fake：无网络、无真实 LLM、无真实 keyring、无真实 .trae-cn、
无真实 config.json；目录一律 tmp_path。
"""
import json
from datetime import datetime

import pytest

import bagu_trainer.config as config_mod
import bagu_trainer.diff_reader as diff_reader
import bagu_trainer.quiz_generator as qg
import bagu_trainer.secrets as secrets_mod
import bagu_trainer.engine as engine
import bagu_trainer.session_reader as session_reader

FAKE_KEY = "sk-fake-secret-key-9f8e7d6c5b4a"
FAKE_PASSWORD = "db-pass-12345-xyz"
FAKE_TOKEN = "tok-abcdef1234567890"

UNVERIFIED = {
    "id": "u1", "knowledge_point": "缓存穿透", "difficulty": "medium",
    "question": "以下关于缓存穿透的说法正确的是？",
    "options": {}, "answer": "",
    "analysis": "来源: r/a.md",
    "source_paths": ["r/a.md"],
    "source_excerpt": (
        "缓存穿透指查询不存在的数据，缓存无法命中后请求会持续落到数据库。"
        "常见治理方式包括缓存空值并设置较短 TTL、使用布隆过滤器提前拦截，"
        "以及在入口校验参数；这些措施都必须结合业务一致性要求选择。"
    ),
    "excerpt_source": "r/a.md",
}
VALID_LLM_CONTENT = json.dumps({
    "question": "结合场景，缓存穿透的正确处理方式是？",
    "options": {"A": "空值缓存", "B": "限流", "C": "熔断", "D": "降级"},
    "answer": "A",
    "analysis": "缓存穿透可用空值缓存兜底。",
}, ensure_ascii=False)


# ============ 1. 敏感文件名与监听过滤 ============
def test_is_sensitive_path_blacklist():
    sensitive = ["config.json", ".env", ".env.local", "api.key", "server.pem",
                 "cert.p12", "id_rsa", "credentials.json", "access-token.txt",
                 "apikeys.json", "sub/config.json"]
    for name in sensitive:
        assert secrets_mod.is_sensitive_path(name), name
    allowed = ["app.py", "README.md", "package.json", "KEY_MIGRATION.md", "notes.txt"]
    for name in allowed:
        assert not secrets_mod.is_sensitive_path(name), name


def test_diff_reader_never_collects_secrets(tmp_path, monkeypatch):
    """config.json（含 fake secret）与私钥类文件不进快照、不进片段"""
    (tmp_path / "app.py").write_text("def cache():\n    return 'redis'\n", encoding="utf-8")
    (tmp_path / "config.json").write_text(
        json.dumps({"llm": {"api_key": FAKE_KEY, "model": "m"}}), encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=%s\n" % FAKE_KEY, encoding="utf-8")
    (tmp_path / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\n%s\n-----END PRIVATE KEY-----" % FAKE_KEY, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("普通笔记", encoding="utf-8")
    (tmp_path / "data.docx").write_text("非白名单扩展名", encoding="utf-8")
    dr = diff_reader.DiffReader(str(tmp_path))
    monkeypatch.setattr(dr, "is_git_repo", lambda: False)
    res = dr.changes()
    assert "app.py" in res["files"]
    assert "notes.txt" in res["files"]
    for banned in ("config.json", ".env", "server.pem", "data.docx"):
        assert banned not in res["files"], banned
    text = "\n".join([res["diff_text"], json.dumps(res["files"])])
    assert FAKE_KEY not in text
    assert "PRIVATE KEY" not in text


def test_diff_reader_git_diff_filtered():
    """git diff 按文件块过滤：config.json 块的变更被整体剔除"""
    stdout = (
        "diff --git a/app.py b/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-x\n+def f(): return 1\n"
        "diff --git a/config.json b/config.json\n"
        "+++ b/config.json\n"
        "@@ -1 +1 @@\n"
        '-  "api_key": ""\n'
        '+  "api_key": "%s"\n'
        "diff --git a/secret.pem b/secret.pem\n"
        "+++ b/secret.pem\n"
        "@@ -1 +1 @@\n"
        "+-----BEGIN PRIVATE KEY-----\n") % FAKE_KEY
    out = diff_reader.DiffReader._filter_git_diff(stdout)
    assert out["files"] == ["app.py"]
    assert FAKE_KEY not in out["diff_text"]
    assert "PRIVATE KEY" not in out["diff_text"]
    assert "return 1" in out["diff_text"]


# ============ 2. 统一脱敏单元 ============
def test_redact_text_patterns():
    text = ("api_key=%s\n" % FAKE_KEY +
            '密码 password: %s\n' % FAKE_PASSWORD +
            "接口 Authorization: Bearer %s\n" % FAKE_TOKEN +
            "另一个 key k=sk-another-abcdef123456\n"
            "-----BEGIN RSA PRIVATE KEY-----\nMIIFAKE==\n-----END RSA PRIVATE KEY-----")
    out = secrets_mod.redact_text(text, extra_secrets=[FAKE_KEY])
    assert FAKE_KEY not in out
    assert FAKE_PASSWORD not in out
    assert FAKE_TOKEN not in out
    assert "sk-another-abcdef123456" not in out
    assert "PRIVATE KEY" not in out
    assert out.count("[REDACTED]") >= 4


def test_resolve_api_key_priority(monkeypatch):
    monkeypatch.setenv(secrets_mod.ENV_KEY, "env-key-11111")
    assert secrets_mod.resolve_api_key({"api_key": "legacy-22222"}) == "env-key-11111"

    monkeypatch.delenv(secrets_mod.ENV_KEY, raising=False)
    monkeypatch.setattr(secrets_mod, "_load_keyring",
                        lambda: _FakeKeyring("ring-key-33333"))
    assert secrets_mod.resolve_api_key({"api_key": "legacy-22222"}) == "ring-key-33333"

    monkeypatch.setattr(secrets_mod, "_load_keyring", lambda: None)
    assert secrets_mod.resolve_api_key({"api_key": "legacy-22222"}) == "legacy-22222"
    assert secrets_mod.resolve_api_key({}) == ""


class _FakeKeyring:
    def __init__(self, stored): self._v = stored

    def get_password(self, s, u): return self._v

    def set_password(self, s, u, v): self._v = v

    def delete_password(self, s, u): self._v = ""


# ============ 3. 密钥不落盘 ============
def test_update_llm_key_to_keyring_not_plaintext(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_file)
    saved = {}

    def _save(k):
        saved["key"] = k
        return "keyring"

    monkeypatch.setattr(secrets_mod, "save_api_key", _save)
    monkeypatch.setattr(secrets_mod, "clear_api_key", lambda: saved.setdefault("cleared", True))

    stored = config_mod.update_llm({"base_url": "https://fake/v1",
                                    "api_key": FAKE_KEY, "model": "m",
                                    "timeout_sec": 30})
    assert stored == "keyring"
    assert saved["key"] == FAKE_KEY
    content = cfg_file.read_text(encoding="utf-8")
    assert FAKE_KEY not in content
    assert json.loads(content)["llm"]["api_key"] == ""

    # 清空：不进明文
    config_mod.update_llm({"base_url": "https://fake/v1", "api_key": "",
                           "model": "m", "timeout_sec": 30},
                          clear_api_key=True)
    assert saved.get("cleared") is True
    assert FAKE_KEY not in cfg_file.read_text(encoding="utf-8")


def test_update_llm_fallback_never_writes_plaintext(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(secrets_mod, "save_api_key", lambda k: "fallback")
    monkeypatch.setattr(secrets_mod, "clear_api_key", lambda: None)
    stored = config_mod.update_llm({"base_url": "https://fake/v1",
                                    "api_key": FAKE_KEY, "model": "m",
                                    "timeout_sec": 30})
    assert stored == "fallback"
    content = cfg_file.read_text(encoding="utf-8")
    assert FAKE_KEY not in content  # 关键：兜底路径也绝不写明文
    assert json.loads(content)["llm"]["api_key"] == ""


# ============ 4. LLM 载荷脱敏 + 截断 + 异常脱敏 ============
def _capture_urlopen(capture):
    def urlopen(req, timeout=None):
        payload = json.loads(req.data.decode("utf-8"))
        is_review = "review_context_grounding" in payload["messages"][-1]["content"]
        capture["review_payload" if is_review else "payload"] = payload
        capture["auth"] = req.headers.get("Authorization") or ""
        if is_review:
            content = json.dumps({"context_relevant": True, "source_supported": True,
                "answer_unique": True, "no_answer_leak": True, "correct_answer": "A",
                "reason": "上下文缓存穿透的处理需求有来源支持且答案唯一。"})
        else:
            candidate = json.loads(VALID_LLM_CONTENT)
            candidate["grounding"] = {"context_quote": "缓存穿透场景。",
                "source_quote": UNVERIFIED["source_excerpt"][:35],
                "rationale": "上下文明确涉及缓存穿透，需要依据来源选取缓存空值的治理措施。"}
            content = json.dumps(candidate, ensure_ascii=False)
        # 响应体必须是 OpenAI 兼容包装格式，content 才是试题 JSON
        return _FakeResp(json.dumps(
            {"choices": [{"message": {"content": content}}]},
            ensure_ascii=False))
    return urlopen


class _FakeResp:
    def __init__(self, text): self._t = text

    def read(self, size=-1):
        body = self._t.encode("utf-8")
        return body if size < 0 else body[:size]

    def __enter__(self): return self

    def __exit__(self, *a): return False


def test_llm_payload_never_contains_secrets(monkeypatch):
    monkeypatch.setattr(qg, "load_config",
                        lambda: {"llm": {"base_url": "https://fake.local/v1",
                                         "api_key": "sk-test-key-1111",
                                         "model": "m", "timeout_sec": 5}})
    capture = {}
    gen = qg.QuizGenerator(urlopen=_capture_urlopen(capture))
    context = ("上下文：当前 Key 是 sk-test-key-1111（不该外泄）；"
               '另有 api_key="%s"、Bearer %s、密码 password: %s。'
               "\n缓存穿透场景。" % (FAKE_KEY, FAKE_TOKEN, FAKE_PASSWORD))
    context += "填充文本" * 800  # 超长：验证截断
    quiz = gen.generate(UNVERIFIED, context)
    assert quiz.get("available") is not False
    body = json.dumps([capture["payload"], capture["review_payload"]], ensure_ascii=False)
    for bad in ("sk-test-key-1111", FAKE_KEY, FAKE_TOKEN, FAKE_PASSWORD):
        assert bad not in body, bad
    assert "[REDACTED]" in body
    user_msg = capture["payload"]["messages"][1]["content"]
    ctx_part = user_msg.split("AI 当前工作上下文：\n")[1]
    assert len(ctx_part) <= 2000
    # 认证头是唯一合法携带 Key 的位置（对服务商鉴权必需），不在消息正文中
    assert capture["auth"] == "Bearer sk-test-key-1111"


def test_llm_error_message_never_leaks_exception_text(monkeypatch):
    monkeypatch.setattr(qg, "load_config",
                        lambda: {"llm": {"base_url": "https://fake.local/v1",
                                         "api_key": "sk-test", "model": "m",
                                         "timeout_sec": 5}})
    leak = "leaked-secret-in-exception-1234567890"

    def boom(req, timeout=None):
        raise RuntimeError(leak)

    gen = qg.QuizGenerator(urlopen=boom)
    unique = dict(UNVERIFIED, knowledge_point="没有回退题的唯一主题",
                  question="这个唯一主题如何工作？", keywords=[])
    quiz = gen.generate(unique, "上下文")
    assert quiz.get("available") is False
    reason = quiz["unavailable_reason"]
    assert "RuntimeError" in reason
    assert leak not in reason  # 异常文本不回显
    assert "sk-test" not in reason


# ============ 5. 服务端：返回值与内部上下文均不含秘密 ============
def _set_diff(diff, text):
    diff.results = [{"source": "git", "files": ["x.py"], "diff_text": text}]


def test_server_returns_and_internal_state_clean(env, tmp_path, monkeypatch):
    env["cfg"]["project_roots"] = [str(tmp_path)]
    # 模拟「当前 Key」已解析；验证整值脱敏链路
    monkeypatch.setattr(secrets_mod, "resolve_api_key", lambda llm=None: FAKE_KEY)
    _set_diff(env["diff"],
              "改缓存代码：连接串里写了 api_key=%s、Bearer %s 和"
              "数据库密码 password: %s，redis 缓存穿透空值缓存。" %
              (FAKE_KEY, FAKE_TOKEN, FAKE_PASSWORD))
    r = engine.process_activity(project_root=str(tmp_path),
                                  description="刚接了 redis 配置")
    assert r["ok"] is True and len(r["quizzes"]) == 1
    dumped = json.dumps(r, ensure_ascii=False)
    for bad in (FAKE_KEY, FAKE_TOKEN, FAKE_PASSWORD):
        assert bad not in dumped, bad
    # matcher / generator 收到的上下文同样已脱敏（进 matcher/LLM 前的统一闸门）
    assert "[REDACTED]" in env["matcher"].last_text
    assert "[REDACTED]" in env["gen"].last_ctx
    for bad in (FAKE_KEY, FAKE_TOKEN, FAKE_PASSWORD):
        assert bad not in env["matcher"].last_text, bad
        assert bad not in env["gen"].last_ctx, bad
    # pending 公开记录也不含秘密
    assert FAKE_KEY not in json.dumps(engine._state["pending"], ensure_ascii=False)


def test_create_quiz_tool_redacts_user_context(env, monkeypatch):
    class FakeKB:
        def get(self, kid):
            if kid != "kp1":
                return None
            return {"id": "kp1", "knowledge_point": "缓存穿透", "difficulty": "easy",
                    "question": "Q?", "options": {}, "answer": "",
                    "analysis": "来源: r/a.md", "source_paths": ["r/a.md"],
                    "source_excerpt": "缓存穿透指查询不存在的数据……",
                    "excerpt_source": "r/a.md"}
    monkeypatch.setattr(engine, "kb", FakeKB())
    monkeypatch.setattr(secrets_mod, "resolve_api_key", lambda llm=None: FAKE_KEY)
    ctx = '用户给的片段：api_key="%s" Bearer %s' % (FAKE_KEY, FAKE_TOKEN)
    r = engine.create_quiz("kp1", context=ctx)
    assert r["ok"] is True and r.get("quiz_id")
    assert FAKE_KEY not in env["gen"].last_ctx
    assert FAKE_TOKEN not in env["gen"].last_ctx
    assert "[REDACTED]" in env["gen"].last_ctx


# ============ 6. project_root 白名单 ============
def test_whitelist_outside_rejected(env, tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    env["cfg"]["project_roots"] = [str(allowed)]

    r = engine.next_quiz(project_root=str(outside))
    assert r["ok"] is False and r["has_pending"] is False
    assert "白名单" in r["error"]
    assert env["gen"].calls == 0

    rep = engine.process_activity(project_root=str(outside))
    assert rep["ok"] is False and "白名单" in rep["error"]
    assert env["gen"].calls == 0


def test_whitelist_path_traversal_rejected(env, tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    env["cfg"]["project_roots"] = [str(allowed)]
    traversal = str(allowed / ".." / "outside")  # 路径穿越：真实指向白名单外
    r = engine.next_quiz(project_root=traversal)
    assert r["ok"] is False and "白名单" in r["error"]
    assert env["gen"].calls == 0


# ============ 7. 会话摄取：默认关闭 / 项目归属过滤 ============
def _mk_entry(intent, t=None):
    return json.dumps({
        "intent": intent, "actions": [], "outcome": "", "learned": [],
        "message_summary_time": (t or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        "message_id": "m1"}, ensure_ascii=False)


def _mk_projects(root):
    """造两个假项目会话目录（含密码级敏感 content 的 intent）"""
    key_a = session_reader._project_key(root.parent / "proj-a")
    key_b = session_reader._project_key(root.parent / "proj-b")
    a = root / "memory" / "projects" / (key_a + "--p2-aaaaaa") / "20261018"
    b = root / "memory" / "projects" / (key_b + "--p2-bbbbbb") / "20261018"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "session_memory_a.jsonl").write_text(
        _mk_entry("改 proj-a 缓存，用了密码 password: %s" % FAKE_PASSWORD) + "\n",
        encoding="utf-8")
    (b / "session_memory_b.jsonl").write_text(
        _mk_entry("改 proj-b 接口") + "\n", encoding="utf-8")
    return a, b


def test_session_reader_project_attribution(tmp_path):
    trae = tmp_path / "trae"
    _mk_projects(trae)
    # 旧接口（不传 project_root）：全量，仍可用（调用方需自行承担显式开启的场景）
    all_entries = session_reader.read_recent_entries(trae, since_minutes=180)
    assert len(all_entries) == 2
    # 传 project_root：只取唯一归属项目
    mine = session_reader.read_recent_entries(
        trae, since_minutes=180, project_root=str(tmp_path / "proj-a"))
    assert len(mine) == 1
    assert "proj-a" in mine[0]["project"]
    assert "proj-b" not in mine[0]["project"]
    # 无法可靠归属 -> 返回空（不读取任何会话）
    none = session_reader.read_recent_entries(
        trae, since_minutes=180, project_root=str(tmp_path / "no-such-proj"))
    assert none == []
    # 归属歧义（同前缀两个目录）-> 返回空
    (tmp_path / "trae" / "memory" / "projects"
     / (session_reader._project_key(tmp_path / "proj-a") + "--p2-cccccc")).mkdir(parents=True)
    amb = session_reader.read_recent_entries(
        trae, since_minutes=180, project_root=str(tmp_path / "proj-a"))
    assert amb == []


def test_server_session_ingest_off_and_gated(env, tmp_path, monkeypatch):
    env["cfg"]["project_roots"] = [str(tmp_path)]
    trae = tmp_path / "trae"
    _mk_projects(trae)
    calls = {}
    monkeypatch.setattr(engine, "find_trae_home", lambda cfg_home=None: trae)

    def _fake_read(trae_home, since_minutes=180, limit=8, project_root=None):
        calls["project_root"] = project_root
        return []

    monkeypatch.setattr(engine, "read_recent_entries", _fake_read)
    # 默认关闭：完全不读取会话
    info = engine._collect_context("", str(tmp_path))
    assert "project_root" not in calls
    assert "会话记录" not in " ".join(info["sources"])
    # 显式开启：只按当前项目归属读取
    env["cfg"]["session_ingest"] = True
    info = engine._collect_context("", str(tmp_path))
    assert calls.get("project_root") is not None
