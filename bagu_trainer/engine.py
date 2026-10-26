"""Desktop learning engine: matching, quiz lifecycle, grading and persistence.

This module has no transport registration or independent background loop.
DesktopMonitor owns change detection and DesktopApp owns interaction.
"""
import collections
import copy
import hashlib
import json
import ntpath
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import secrets, config
from .config import PROJECT_ROOT, load_config
from .diff_reader import DiffReader
from .knowledge_base import KnowledgeBase
from .matcher import Matcher
from .quiz_generator import QuizGenerator, get_verified_fallback
from .quiz_validation import has_passed_review
from .scheduler import next_state
from .scorer import Scorer
from .session_reader import entries_to_text, find_trae_home, read_recent_entries
from .store import Store

cfg = load_config()
kb = KnowledgeBase()
matcher = Matcher(kb)
generator = QuizGenerator(cfg)
scorer = Scorer()

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


def _normalize_project_root(project_root) -> str:
    """项目作用域键：先 realpath 消除相对路径/遍历/符号链接，再 normcase
    统一 Windows 大小写；所有 ready/pending 作用域比较必须使用该键。"""
    raw = str(project_root or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.realpath(raw))
    except (OSError, ValueError):
        return ""


def _db_path() -> str:
    """SQLite 路径：config db_path（相对用户数据目录）或默认 data/trainer.db。"""
    return str(config.db_path((load_config().get("db_path") or "").strip()))


def _project_roots():
    """Use the acknowledged desktop scope; a config write alone cannot apply it."""
    roots = session._watch_roots
    return list(roots) if roots is not None else load_config().get("project_roots", [str(PROJECT_ROOT)])


def reload_bank():
    """Use the newly activated bank without replacing pending/history state."""
    global kb, matcher
    updated = KnowledgeBase()
    updated_matcher = Matcher(updated)
    with content_maintenance():
        kb, matcher = updated, updated_matcher
        session._content_changed()


@contextmanager
def content_maintenance():
    """File replacement/deletion must serialize with this live learning session."""
    with session.maintenance():
        session._ensure_open()
        yield


def initialize():
    """Start a fresh session after terminal shutdown; DB hydration stays lazy."""
    global session
    if session._closed:
        session = EngineSession()


def observe_project(root, *, reader_factory=None):
    """Collect one scoped/redacted signal, optionally establishing a baseline."""
    owner = session
    with owner._lock:
        owner._ensure_open()
    with owner._readers_lock:
        if reader_factory is not None:
            owner._diff_readers[_normalize_project_root(root)] = reader_factory(root)
    return _collect_context("", root)


def generate_activity(root, info, *, retry_failed_ids=(), revision=None, stop=None):
    """Reserve under the session lock; never hold it across a model request."""
    return _generate_matches(
        info["context"], root, cfg["max_quizzes_per_hit"], _caps(),
        " / ".join(info.get("sources") or [])[:200], retry_failed_ids=retry_failed_ids,
        revision=revision, stop=stop)


def invalidate_content():
    """Invalidate delivery and in-flight content before maintenance replaces files."""
    with session.maintenance():
        session.invalidate_content()
        from .concepts import _load_concept_profiles
        from .fallback_bank import _load_fallback_cached
        from .matcher import _tokenize_cached, _domain_profile
        _load_concept_profiles.cache_clear()
        _load_fallback_cached.cache_clear()
        _tokenize_cached.cache_clear()
        _domain_profile.cache_clear()


def _open_config_window() -> bool:
    """以独立进程弹出 AI 接口配置窗口（tkinter 在独立进程运行）"""
    try:
        from .paths import command
        subprocess.Popen(
            command("--configure"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except OSError as e:
        print(f"[config] 弹出配置窗口失败（{type(e).__name__}）", file=sys.stderr)
        return False


def _startup_log():
    """Print local engine status without transport setup instructions or secrets."""
    print(f"[bagu-trainer] 桌面学习引擎：{len(kb.all())} 个知识点", file=sys.stderr)
    print("[bagu-trainer] 所有新题须由 AI 生成；离线选择题回退已禁用", file=sys.stderr)
    print(f"[bagu-trainer] 学习数据: {_db_path()}", file=sys.stderr)


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


def _generation_failure(quiz: dict, *, require_review=False):
    """Enforce the AI-only display policy for fresh and persisted questions."""
    if quiz.get("available") is False:
        return quiz.get("unavailable_reason") or "题目生成失败"
    if quiz.get("generated_by") != "llm":
        return "仅允许展示 AI 生成的题目；离线选择题及来源不明的旧题不再展示。"
    if require_review and not has_passed_review(quiz):
        return "新题缺少通过的上下文关联性审核记录，请重试生成。"
    return None


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
    c = load_config()
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
    owner = session
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
    c = load_config()
    return {
        "ttl": int(c["quiz_ttl_seconds"]),
        "max_pending": int(c["max_pending_quizzes"]),
        "max_quizzes": int(c["max_quizzes"]),
        "seen_cap": int(c["seen_hash_capacity"]),
    }


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
    """Reserve scoped work once; model I/O is outside all session/store locks."""
    project_root = _normalize_project_root(project_root)
    owner = session
    with owner._lock:
        if revision is not None and not owner._accepts_result(revision):
            return [], [], 0, []
        owner._ensure_open()
        bank = kb
        revision = revision or owner._generation_token()
        ranked = copy.deepcopy(matcher.rank(ctx, top_n=top_n))
        model = generator
    matches, generated, skipped, unavailable = [], [], 0, []
    for r in ranked:
        if stop is not None and stop.is_set():
            break
        q, score, hits = r["question"], r["score"], r["hits"]
        if retry_failed_ids and q["id"] not in retry_failed_ids:
            continue
        matches.append({"question": q, "score": score, "hits": hits,
                        "confidence": r["confidence"], "margin": r["margin"]})
        key = _match_hash(project_root, q["id"], ctx)
        slot = (project_root, q["id"])
        task = object()
        with owner._lock:
            if session is not owner or kb is not bank or not owner._accepts_result(revision):
                break
            owner._cleanup_locked(caps)
            store = owner._get_store_locked()
            scopes = {_normalize_project_root(root) for root in
                      _project_roots()}
            if project_root not in scopes or getattr(kb, "explicit_empty", False):
                skipped += 1
                continue
            fresh = owner._mark_seen_locked(key, caps["seen_cap"])
            st = store.concept_state(q["id"])
            if (slot in owner._generations or
                    (not fresh and q["id"] not in retry_failed_ids) or
                    q["id"] in owner._pending_kp_ids_locked(project_root) or
                    (st and (st.get("next_review_at") or 0) > _now())):
                skipped += 1
                continue
            owner._generations[slot] = task
        try:
            try:
                quiz = model.generate(q, ctx)
                quiz["sources"] = q.get("sources", [])
            except Exception as error:
                quiz = {"available": False, "failure_kind": "service",
                        "unavailable_reason": f"出题失败（{type(error).__name__}），请检查模型配置后重试。"}
            with owner._lock:
                if (session is not owner or kb is not bank or not owner._accepts_result(revision)
                        or owner._generations.get(slot) is not task):
                    skipped += 1
                    continue
                scopes = {_normalize_project_root(root) for root in
                          _project_roots()}
                if project_root not in scopes or getattr(kb, "explicit_empty", False):
                    skipped += 1
                    continue
                owner._cleanup_locked(caps)
                store = owner._get_store_locked()
                st = store.concept_state(q["id"])
                if (q["id"] in owner._pending_kp_ids_locked(project_root) or
                        (st and (st.get("next_review_at") or 0) > _now())):
                    skipped += 1
                    continue
                failure = _generation_failure(quiz, require_review=True)
                if failure:
                    unavailable.append({"kp_id": q["id"], "knowledge_point": q["knowledge_point"],
                                        "reason": failure,
                                        "failure_kind": "quality" if quiz.get("failure_kind") == "quality" else "service"})
                    continue
                now = _now()
                status = "delivered" if deliver_generated else "ready"
                delivered_at = now if deliver_generated else None
                quiz.update({"_created_at": now, "_expires_at": now + caps["ttl"],
                             "_status": status})
                quiz_id = uuid.uuid4().hex[:8]
                store.create_quiz(
                    quiz_id, q["id"], _quiz_snapshot(quiz), status,
                    str(project_root), f"{source_summary} | kp={q['id']}"[:200],
                    now, now + caps["ttl"], delivered_at=delivered_at)
                owner._state["quizzes"][quiz_id] = quiz
                public = {"hits": list(hits), "score": score,
                          "confidence": r["confidence"], "margin": r["margin"],
                          **_public_quiz(quiz_id, quiz, project_root)}
                owner._state["pending"].append({
                    "quiz_id": quiz_id,
                    "kp_id": q["id"],
                    "project_root": str(project_root),
                    "status": status,
                    "created_at": now,
                    "delivered_at": delivered_at,
                    "answered_at": None,
                    "expires_at": now + caps["ttl"],
                    **public,
                })
                generated.append(public)
                owner._cleanup_locked(caps)
        finally:
            with owner._lock:
                if owner._generations.get(slot) is task:
                    owner._generations.pop(slot, None)
    return matches, generated, skipped, unavailable


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
        revision = session._generation_token()
        info = _collect_context(description, project_root)
        if info.get("error"):
            session._cleanup_locked(caps)
            return {"ok": False, "error": info["error"], "info": info,
                    "generated": [], "matches": [], "skipped": 0,
                    "unavailable": []}
        if not info["context"].strip():
            session._cleanup_locked(caps)
            return {"info": info, "generated": [], "matches": [],
                    "skipped": 0, "unavailable": []}
    matches, generated, skipped, unavailable = _generate_matches(
        info["context"], str(info["project_root"]), cfg["max_quizzes_per_hit"], caps,
        " / ".join(info.get("sources") or [])[:200], revision=revision,
        deliver_generated=deliver_generated)
    with content_maintenance():
        session._cleanup_locked(caps)
    return {"info": info, "generated": generated, "matches": matches,
            "skipped": skipped, "unavailable": unavailable}


def _poll_once():
    """后台监测：执行一次共享刷新流程（只生成 ready 题，不交付用户）"""
    _refresh("", "")


def list_knowledge_points(tag: str = "") -> dict:
    """列出题库知识点（可按 tag 过滤，如 http/cache/并发）"""
    kps = kb.list_knowledge_points()
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
        session._cleanup_locked(caps)
        # 0) 已有 ready 题直接交付（后台预生成复用）
        ready = session._first_ready_locked(scope_root)
        if ready:
            session._deliver_pending_locked(ready)
            return {"ok": True, "has_pending": True, "quiz": _public_pending(ready),
                    "project_root": scope_root, "scope_note": _PROJECT_SCOPE_NOTE,
                    "next": "把题目与选项呈现给用户作答，然后用 submit_answer(quiz_id, 用户答案) 判分并转述解析"}
        # 采集一次上下文（供相关性判断与新题生成复用，避免二次采集导致 watch 快照失真）
        info = _collect_context("", str(root_path))
        ctx = "" if info.get("error") else (info.get("context") or "").strip()
        matched_ids = None
        if ctx:
            matched_ids = [r["id"] for r in matcher.rank(ctx, top_n=5)]
            # 1) 与当前上下文相关的到期复习题优先（错题/到期知识点重练）
            review = session._deliver_due_review_locked(
                str(root_path), prefer=matched_ids, only_prefer=True, caps=caps)
            if review:
                return {"ok": True, "has_pending": True, "quiz": review,
                        "project_root": scope_root, "scope_note": _PROJECT_SCOPE_NOTE,
                        "next": "把题目与选项呈现给用户作答，然后用 submit_answer(quiz_id, 用户答案) 判分并转述解析"}
        revision = session._generation_token()
    if ctx:
        # 2) 当前上下文匹配到的新知识点（同步生成，受锁保护不并发重复生成）
        matches, generated, skipped, unavailable = _generate_matches(
            info["context"], str(info["project_root"]),
            cfg["max_quizzes_per_hit"], caps,
            " / ".join(info.get("sources") or [])[:200], revision=revision)
    else:
        matches, generated, skipped, unavailable = [], [], 0, []
    with content_maintenance():
        session._cleanup_locked(caps)
        ready = session._first_ready_locked(scope_root)
        if ready:
            session._deliver_pending_locked(ready)
            return {"ok": True, "has_pending": True, "quiz": _public_pending(ready),
                    "project_root": scope_root, "scope_note": _PROJECT_SCOPE_NOTE,
                    "next": "把题目与选项呈现给用户作答，然后用 submit_answer(quiz_id, 用户答案) 判分并转述解析"}
        # 3) 上下文无关的到期复习兜底：保证错题总会在更短周期内再次出现
        review = session._deliver_due_review_locked(
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
    revision = session._generation_token()
    q = kb.get(knowledge_point_id) or get_verified_fallback(knowledge_point_id)
    if not q:
        return {"ok": False, "note": f"知识点 {knowledge_point_id} 不存在，可先 list_knowledge_points"}
    if context:
        c = load_config()
        secret_key = secrets.resolve_api_key(c.get("llm"))
        context = secrets.redact_text(
            str(context), [secret_key] if secret_key else [])[:2000]
    else:
        context = _collect_context("", "")["context"]
    quiz = generator.generate(q, context)
    quiz["sources"] = q.get("sources", [])
    failure = _generation_failure(quiz, require_review=True)
    if failure:
        return {"ok": False, "knowledge_point_id": knowledge_point_id,
                "reason": failure,
                "note": "不提供无法作答的题目。可在主窗口配置出题模型后重试"}
    quiz_id = uuid.uuid4().hex[:8]
    with session._lock:
        if not session._accepts_result(revision):
            return {"ok": False, "note": "题库内容已变化，已丢弃过期出题结果。"}
        caps = _caps()
        now = _now()
        quiz.update({"_created_at": now, "_expires_at": now + caps["ttl"],
                     "_status": "delivered"})
        session._state["quizzes"][quiz_id] = quiz
        session._get_store_locked().create_quiz(
            quiz_id, quiz.get("kp_id") or knowledge_point_id,
            _quiz_snapshot(quiz), "delivered", str(PROJECT_ROOT),
            f"手动 create_quiz | kp={knowledge_point_id}",
            now, now + caps["ttl"], delivered_at=now)
        session._cleanup_locked(caps)
    return {"ok": True, **_public_quiz(quiz_id, quiz, str(PROJECT_ROOT)),
            "next": "用 submit_answer(quiz_id, 用户答案) 判分并获取解析"}


def submit_answer(quiz_id: str, answer: str) -> dict:
    """提交答案，返回对错 + 解析 + 复习安排（schedule）。
    学习闭环：单事务落库 attempt 与 concept_state（掌握度/下次复习时间由
    SM-2 变体确定性计算），quiz 标记 answered。
    幂等性：同一 quiz 只允许提交一次，重复提交明确拒绝（不重复计数）。"""
    with content_maintenance():
        now = _now()
        quiz = session._state["quizzes"].get(quiz_id)
        if not quiz:
            store = session._get_store_locked()  # 懒建库时会先水合 DB 中未完成的题
            quiz = session._state["quizzes"].get(quiz_id)
            if not quiz:
                row = store.get_quiz(quiz_id)
                if row and row["status"] == "answered":
                    return {"ok": False, "note": f"quiz_id {quiz_id} 已作答，重复提交已拒绝（不重复计数）"}
                return {"ok": False, "note": f"quiz_id {quiz_id} 不存在或已过期"}
        if quiz.get("_status") == "answered":
            return {"ok": False,
                    "note": f"quiz_id {quiz_id} 已作答，重复提交已拒绝（不重复计数）"}
        failure = _generation_failure(quiz, require_review=not quiz.get("review"))
        if failure:
            session._cleanup_locked(_caps())
            return {"ok": False, "note": failure}
        if (quiz.get("_expires_at") or 0) <= now:
            session._get_store_locked().set_status(quiz_id, "expired")
            session._state["quizzes"].pop(quiz_id, None)
            session._state["pending"] = [p for p in session._state["pending"]
                                 if p["quiz_id"] != quiz_id]
            return {"ok": False, "note": f"quiz_id {quiz_id} 不存在或已过期"}
        grade = scorer.grade(quiz, answer)
        store = session._get_store_locked()
        prev = store.concept_state(quiz.get("kp_id"))
        state = next_state(grade["correct"], prev, now)
        # 单事务：attempt + concept_state + quiz 状态
        recorded = store.record_attempt(
            quiz_id, quiz.get("kp_id") or "", grade["your_answer"],
            grade["normalized_answer"], grade["correct"], now, state, once=True)
        if recorded is False:
            quiz["_status"] = "answered"
            return {"ok": False, "note": "该题已作答，重复提交已拒绝（不重复计数）"}
        for p in session._state["pending"]:
            if p["quiz_id"] == quiz_id:
                p["status"] = "answered"
                p["answered_at"] = now
        quiz["_status"] = "answered"
    return {
        "ok": True,
        **grade,
        "schedule": {
            "next_review_at": state["next_review_at"],
            "interval_days": state["interval_days"],
            "mastery": state["mastery"],
        },
    }


def _history_path():
    return session.database_path


def history_time(timestamp):
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except (ValueError, TypeError, OverflowError, OSError):
        return "旧记录未提供有效时间"


def get_answer_history(**filters):
    """Read-only, deliberately independent of session._lock and current bank."""
    try:
        result = Store.history_page(_history_path(), **filters)
        for item in result["items"]:
            item["time_text"] = history_time(item["answered_at"])
            item["project_name"] = ntpath.basename(item["project_root"].rstrip("/\\")) or "未记录项目"
            item["result_text"] = "答对" if item["correct"] else "答错"
        return {"ok": True, **result}
    except Exception as error:
        return {"ok": False, "note": f"查询失败（{type(error).__name__}），请重试。"}


def _repractice_failure(snapshot):
    failure = _generation_failure(snapshot)  # same admission policy as due review
    if failure:
        return failure
    if not isinstance(snapshot.get("question"), str) or not snapshot["question"].strip():
        return "旧快照缺少完整题干，无法重练。"
    options = snapshot.get("options")
    if not isinstance(options, dict) or any(not isinstance(options.get(k), str) or not options[k].strip() for k in "ABCD"):
        return "旧快照缺少完整的四个选项，无法重练。"
    if snapshot.get("answer") not in tuple("ABCD"):
        return "旧快照缺少有效的正确答案，无法重练。"
    if not isinstance(snapshot.get("analysis"), str) or not snapshot["analysis"].strip():
        return "旧快照缺少解析，无法重练。"
    if QuizGenerator._parse_quiz(json.dumps(snapshot, ensure_ascii=False)) is None:
        return "旧快照未通过现有题目校验（题干长度、四个不同选项或答案格式），无法重练。"
    return None


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
    """Clone a historical AI snapshot, never collect context or consult the bank.

    One unfinished retry per original attempt also deduplicates separate UI
    requests and survives restarts. Persist before publishing in-memory state.
    """
    with content_maintenance():
        if getattr(kb, "explicit_empty", False):
            return {"ok": False, "note": "当前无题库，请重新下载并构建来源后练习；历史快照仍可查看。"}
        detail = get_answer_detail(attempt_id)
        if not detail.get("ok") or not detail.get("can_retry"):
            return {"ok": False, "note": detail.get("retry_reason") or detail.get("note")}
        store = session._get_store_locked()
        now, caps = _now(), _caps()
        row = store.unfinished_repractice(int(attempt_id), now)
        if row is None:
            quiz = dict(detail["snapshot"])
            quiz.update(review=True, manual_review=True, retry_of_attempt_id=int(attempt_id),
                        retry_of_quiz_id=detail["quiz_id"])
            quiz["kp_id"] = detail.get("kp_id") or quiz.get("kp_id") or ""
            qid = uuid.uuid4().hex
            store.create_quiz(qid, quiz["kp_id"], quiz, "delivered", detail.get("project_root"),
                              detail.get("source_summary"), now, now + caps["ttl"], delivered_at=now)
            row = store.get_quiz(qid)
        qid, quiz = row["quiz_id"], dict(row["snapshot"])
        quiz.update(_status=row["status"], _created_at=row["created_at"], _expires_at=row["expires_at"])
        session._state["quizzes"][qid] = quiz
        if not any(p["quiz_id"] == qid for p in session._state["pending"]):
            session._state["pending"].append({**row, **_public_quiz(qid, quiz, row["project_root"])})
        return {"ok": True, "quiz": {**_public_quiz(qid, quiz, row["project_root"]), "manual_review": True}}


def get_learning_stats() -> dict:
    """【学习统计·只读】返回学习闭环聚合数据：已作答题数、总作答次数、
    正确率、待复习数、薄弱知识点（掌握度低于 0.6 且至少作答一次）。
    不返回任何会话原文/代码内容，只有聚合数字与知识点名称。"""
    with content_maintenance():
        store = session._get_store_locked()
        s = store.stats(_now())
        weak = []
        for w in store.weak_kps(WEAK_LIMIT, WEAK_MASTERY):
            kp = kb.get(w["kp_id"])
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


class EngineSession:
    """Own delivery state and its SQLite lifetime, independent of pages/workers.

    Generation/grading still live in the engine module during this first batch.
    They share this session's reentrant lock; callers use pending operations and
    maintenance(), never manipulate a connection or a delivery record.
    """
    def __init__(self, *, database=None, clock=None):
        self._database = database
        self._clock = clock
        self._lock = threading.RLock()
        self._state = {"quizzes": {}, "pending": [],
                       "seen": collections.deque(), "seen_set": set()}
        self._store = None
        self._readers_lock = threading.RLock()
        self._diff_readers = {}
        self._generations = {}
        self._watch_roots = None
        self._revision = 0
        self._closed = False

    def _now(self):
        return self._clock() if self._clock is not None else _now()

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("学习会话已关闭")

    def maintenance(self):
        """Serialize bank/file maintenance with learning writes and generation."""
        return self._lock

    @property
    def database_path(self):
        return self._store.path if self._store is not None else (self._database or _db_path())

    def _generation_token(self):
        with self._lock:
            self._ensure_open()
            return self, self._revision

    def _accepts_result(self, token):
        with self._lock:
            return not self._closed and token == (self, self._revision)

    def _content_changed(self):
        """A bank switch keeps pending/history but retires old generation work."""
        with self._lock:
            self._ensure_open()
            self._revision += 1
            self._generations.clear()

    def _clear(self):
        self._revision += 1
        self._generations.clear()
        for value in self._state.values():
            value.clear()
        with self._readers_lock:
            self._diff_readers.clear()
        if self._store is not None:
            self._store.close()
            self._store = None

    def invalidate_content(self):
        """Release the DB before maintenance; next use rehydrates surviving rows."""
        with self._lock:
            self._ensure_open()
            self._clear()

    def close(self):
        """Idempotent terminal close; retain persisted pending/history on disk."""
        with self._lock:
            if not self._closed:
                self._clear()
                self._closed = True

    def forget_projects(self, roots):
        """Drop only removed watch baselines; preserve their learning history."""
        with self._lock:
            self._ensure_open()
            for root in roots:
                scope = _normalize_project_root(root)
                with self._readers_lock:
                    self._diff_readers.pop(scope, None)
                for slot in list(self._generations):
                    if slot[0] == scope:
                        self._generations.pop(slot, None)

    def set_watch_roots(self, roots):
        """Commit the active allowlist together with retirement of removed work."""
        with self._lock:
            self._ensure_open()
            previous = self._watch_roots or ()
            self.forget_projects(set(previous) - set(roots))
            self._watch_roots = tuple(roots)

    def _pending_kp_ids_locked(self, project_root: str) -> set:
        """当前项目未作答 pending 题涉及的知识点集合（项目内同 kp 冷却用）。

        学习历史另按知识点全局共享；这里只隔离尚未作答的 ready/delivered 冷却，
        避免项目 A 的未答题阻塞项目 B 的同知识点新题。"""
        scope = _normalize_project_root(project_root)
        return {p["kp_id"] for p in self._state["pending"]
                if p["status"] in ("ready", "delivered")
                and _normalize_project_root(p.get("project_root")) == scope}

    def _get_store_locked(self):
        """懒加载 SQLite 存储（调用方必须已持 self._lock）。
        首次创建后立即从库中水合未完成的题，实现重启后状态延续。"""
        self._ensure_open()
        if self._store is None:
            self._store = Store(self.database_path)
            try:
                self._hydrate_pending_locked(self._store)
            except Exception:
                # Do not leave a half-hydrated queue/connection after a failed open.
                self._clear()
                raise
        return self._store

    def _hydrate_pending_locked(self, store):
        """服务重启水合：库中未过期且未作答的题回到内存队列（幂等，不重复添加）"""
        now = self._now()
        for row in store.load_pending(now):
            qid = row["quiz_id"]
            if qid in self._state["quizzes"]:
                continue
            quiz = dict(row["snapshot"])
            if _generation_failure(quiz, require_review=not quiz.get("review")):
                # Keep the snapshot and all learning history; retire only delivery.
                store.set_status(qid, "expired")
                continue
            quiz.update({"_status": row["status"],
                         "_created_at": row["created_at"],
                         "_expires_at": row["expires_at"]})
            self._state["quizzes"][qid] = quiz
            self._state["pending"].append({
                "quiz_id": qid,
                "kp_id": row["kp_id"],
                "project_root": row["project_root"],
                "status": row["status"],
                "created_at": row["created_at"],
                "delivered_at": row["delivered_at"],
                "answered_at": row["answered_at"],
                "expires_at": row["expires_at"],
                **_public_quiz(qid, quiz, row["project_root"]),
            })

    def _mark_seen_locked(self, key: str, seen_cap: int) -> bool:
        """登记指纹；已存在返回 False（去重防重复生成），容量超限确定性淘汰最旧"""
        if key in self._state["seen_set"]:
            return False
        self._state["seen_set"].add(key)
        self._state["seen"].append(key)
        while len(self._state["seen"]) > seen_cap:
            self._state["seen_set"].discard(self._state["seen"].popleft())
        return True

    def _deliver_pending_locked(self, entry: dict, now=None):
        """原子交付单条 ready pending：内存状态、quiz 快照状态、SQLite 同步更新。
        调用方必须已持 self._lock；整批交付由 process_activity 在同一锁内完成。"""
        delivered_at = self._now() if now is None else now
        entry["status"] = "delivered"
        entry["delivered_at"] = delivered_at
        quiz = self._state["quizzes"].get(entry["quiz_id"])
        if quiz is not None:
            quiz["_status"] = "delivered"
        self._get_store_locked().set_status(
            entry["quiz_id"], "delivered", delivered_at=delivered_at)
        return entry

    def _deliver_generated_locked(self, generated: list):
        """把本次 report 生成的 ready 题在同一状态锁内直接交付。"""
        generated_ids = {g["quiz_id"] for g in generated}
        now = self._now()
        for p in self._state["pending"]:
            if p["quiz_id"] in generated_ids and p["status"] == "ready":
                self._deliver_pending_locked(p, now=now)

    def _cleanup_locked(self, caps: dict):
        """过期/超量题的确定性清理（调用方必须已持 self._lock）。
        过期状态写通数据库（expired 持久化）；容量淘汰仅清内存，数据库保留历史。"""
        now = self._now()
        store = self._get_store_locked()
        # 1) 到期未作答：pending -> expired（写通 DB），quiz 一并移除
        for p in self._state["pending"]:
            quiz = self._state["quizzes"].get(p["quiz_id"], {})
            if p["status"] in ("ready", "delivered") and (
                    p["expires_at"] <= now or
                    _generation_failure(quiz, require_review=not quiz.get("review"))):
                p["status"] = "expired"
                store.set_status(p["quiz_id"], "expired")
        for p in [x for x in self._state["pending"] if x["status"] == "expired"]:
            self._state["quizzes"].pop(p["quiz_id"], None)
        self._state["pending"] = [x for x in self._state["pending"] if x["status"] != "expired"]
        # 2) quizzes 中无 pending 记录的历史过期题一并扫除。
        #   DB 侧仅淘汰未作答的（过期=放弃作答）；已作答行永久保留 answered，
        #   作为复习快照来源与学习历史（复习可能安排在 TTL 之后到期）。
        for qid, q in list(self._state["quizzes"].items()):
            if q.get("_expires_at", float("inf")) <= now:
                self._state["quizzes"].pop(qid, None)
                if q.get("_status") != "answered":
                    store.set_status(qid, "expired")
        # 3) 已作答记录从 pending 移除（quiz 留待容量淘汰）
        self._state["pending"] = [x for x in self._state["pending"] if x["status"] != "answered"]
        # 4) pending 超量：按 created_at 最旧确定性淘汰（连带 quiz）
        automatic = [p for p in self._state["pending"] if not self._state["quizzes"].get(p["quiz_id"], {}).get("manual_review")]
        if len(automatic) > caps["max_pending"]:
            self._state["pending"].sort(key=lambda p: (p["created_at"], p["quiz_id"]))
            drop = sorted(automatic, key=lambda p: (p["created_at"], p["quiz_id"]))[:len(automatic) - caps["max_pending"]]
            for p in drop:
                self._state["quizzes"].pop(p["quiz_id"], None)
            drop_ids = {p["quiz_id"] for p in drop}
            self._state["pending"] = [p for p in self._state["pending"] if p["quiz_id"] not in drop_ids]
        # 5) quizzes 超量：按 _created_at 最旧确定性淘汰（连带 pending 引用）
        automatic_quizzes = [(qid, q) for qid, q in self._state["quizzes"].items() if not q.get("manual_review")]
        if len(automatic_quizzes) > caps["max_quizzes"]:
            ordered = sorted(automatic_quizzes,
                             key=lambda kv: (kv[1].get("_created_at", 0.0), kv[0]))
            drop_ids = {qid for qid, _ in
                        ordered[:len(automatic_quizzes) - caps["max_quizzes"]]}
            for qid in drop_ids:
                self._state["quizzes"].pop(qid, None)
            self._state["pending"] = [x for x in self._state["pending"]
                                 if x["quiz_id"] not in drop_ids]

    def _first_ready_locked(self, project_root: str):
        """取指定项目 oldest-first 的 ready 题（调用方必须已持 self._lock）。

        先 realpath + normcase 归一化 project_root，只返回作用域完全一致的题；
        其他项目预生成的 ready 题不得跨项目交付。"""
        scope = _normalize_project_root(project_root)
        for p in sorted(self._state["pending"], key=lambda x: (x["created_at"], x["quiz_id"])):
            if (p["status"] == "ready"
                    and _normalize_project_root(p.get("project_root")) == scope):
                return p
        return None

    def _deliver_due_review_locked(self, root: str, prefer, only_prefer: bool, caps: dict):
        """到期复习交付（调用方必须已持 self._lock）：按 concept_state.next_review_at
        到期顺序，重建该知识点最近一次作答题目的快照（确定性重练，不调用 LLM、
        不重复计费）。返回公开题目 dict 或 None。

        规则：当前项目未作答 pending 中的 kp 跳过（避免同一题重复推送）；
        其他项目的未作答 pending 不参与本项目的冷却。学习历史/复习计划按知识点
        全局共享，但这里只可从已作答的 AI 快照重建题目，不会交付旧离线题或搬运其他项目 pending。
        only_prefer=True 时只在 prefer 集合内出题（「上下文相关的到期复习」优先）；
        否则 prefer 内 kp 排前、其余兜底（保证错题总会回来）。
        """
        root = _normalize_project_root(root)
        store = self._get_store_locked()
        now = self._now()
        due = store.due_kps(now)
        if not due:
            return None
        pending_kps = self._pending_kp_ids_locked(root)
        prefer_set = set(prefer or [])
        cands = [d for d in due if d["kp_id"] not in pending_kps]
        if only_prefer:
            cands = [d for d in cands if d["kp_id"] in prefer_set]
            if not cands:
                return None
        else:
            cands.sort(key=lambda d: (0 if d["kp_id"] in prefer_set else 1,
                                      d["next_review_at"], d["kp_id"]))
        for row in cands:
            hist = store.latest_answered_snapshot(row["kp_id"])
            if not hist:
                continue  # 快照缺失（不应发生）时跳过该 kp，尝试下一个
            quiz = dict(hist["snapshot"])
            if _generation_failure(quiz):
                continue  # Retain attempts/schedules, but never replay offline snapshots.
            for field in ("manual_review", "retry_of_attempt_id", "retry_of_quiz_id"):
                quiz.pop(field, None)
            quiz["review"] = True
            quiz.update({"_created_at": now, "_expires_at": now + caps["ttl"],
                         "_status": "delivered"})
            quiz_id = uuid.uuid4().hex[:8]
            self._state["quizzes"][quiz_id] = quiz
            public = _public_quiz(quiz_id, quiz, root)
            self._state["pending"].append({
                "quiz_id": quiz_id,
                "kp_id": row["kp_id"],
                "project_root": root,
                "status": "delivered",
                "created_at": now,
                "delivered_at": now,
                "answered_at": None,
                "expires_at": now + caps["ttl"],
                **public,
            })
            store.create_quiz(
                quiz_id, row["kp_id"], _quiz_snapshot(quiz), "delivered", root,
                f"到期复习 | kp={row['kp_id']} | 上次作答 {hist['answered_at']}"[:200],
                now, now + caps["ttl"], delivered_at=now)
            return public
        return None

    def take_quiz(self):
        """Deliver the oldest ready quiz in the current configured scopes."""
        if getattr(kb, "explicit_empty", False):
            return None
        with self._lock:
            self._ensure_open()
            self._get_store_locked()
            self._cleanup_locked(_caps())
            roots = _project_roots()
            scopes = {_normalize_project_root(r) for r in roots}
            for pending in sorted(self._state["pending"], key=lambda p: p["created_at"]):
                if pending["status"] != "ready" or pending["project_root"] not in scopes:
                    continue
                self._deliver_pending_locked(pending)
                return _public_pending(pending)
        return None

    def list_pending(self):
        """Read-only pending list (待答列表): ready + delivered, unanswered, in
        project scope (or manual), oldest first.  Viewing the list must NOT mark
        anything delivered — it only reads the current state after cleanup.
        Monitoring/automatic-delivery pauses do not hide existing practice.
        Expiry and dedupe/cooldown rules are enforced by _cleanup_locked."""
        if getattr(kb, "explicit_empty", False):
            return []
        with self._lock:
            self._ensure_open()
            self._get_store_locked()
            self._cleanup_locked(_caps())
            roots = _project_roots()
            scopes = {_normalize_project_root(r) for r in roots}
            out = []
            for pending in sorted(self._state["pending"], key=lambda p: (p["created_at"], p["quiz_id"])):
                if pending["status"] not in ("ready", "delivered"):
                    continue
                manual = bool(self._state["quizzes"].get(pending["quiz_id"], {}).get("manual_review"))
                if pending["project_root"] not in scopes and not manual:
                    continue
                public = _public_pending(pending)
                public["status"] = pending["status"]
                public["manual_review"] = manual
                out.append(public)
            return out

    def enter_quiz(self, quiz_id):
        """按题进入：显式选中一条待答题交付作答。仅当用户主动选择时才把
        ready 题交付；返回公开题目，题不存在/过期/越权时返回 None。
        主动进入不受监听暂停或自动推送暂停限制；自动交付由 take_quiz 控制。"""
        if getattr(kb, "explicit_empty", False):
            return None
        with self._lock:
            self._ensure_open()
            self._get_store_locked()
            self._cleanup_locked(_caps())
            roots = _project_roots()
            scopes = {_normalize_project_root(r) for r in roots}
            for pending in sorted(self._state["pending"], key=lambda p: (p["created_at"], p["quiz_id"])):
                if pending["quiz_id"] != quiz_id or pending["status"] not in ("ready", "delivered"):
                    continue
                manual = bool(self._state["quizzes"].get(pending["quiz_id"], {}).get("manual_review"))
                if pending["project_root"] not in scopes and not manual:
                    continue
                if pending["status"] == "ready":
                    self._deliver_pending_locked(pending)
                public = _public_pending(pending)
                if manual:
                    public["manual_review"] = True
                return public
        return None

    def resume_quiz(self):
        """Explicit practice: resume pending work, then replay a due AI snapshot.

        Unlike automatic delivery, this remains available while monitoring or
        popups are paused. Never scan project context or generate a new quiz.
        """
        if getattr(kb, "explicit_empty", False):
            return None
        with self._lock:
            self._ensure_open()
            self._get_store_locked()
            caps = _caps()
            self._cleanup_locked(caps)
            roots = _project_roots()
            roots = list(dict.fromkeys(_normalize_project_root(r) for r in roots))
            scopes = set(roots)
            pending_items = sorted(self._state["pending"], key=lambda p: (p["created_at"], p["quiz_id"]))
            for status in ("delivered", "ready"):
                for pending in pending_items:
                    manual = self._state["quizzes"].get(pending["quiz_id"], {}).get("manual_review")
                    if pending["status"] != status or (pending["project_root"] not in scopes and not manual):
                        continue
                    if status == "ready":
                        self._deliver_pending_locked(pending)
                    return {**_public_pending(pending), **({"manual_review": True} if manual else {})}
            for root in roots:
                _, error = _resolve_project_root(root)
                if error:
                    continue
                review = self._deliver_due_review_locked(root, prefer=None, only_prefer=False, caps=caps)
                if review:
                    return review
        return None


session = EngineSession()
