"""Desktop learning engine: matching, quiz lifecycle, grading and persistence.

This module has no transport registration or independent background loop.
DesktopMonitor owns change detection and DesktopApp owns interaction.
"""
import copy
import hashlib
import ntpath
import os
import subprocess
import sys
import time
import uuid
import contextvars
import types
from contextlib import contextmanager
from pathlib import Path

from bagu_trainer.infrastructure.paths import canonical_project_root as _normalize_project_root
from bagu_trainer.infrastructure import secrets
from bagu_trainer.infrastructure import config
from bagu_trainer.infrastructure.config import PROJECT_ROOT
from bagu_trainer.infrastructure.config import load_config
from bagu_trainer.infrastructure.diff_reader import DiffReader
from bagu_trainer.infrastructure.knowledge_base import KnowledgeBase
from bagu_trainer.infrastructure.matcher import Matcher
from bagu_trainer.application.learning.generator import get_verified_fallback
from bagu_trainer.domain.quizzes import admission_failure as _generation_failure
from bagu_trainer.domain.quizzes import repractice_failure as _repractice_failure
from bagu_trainer.domain.quizzes import quiz_deadline
from bagu_trainer.infrastructure.session_reader import entries_to_text
from bagu_trainer.infrastructure.session_reader import find_trae_home
from bagu_trainer.infrastructure.session_reader import read_recent_entries
from bagu_trainer.infrastructure.store import Store

_ACTIVE_ENGINE = contextvars.ContextVar("bagu_active_engine", default=None)
_PROJECT_READER = contextvars.ContextVar("bagu_project_reader", default=None)
_DEPENDENCIES = frozenset({"cfg", "kb", "matcher", "generator", "scorer", "session", "learning"})


def _engine():
    """Return the engine bound to this call, lazily assembling the app default."""
    current = _ACTIVE_ENGINE.get()
    if current is not None:
        return current
    from bagu_trainer.bootstrap import get_engine
    return get_engine()


def _runtime_config():
    return _engine().load_config()


@contextmanager
def _using(engine):
    token = _ACTIVE_ENGINE.set(engine)
    try:
        yield
    finally:
        _ACTIVE_ENGINE.reset(token)

# 薄弱知识点阈值与上限（get_learning_stats 用）
WEAK_MASTERY = 0.6
WEAK_LIMIT = 10

_PROJECT_SCOPE_NOTE = (
    "待答队列与未作答知识点冷却按 project_root 隔离；作答记录、掌握度和到期"
    "复习计划按知识点全局共享，但复习题只从已作答历史重建，不会把其他项目"
    "未完成的 pending 题直接推送给当前项目。"
)


def _now() -> float:
    """统一时钟入口（测试可 monkeypatch 为固定时间，保证确定性）"""
    return time.time()


def _db_path() -> str:
    """SQLite 路径：config db_path（相对用户数据目录）或默认 data/trainer.db。"""
    return str(config.db_path((_runtime_config().get("db_path") or "").strip()))


def _project_roots():
    """Use the acknowledged desktop scope; a config write alone cannot apply it."""
    roots = _engine().session._watch_roots
    return list(roots) if roots is not None else _runtime_config().get("project_roots", [str(PROJECT_ROOT)])


def reload_bank():
    """Use the newly activated bank without replacing pending/history state."""
    owner = _engine()
    with content_maintenance():
        # Read the active pointer/files under the same lock as deletion. A
        # loader waiting behind maintenance must not install a stale bank.
        updated = owner.knowledge_base_factory()
        updated_matcher = owner.matcher_factory(updated)
        owner.kb, owner.matcher = updated, updated_matcher
        owner.session._content_changed()


@contextmanager
def content_maintenance():
    """File replacement/deletion must serialize with this live learning session."""
    owner = _engine()
    with _using(owner):
        with owner.session.maintenance():
            owner.session._ensure_open(maintenance=True)
            yield


def initialize():
    """Start a fresh session after terminal shutdown; DB hydration stays lazy."""
    _engine().initialize()


def observe_project(root, *, reader_factory=None, reader=None):
    """Collect one scoped/redacted signal, optionally establishing a baseline."""
    owner = _engine().session
    with owner._lock:
        owner._ensure_open()
    if reader is not None:
        token = _PROJECT_READER.set(reader)
        try:
            return _collect_context("", root)
        finally:
            _PROJECT_READER.reset(token)
    with owner._readers_lock:
        if reader_factory is not None:
            owner._diff_readers[_normalize_project_root(root)] = reader_factory(root)
    return _collect_context("", root)


def generate_activity(root, info, *, retry_failed_ids=(), revision=None, stop=None):
    """Reserve under the session lock; never hold it across a model request."""
    return _generate_matches(
        info["context"], root, _engine().cfg["max_quizzes_per_hit"], _caps(),
        " / ".join(info.get("sources") or [])[:200], retry_failed_ids=retry_failed_ids,
        revision=revision, stop=stop)


def invalidate_content():
    """Invalidate delivery and in-flight content before maintenance replaces files."""
    with _engine().session.maintenance():
        _engine().session.invalidate_content()
        from bagu_trainer.infrastructure.concepts import _load_concept_profiles
        from bagu_trainer.infrastructure.fallback_bank import _load_fallback_cached
        from bagu_trainer.infrastructure.matcher import _tokenize_cached
        from bagu_trainer.infrastructure.matcher import _domain_profile
        _load_concept_profiles.cache_clear()
        _load_fallback_cached.cache_clear()
        _tokenize_cached.cache_clear()
        _domain_profile.cache_clear()


def _open_config_window() -> bool:
    """以独立进程弹出 AI 接口配置窗口（tkinter 在独立进程运行）"""
    try:
        from bagu_trainer.infrastructure.paths import command
        subprocess.Popen(
            command("--configure"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except OSError as e:
        print(f"[config] 弹出配置窗口失败（{type(e).__name__}）", file=sys.stderr)
        return False


def _public_quiz(quiz_id: str, quiz: dict, project_root=None) -> dict:
    """发往用户侧的题目：隐藏答案与解析"""
    out = {
        "quiz_id": quiz_id,
        "kp_id": quiz.get("kp_id"),
        "knowledge_point": quiz.get("kp_name"),
        "difficulty": quiz.get("difficulty"),
        "question": quiz.get("question"),
        "options": quiz.get("options"),
        "generated_by": quiz.get("generated_by"),
    }
    if quiz.get("excerpt_source"):
        out["source_ref"] = quiz["excerpt_source"]
    if quiz.get("review"):
        out["review"] = True
    if project_root is not None:
        out["project_root"] = (_normalize_project_root(project_root)
                               or str(project_root))
    return out


def _quiz_snapshot(quiz: dict) -> dict:
    """落库快照：剔除内部 _ 前缀字段（保留题目/选项/答案/解析等作答所需内容）"""
    return {k: v for k, v in quiz.items() if not str(k).startswith("_")}


def _resolve_project_root(project_root: str):
    """project_root 白名单校验（隐私边界）。
    返回 (Path|None, error|None)：
      - realpath 归一化后再比较，杜绝路径穿越（..、大小写、符号链接）
      - 必须是 config project_roots 白名单中的目录或其子目录，否则拒绝
    """
    whitelist = _project_roots()
    norm_roots = []
    for r in whitelist:
        normalized = _normalize_project_root(r)
        if normalized:
            norm_roots.append(normalized)
    raw = (project_root or "").strip() or (norm_roots[0] if norm_roots else str(PROJECT_ROOT))
    if not Path(raw).is_dir():
        return None, f"无效 project_root: {raw} 不是存在的目录"
    real = _normalize_project_root(raw)
    if not real:
        return None, f"无效 project_root: 无法解析 {raw}"
    allowed = any(real == nr or real.startswith(nr + os.sep) for nr in norm_roots)
    if not allowed:
        return None, (f"project_root 被拒绝: {raw} 不在 project_roots 白名单内"
                      "（隐私保护：拒绝采集白名单外目录，含路径穿越）")
    return Path(real), None


def _collect_context(description: str, project_root: str) -> dict:
    """双信号源：会话记录（意图，仅限当前项目且显式开启）+ 文件变动（实例）。
    隐私保证：
      - project_root 必须通过白名单校验；
      - 会话摄取默认关闭（session_ingest），开启时也只读与当前项目
        路径唯一匹配的项目目录，无归属即不读；
      - 拼接后的上下文在进入 matcher/LLM 前统一脱敏并截断。
    """
    c = _runtime_config()
    root, err = _resolve_project_root(project_root)
    if err:
        return {"context": "", "sources": [], "error": err,
                "project_root": (project_root or "").strip(), "changes": None}
    pieces, sources = [], []
    if description:
        pieces.append(str(description))
        sources.append("调用方描述")
    trae_home = find_trae_home(c.get("trae_home"))
    if trae_home and c.get("session_ingest"):
        entries = read_recent_entries(
            trae_home, since_minutes=c.get("session_recent_minutes", 180),
            project_root=str(root))
        text = entries_to_text(entries)
        if text:
            pieces.append(text)
            sources.append(f".trae-cn 会话记录({len(entries)} 条)")
    key = str(root)
    reader = _PROJECT_READER.get()
    if reader is None:
        owner = _engine().session
        with owner._readers_lock:
            if key not in owner._diff_readers:
                owner._diff_readers[key] = DiffReader(key)
            reader = owner._diff_readers[key]
    changes = reader.changes()
    if changes["diff_text"]:
        pieces.append(changes["diff_text"])
    sources.append(f"文件变动[{changes['source']}]({len(changes['files'])} 个文件)")
    # 统一脱敏（当前 Key/ sk-*/Bearer/赋值式/私钥）+ 字符上限，matcher/LLM 前最后一道闸
    secret_key = secrets.resolve_api_key(c.get("llm"))
    ctx = secrets.redact_text("\n".join(pieces), [secret_key] if secret_key else [])
    ctx = ctx[:int(c.get("context_char_limit") or 4000)]
    return {
        "context": ctx,
        "sources": sources,
        "project_root": str(root),
        "changes": changes,
    }


def _caps() -> dict:
    """实时读取容量/TTL 配置（低成本，便于测试与运行时调整）"""
    c = _runtime_config()
    return {
        "ttl": int(c["quiz_ttl_seconds"]),
        "max_pending": int(c["max_pending_quizzes"]),
        "max_quizzes": int(c["max_quizzes"]),
        "seen_cap": int(c["seen_hash_capacity"]),
    }


def _quiz_deadline(now, caps):
    # Keep persisted deadlines finite and compatible with SQLite/JSON readers.
    return quiz_deadline(now, caps["ttl"])


def _norm_context(ctx: str) -> str:
    """归一化上下文：压缩多余空白，保证换行/缩进差异不构成新指纹"""
    return " ".join((ctx or "").split())


def _match_hash(project_root: str, kp_id: str, ctx: str) -> str:
    """去重指纹：同一 (项目, 知识点, 归一化上下文) 只出一次题"""
    material = "\x1f".join((str(project_root), str(kp_id), _norm_context(ctx)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _public_pending(entry: dict) -> dict:
    """把 pending 记录还原为发往用户侧的题目（剔除内部状态字段）"""
    internal = {"status", "created_at", "delivered_at", "answered_at",
                "expires_at"}
    out = {k: v for k, v in entry.items() if k not in internal}
    if entry.get("project_root"):
        out["project_root"] = (_normalize_project_root(entry["project_root"])
                               or entry["project_root"])
    return copy.deepcopy(out)


def _generate_matches(ctx: str, project_root: str, top_n: int, caps: dict,
                      source_summary: str = "", *, retry_failed_ids=(), revision=None, stop=None,
                      deliver_generated=False):
    """Compatibility seam for the learning module's generation workflow."""
    return _engine().learning._generate_matches(
        ctx, project_root, top_n, caps, source_summary,
        retry_failed_ids=retry_failed_ids, revision=revision, stop=stop,
        deliver_generated=deliver_generated)


def _refresh(project_root: str, description: str = "",
             deliver_generated: bool = False) -> dict:
    """活动处理共用的受锁刷新流程：
    采集上下文 -> 匹配 -> 去重生成 -> 清理。
    返回 info/generated/matches/skipped/unavailable；白名单校验失败时
    返回 ok=False + error（不进行任何采集与生成）。deliver_generated=True
    供 process_activity 使用：生成结果直接以 delivered 入库，不暴露中间 ready 状态。
    """
    caps = _caps()
    with content_maintenance():
        revision = _engine().session._generation_token()
        info = _collect_context(description, project_root)
        if info.get("error"):
            _engine().session._cleanup_locked(caps)
            return {"ok": False, "error": info["error"], "info": info,
                    "generated": [], "matches": [], "skipped": 0,
                    "unavailable": []}
        if not info["context"].strip():
            _engine().session._cleanup_locked(caps)
            return {"info": info, "generated": [], "matches": [],
                    "skipped": 0, "unavailable": []}
    matches, generated, skipped, unavailable = _generate_matches(
        info["context"], str(info["project_root"]), _engine().cfg["max_quizzes_per_hit"], caps,
        " / ".join(info.get("sources") or [])[:200], revision=revision,
        deliver_generated=deliver_generated)
    with content_maintenance():
        _engine().session._cleanup_locked(caps)
    return {"info": info, "generated": generated, "matches": matches,
            "skipped": skipped, "unavailable": unavailable}


def list_knowledge_points(tag: str = "") -> dict:
    """列出题库知识点（可按 tag 过滤，如 http/cache/并发）"""
    kps = _engine().kb.list_knowledge_points()
    if tag:
        kps = [
            k for k in kps
            if tag in k["tags"]
            or tag in k.get("knowledge_tags", [])
            or tag == k.get("knowledge_domain")
        ]
    return {"total": len(kps), "knowledge_points": kps,
            "generation_policy": "llm_only",
            "hint": "调用 create_quiz(knowledge_point_id, context) 针对某知识点出题"}


def process_activity(description: str = "", project_root: str = "") -> dict:
    """Process activity through the shared matching and persistence pipeline.

    Project scope, deduplication, source checks and redaction apply to every call.
    """
    res = _refresh(project_root, description, deliver_generated=True)
    if res.get("ok") is False:
        return {"ok": False, "error": res["error"],
                "note": "project_root 校验失败：请确认该目录在 config.json 的 project_roots 白名单内"}
    info = res["info"]
    if not info["context"].strip():
        return {"ok": False, "note": "无可用信号：请提供 description，或确认 config.json 的 trae_home/project_roots 正确"}
    matches = res["matches"]
    if not matches:
        return {"ok": True, "matched": [], "quizzes": [],
                "note": "未命中题库知识点，可调用 list_knowledge_points 查看现有覆盖",
                "sampled_context": info["context"][:300]}
    result = {
        "ok": True,
        "project_root": info["project_root"],
        "scope_note": _PROJECT_SCOPE_NOTE,
        "sources": info["sources"],
        "changed_files": info["changes"]["files"][:50] if info["changes"] else [],
        "matched": [{"knowledge_point": m["question"]["knowledge_point"],
                     "score": m["score"], "hits": m["hits"],
                     "confidence": m["confidence"], "margin": m["margin"]}
                    for m in matches],
        "quizzes": res["generated"],
    }
    if res["unavailable"]:
        result["unavailable"] = res["unavailable"]
    if res["generated"]:
        result["next"] = "请让用户选择 A/B/C/D 作答，然后用 submit_answer(quiz_id, 用户答案) 判分并获取解析"
    elif res["unavailable"]:
        result["note"] = ("命中知识点但题目生成失败（原因见 unavailable 字段，"
                          "如未配置 LLM 或来源正文缺失），不会提供无法作答的题目。"
                          "可在主窗口配置出题模型后重试")
    if res["skipped"]:
        result["note"] = f"相同上下文已出过题，跳过 {res['skipped']} 道重复题目（防重复计费）"
    return result


def next_quiz(project_root: str = "") -> dict:
    """Return the next scoped quiz or due review, generating if needed.

    Delivery is atomic; a delivered question is not delivered a second time.
    """
    root_path, err = _resolve_project_root((project_root or "").strip())
    if err:
        return {"ok": False, "has_pending": False, "error": err,
                "note": "project_root 请传入 project_roots 白名单内的目录，或留空使用 config.json 的默认项目目录"}
    scope_root = str(root_path)
    with content_maintenance():
        caps = _caps()
        _engine().session._cleanup_locked(caps)
        # 0) 已有 ready 题直接交付（后台预生成复用）
        ready = _engine().session._first_ready_locked(scope_root)
        if ready:
            _engine().session._deliver_pending_locked(ready)
            return {"ok": True, "has_pending": True, "quiz": _public_pending(ready),
                    "project_root": scope_root, "scope_note": _PROJECT_SCOPE_NOTE,
                    "next": "把题目与选项呈现给用户作答，然后用 submit_answer(quiz_id, 用户答案) 判分并转述解析"}
        # 采集一次上下文（供相关性判断与新题生成复用，避免二次采集导致 watch 快照失真）
        info = _collect_context("", str(root_path))
        ctx = "" if info.get("error") else (info.get("context") or "").strip()
        matched_ids = None
        if ctx:
            matched_ids = [r["id"] for r in _engine().matcher.rank(ctx, top_n=5)]
            # 1) 与当前上下文相关的到期复习题优先（错题/到期知识点重练）
            review = _engine().session._deliver_due_review_locked(
                str(root_path), prefer=matched_ids, only_prefer=True, caps=caps)
            if review:
                return {"ok": True, "has_pending": True, "quiz": review,
                        "project_root": scope_root, "scope_note": _PROJECT_SCOPE_NOTE,
                        "next": "把题目与选项呈现给用户作答，然后用 submit_answer(quiz_id, 用户答案) 判分并转述解析"}
        revision = _engine().session._generation_token()
    if ctx:
        # 2) 当前上下文匹配到的新知识点（同步生成，受锁保护不并发重复生成）
        matches, generated, skipped, unavailable = _generate_matches(
            info["context"], str(info["project_root"]),
            _engine().cfg["max_quizzes_per_hit"], caps,
            " / ".join(info.get("sources") or [])[:200], revision=revision)
    else:
        matches, generated, skipped, unavailable = [], [], 0, []
    with content_maintenance():
        _engine().session._cleanup_locked(caps)
        ready = _engine().session._first_ready_locked(scope_root)
        if ready:
            _engine().session._deliver_pending_locked(ready)
            return {"ok": True, "has_pending": True, "quiz": _public_pending(ready),
                    "project_root": scope_root, "scope_note": _PROJECT_SCOPE_NOTE,
                    "next": "把题目与选项呈现给用户作答，然后用 submit_answer(quiz_id, 用户答案) 判分并转述解析"}
        # 3) 上下文无关的到期复习兜底：保证错题总会在更短周期内再次出现
        review = _engine().session._deliver_due_review_locked(
            str(root_path), prefer=matched_ids, only_prefer=False, caps=caps)
        if review:
            return {"ok": True, "has_pending": True, "quiz": review,
                    "project_root": scope_root, "scope_note": _PROJECT_SCOPE_NOTE,
                    "next": "把题目与选项呈现给用户作答，然后用 submit_answer(quiz_id, 用户答案) 判分并转述解析"}
        result = {"ok": True, "has_pending": False,
                  "project_root": scope_root, "scope_note": _PROJECT_SCOPE_NOTE,
                  "note": "当前无待答测试题（本次上下文未命中题库，或相同上下文已出过题）"}
        if unavailable:
            result["unavailable"] = unavailable
            result["note"] = ("当前无待答测试题：命中知识点但生成失败，"
                              "原因见 unavailable 字段（不提供无法作答的题目）")
        return result


def open_config_window() -> dict:
    """Open the desktop model settings in a separate GUI process."""
    if _open_config_window():
        return {"ok": True}
    return {"ok": False, "note": "配置窗口打开失败，请运行 scripts/config_window.py 查看错误"}


def create_quiz(knowledge_point_id: str, context: str = "") -> dict:
    """按知识点 ID 生成一道题。context 可附带当前场景（如相关代码片段），未提供时用会话记录。
    传入的 context 在进入 LLM 前统一脱敏（密钥/令牌自动替换为 [REDACTED]）并限制长度。
    生成失败（未配置 LLM、LLM 输出校验失败、来源正文缺失）时
    返回 ok=False 与明确原因，绝不返回无选项/无答案的题目。"""
    revision = _engine().session._generation_token()
    q = _engine().kb.get(knowledge_point_id) or get_verified_fallback(knowledge_point_id)
    if not q:
        return {"ok": False, "note": f"知识点 {knowledge_point_id} 不存在，可先 list_knowledge_points"}
    if context:
        c = _runtime_config()
        secret_key = secrets.resolve_api_key(c.get("llm"))
        context = secrets.redact_text(
            str(context), [secret_key] if secret_key else [])[:2000]
    else:
        context = _collect_context("", "")["context"]
    quiz = _engine().generator.generate(q, context)
    quiz["sources"] = q.get("sources", [])
    failure = _generation_failure(quiz, require_review=True)
    if failure:
        return {"ok": False, "knowledge_point_id": knowledge_point_id,
                "reason": failure,
                "note": "不提供无法作答的题目。可在主窗口配置出题模型后重试"}
    quiz_id = uuid.uuid4().hex[:8]
    with _engine().session._lock:
        if not _engine().session._accepts_result(revision):
            return {"ok": False, "note": "题库内容已变化，已丢弃过期出题结果。"}
        caps = _caps()
        now = _now()
        quiz.update({"_created_at": now, "_expires_at": _quiz_deadline(now, caps),
                     "_status": "delivered"})
        _engine().session._state["quizzes"][quiz_id] = quiz
        _engine().session._get_store_locked().create_quiz(
            quiz_id, quiz.get("kp_id") or knowledge_point_id,
            _quiz_snapshot(quiz), "delivered", str(PROJECT_ROOT),
            f"手动 create_quiz | kp={knowledge_point_id}",
            now, _quiz_deadline(now, caps), delivered_at=now)
        _engine().session._cleanup_locked(caps)
    return {"ok": True, **_public_quiz(quiz_id, quiz, str(PROJECT_ROOT)),
            "next": "用 submit_answer(quiz_id, 用户答案) 判分并获取解析"}


def submit_answer(quiz_id: str, answer: str) -> dict:
    """Compatibility entry point for the learning module's submit workflow."""
    return _engine().learning.submit_answer(quiz_id, answer)


def _history_path():
    return _engine().session.database_path


def history_time(timestamp):
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except (ValueError, TypeError, OverflowError, OSError):
        return "旧记录未提供有效时间"


def get_answer_history(**filters):
    """Read-only, deliberately independent of the session lock and current bank."""
    try:
        result = Store.history_page(_history_path(), **filters)
        for item in result["items"]:
            item["time_text"] = history_time(item["answered_at"])
            item["project_name"] = ntpath.basename(item["project_root"].rstrip("/\\")) or "未记录项目"
            item["result_text"] = "答对" if item["correct"] else "答错"
        return {"ok": True, **result}
    except Exception as error:
        return {"ok": False, "note": f"查询失败（{type(error).__name__}），请重试。"}


def get_answer_detail(attempt_id):
    try:
        row = Store.history_detail(_history_path(), int(attempt_id))
        if row is None:
            return {"ok": False, "note": "这条作答记录已不存在，请返回列表刷新。"}
        reason = _repractice_failure(row["snapshot"])
        return {"ok": True, **row, "time_text": history_time(row["answered_at"]),
                "retry_reason": reason, "can_retry": reason is None}
    except Exception as error:
        return {"ok": False, "note": f"详情查询失败（{type(error).__name__}），请重试。"}


def repractice_attempt(attempt_id):
    """Compatibility entry point for historical practice."""
    return _engine().learning.repractice_history(attempt_id)


def get_learning_stats() -> dict:
    """【学习统计·只读】返回学习闭环聚合数据：已作答题数、总作答次数、
    正确率、待复习数、薄弱知识点（掌握度低于 0.6 且至少作答一次）。
    不返回任何会话原文/代码内容，只有聚合数字与知识点名称。"""
    with content_maintenance():
        store = _engine().session._get_store_locked()
        s = store.stats(_now())
        weak = []
        for w in store.weak_kps(WEAK_LIMIT, WEAK_MASTERY):
            kp = _engine().kb.get(w["kp_id"])
            weak.append({
                "kp_id": w["kp_id"],
                "knowledge_point": (kp or {}).get("knowledge_point") or w["kp_id"],
                "mastery": w["mastery"],
                "attempts": w["attempts"],
                "correct_count": w["correct_count"],
                "next_review_at": w["next_review_at"],
            })
        total = s["total_attempts"]
        accuracy = round(s["correct_count"] / total, 4) if total else 0.0
        return {
            "ok": True,
            "answered_quizzes": s["answered_quizzes"],
            "total_attempts": total,
            "correct_count": s["correct_count"],
            "accuracy": accuracy,
            "due_reviews": s["due_reviews"],
            "weak_knowledge_points": weak,
            "note": ("只读统计：不含会话原文与代码内容。"
                     "待复习数为 next_review_at 已到期的知识点数。"),
        }


from bagu_trainer.application.learning.session import EngineSession


class Engine:
    """Own all mutable learning dependencies for one application instance.

    The public operations below retain the module's historic learning
    behaviour while binding every helper and session call to this instance.
    """

    def __init__(self, *, cfg, kb, matcher, generator, scorer, session=None,
                 config_loader=None, knowledge_base_factory=None,
                 matcher_factory=None):
        self.cfg = cfg
        self.kb = kb
        self.matcher = matcher
        self.generator = generator
        self.scorer = scorer
        self.config_loader = config_loader
        self.knowledge_base_factory = knowledge_base_factory or KnowledgeBase
        self.matcher_factory = matcher_factory or Matcher
        from bagu_trainer.application.learning import Learning
        self.learning = Learning(self, session or EngineSession(engine=self))

    PROJECT_ROOT = PROJECT_ROOT

    @property
    def session(self):
        """Compatibility access for legacy engine.py callers."""
        return self.learning._compat_session

    @session.setter
    def session(self, value):
        self.learning._attach_compat_session(value)

    @contextmanager
    def content_maintenance(self):
        with _using(self):
            with self.session.maintenance():
                self.session._ensure_open(maintenance=True)
                yield

    def close(self):
        self.learning.close()

    def initialize(self):
        """Reopen this engine after close without changing its identity."""
        self.learning.reopen()
        return self

    def _invoke(self, operation, *args, **kwargs):
        with _using(self):
            return operation(*args, **kwargs)

    def load_config(self):
        """Merge this instance's base config with its explicit loader overrides.

        This bound reader is also injected into the default generator; it does
        not need a compatibility context or the default application engine.
        """
        values = self.config_loader() if self.config_loader is not None else {}
        return {**self.cfg, **(values or {})}

    def reload_bank(self):
        return self._invoke(reload_bank)

    def observe_project(self, root, *, reader_factory=None, reader=None):
        return self._invoke(observe_project, root, reader_factory=reader_factory,
                            reader=reader)

    def generate_activity(self, root, info, *, retry_failed_ids=(), revision=None, stop=None):
        return self._invoke(generate_activity, root, info, retry_failed_ids=retry_failed_ids,
                            revision=revision, stop=stop)

    def invalidate_content(self):
        return self._invoke(invalidate_content)

    def list_knowledge_points(self, tag=""):
        return self._invoke(list_knowledge_points, tag)

    def process_activity(self, description="", project_root=""):
        return self._invoke(process_activity, description, project_root)

    def next_quiz(self, project_root=""):
        return self._invoke(next_quiz, project_root)

    def open_config_window(self):
        return self._invoke(open_config_window)

    def create_quiz(self, knowledge_point_id, context=""):
        return self._invoke(create_quiz, knowledge_point_id, context)

    def submit_answer(self, quiz_id, answer):
        return self._invoke(submit_answer, quiz_id, answer)

    def history_time(self, timestamp):
        return self._invoke(history_time, timestamp)

    def get_answer_history(self, **filters):
        return self._invoke(get_answer_history, **filters)

    def get_answer_detail(self, attempt_id):
        return self._invoke(get_answer_detail, attempt_id)

    def repractice_attempt(self, attempt_id):
        return self._invoke(repractice_attempt, attempt_id)

    def get_learning_stats(self):
        return self._invoke(get_learning_stats)


# Session methods are also part of the engine's interface when a monitor calls
# them directly. Bind their helper lookups without changing their lock nesting.
for _method_name, _method in list(EngineSession.__dict__.items()):
    if not callable(_method) or _method_name == "__init__":
        continue

    def _bound_session_method(self, *args, __method=_method, **kwargs):
        owner = self._engine or _engine()
        if self._engine is None:
            self._engine = owner
        with _using(owner):
            return __method(self, *args, **kwargs)

    _bound_session_method.__name__ = _method_name
    _bound_session_method.__doc__ = _method.__doc__
    setattr(EngineSession, _method_name, _bound_session_method)


class _EngineCompatibilityModule(types.ModuleType):
    """Lazy dependency aliases are descriptors, not module dictionary entries.

    Introspection and patch tools must restore the owning engine's value rather
    than deleting a module attribute that was never stored in this namespace.
    """
    def _set_dependency(self, name, value):
        owner = _engine()
        setattr(owner, name, value)
        if name == "session" and value is not None:
            value._engine = owner


for _dependency in _DEPENDENCIES:
    setattr(_EngineCompatibilityModule, _dependency, property(
        lambda self, name=_dependency: getattr(_engine(), name),
        lambda self, value, name=_dependency: self._set_dependency(name, value)))


sys.modules[__name__].__class__ = _EngineCompatibilityModule

# Explicit compatibility exports used by evaluation/legacy callers.
__all__ = ['Engine', 'EngineSession', 'load_config', 'initialize', 'observe_project',
           'generate_activity', 'reload_bank', 'content_maintenance', 'invalidate_content',
           'list_knowledge_points', 'process_activity', 'next_quiz', 'open_config_window',
           'create_quiz', 'submit_answer', 'history_time', 'get_answer_history',
           'get_answer_detail', 'repractice_attempt', 'get_learning_stats']
