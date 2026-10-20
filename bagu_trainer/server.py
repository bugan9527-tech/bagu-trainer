# -*- coding: utf-8 -*-
"""「AI 八股训练器」MCP Server（stdio）。

工具：
  list_knowledge_points   列出题库知识点
  report_ai_activity      报告/采集 AI 当前活动 -> 匹配知识点 -> 生成情景题
  generate_quiz           按知识点 ID + 上下文生成题目
  submit_answer           提交答案并判分、给解析

启动日志打到 stderr（stdout 是 MCP 协议通道）。可直接运行：
  python bagu_trainer/server.py
"""
import collections
import hashlib
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# MCP 协议要求 UTF-8；Windows 默认 GBK 写 stdout 会破坏传输，必须显式切换
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

# 直接执行本文件时，Python 会把 bagu_trainer/ 放到 sys.path[0]。
# 这会让第三方库的 `import secrets` 误命中项目的 secrets.py，导致
# starlette 无法导入标准库 token_hex。先移除脚本目录，再加入项目根目录。
_SCRIPT_DIR_RAW = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_DIR = os.path.normcase(_SCRIPT_DIR_RAW)
_PROJECT_DIR_RAW = os.path.dirname(_SCRIPT_DIR_RAW)
_PROJECT_DIR = os.path.normcase(_PROJECT_DIR_RAW)


def _same_path(left, right):
    try:
        return os.path.normcase(os.path.realpath(left)) == right
    except (OSError, ValueError):
        return False


sys.path[:] = [p for p in sys.path if not _same_path(p, _SCRIPT_DIR)]
if not any(_same_path(p, _PROJECT_DIR) for p in sys.path):
    sys.path.insert(0, _PROJECT_DIR_RAW)

from bagu_trainer import secrets
from bagu_trainer.config import CONFIG_PATH, PROJECT_ROOT, load_config, update_llm
from bagu_trainer.diff_reader import DiffReader
from bagu_trainer.desktop_bridge import desktop_running, forward_to_desktop
from bagu_trainer.knowledge_base import KnowledgeBase
from bagu_trainer.matcher import Matcher
from bagu_trainer.quiz_generator import (
    QuizGenerator,
    get_verified_fallback,
    test_llm,
    verified_fallback_count,
)
from bagu_trainer.scheduler import next_state
from bagu_trainer.scorer import Scorer
from bagu_trainer.session_reader import entries_to_text, find_trae_home, read_recent_entries
from bagu_trainer.store import Store

from mcp.server.fastmcp import FastMCP

if "--check-imports" in sys.argv:
    print("server imports ok")
    raise SystemExit(0)

mcp = FastMCP("bagu-trainer")

cfg = load_config()
kb = KnowledgeBase()
matcher = Matcher(kb)
generator = QuizGenerator(cfg)
scorer = Scorer()

# 服务进程内存态：
#   quizzes: quiz_id -> 完整 quiz（含 answer 与 _meta，作答前不外发）
#   pending: 待答题状态记录，状态机 ready/delivered/answered/expired
#   seen:    已出题指纹 (project_root, kp_id, 归一化上下文 hash)，带容量上限
# 作用域：ready/delivered 待答队列与未作答知识点冷却严格按 project_root 隔离；
# attempts/concept_state 等学习历史和到期复习计划按知识点全局共享。复习只从
# 已作答历史重建，绝不会把其他项目的未完成 pending 直接交付给当前项目。
_state = {
    "quizzes": {},
    "pending": [],
    "seen": collections.deque(),
    "seen_set": set(),
}
# SQLite 学习闭环存储（懒初始化：首个工具调用时按 config db_path 建库，
# 一并水合未完成/未复习的持久化状态，保证服务重启不丢失）
_store = None
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
    """SQLite 路径：config db_path（相对路径按项目根解析）或默认 data/trainer.db"""
    p = (load_config().get("db_path") or "").strip()
    if p:
        path = Path(p).expanduser()
        return str(path if path.is_absolute() else (PROJECT_ROOT / path))
    return str(PROJECT_ROOT / "data" / "trainer.db")
# 后台监测线程与工具调用共用同一把锁，串行化「采集->匹配->生成->清理」全流程，
# 避免并发重复生成/重复计费
_STATE_LOCK = threading.RLock()
# watch 模式监听器按项目根目录跨调用复用（快照不持久会永远检测不到变更）
_diff_readers = {}

CONFIG_WINDOW = str(PROJECT_ROOT / "scripts" / "config_window.py")


def _open_config_window() -> bool:
    """以独立进程弹出 AI 接口配置窗口（tkinter 需独立进程，不与 stdio 冲突）"""
    try:
        subprocess.Popen(
            [sys.executable, CONFIG_WINDOW],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except OSError as e:
        print(f"[config] 弹出配置窗口失败: {e}", file=sys.stderr)
        return False


def _llm_configured() -> bool:
    return bool(secrets.resolve_api_key(load_config().get("llm")))


def _startup_log():
    server_file = os.path.abspath(__file__)
    trae_home = find_trae_home(cfg.get("trae_home"))
    llm = cfg.get("llm")
    fallback_total = verified_fallback_count()
    lines = [
        "=" * 60,
        "[bagu-trainer] AI 八股训练器 MCP Server 启动",
        f"[bagu-trainer] 题库: {PROJECT_ROOT / 'data' / 'questions.json'} "
        f"({len(kb.all())} 题 / {len(kb.list_knowledge_points())} 个知识点)",
        f"[bagu-trainer] 已验证回退题: {fallback_total} 道"
        f"（当前离线可出 {fallback_total} 道带四选项、答案和解析的题）",
        f"[bagu-trainer] .trae-cn 会话目录: {trae_home if trae_home else '未检测到(可配置 config.json 的 trae_home)'}",
        f"[bagu-trainer] LLM: {'已配置 ' + llm['model'] if _llm_configured() and llm else f'未配置(合成出题不可用；当前离线可出 {fallback_total} 道已验证选择题，其余知识点将明确提示不可用；配置参照 config.example.json / KEY_MIGRATION.md)'}",
        f"[bagu-trainer] 配置: {CONFIG_PATH if CONFIG_PATH.exists() else 'config.json 不存在，使用默认值'}",
        f"[bagu-trainer] 学习数据(练习记录/掌握度/复习计划): {_db_path()} (SQLite, 重启不丢失)",
        "-" * 60,
        "[bagu-trainer] 在 TraeCode 中注册（TraeCode -> 设置 -> MCP -> 添加 MCP 服务器，类型选 stdio）：",
        f"  命令: python",
        f"  参数: {server_file}",
        "-" * 60,
    ]
    for line in lines:
        print(line, file=sys.stderr)


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
        "fallback_reason": quiz.get("fallback_reason"),
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


def _pending_kp_ids_locked(project_root: str) -> set:
    """当前项目未作答 pending 题涉及的知识点集合（项目内同 kp 冷却用）。

    学习历史另按知识点全局共享；这里只隔离尚未作答的 ready/delivered 冷却，
    避免项目 A 的未答题阻塞项目 B 的同知识点新题。"""
    scope = _normalize_project_root(project_root)
    return {p["kp_id"] for p in _state["pending"]
            if p["status"] in ("ready", "delivered")
            and _normalize_project_root(p.get("project_root")) == scope}


def _get_store_locked():
    """懒加载 SQLite 存储（调用方必须已持 _STATE_LOCK）。
    首次创建后立即从库中水合未完成的题，实现重启后状态延续。"""
    global _store
    if _store is None:
        _store = Store(_db_path())
        _hydrate_pending_locked(_store)
    return _store


def _hydrate_pending_locked(store):
    """服务重启水合：库中未过期且未作答的题回到内存队列（幂等，不重复添加）"""
    now = _now()
    for row in store.load_pending(now):
        qid = row["quiz_id"]
        if qid in _state["quizzes"]:
            continue
        quiz = dict(row["snapshot"])
        quiz.update({"_status": row["status"],
                     "_created_at": row["created_at"],
                     "_expires_at": row["expires_at"]})
        _state["quizzes"][qid] = quiz
        _state["pending"].append({
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


def _resolve_project_root(project_root: str):
    """project_root 白名单校验（隐私边界）。
    返回 (Path|None, error|None)：
      - realpath 归一化后再比较，杜绝路径穿越（..、大小写、符号链接）
      - 必须是 config project_roots 白名单中的目录或其子目录，否则拒绝
    """
    c = load_config()
    whitelist = c.get("project_roots") or [str(PROJECT_ROOT)]
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
    if key not in _diff_readers:
        _diff_readers[key] = DiffReader(key)
    changes = _diff_readers[key].changes()
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


def _mark_seen_locked(key: str, seen_cap: int) -> bool:
    """登记指纹；已存在返回 False（去重防重复生成），容量超限确定性淘汰最旧"""
    if key in _state["seen_set"]:
        return False
    _state["seen_set"].add(key)
    _state["seen"].append(key)
    while len(_state["seen"]) > seen_cap:
        _state["seen_set"].discard(_state["seen"].popleft())
    return True


def _public_pending(entry: dict) -> dict:
    """把 pending 记录还原为发往用户侧的题目（剔除内部状态字段）"""
    internal = {"status", "created_at", "delivered_at", "answered_at",
                "expires_at"}
    out = {k: v for k, v in entry.items() if k not in internal}
    if entry.get("project_root"):
        out["project_root"] = (_normalize_project_root(entry["project_root"])
                               or entry["project_root"])
    return out


def _generate_matches_locked(ctx: str, project_root: str, top_n: int, caps: dict,
                             source_summary: str = ""):
    """匹配知识点并生成题目（调用方必须已持 _STATE_LOCK）。
    返回 (matches, generated, skipped, unavailable)：matches 为匹配详情列表
    [{question, score, hits, confidence, margin}]；generated 为可作答的公开题目列表，
    skipped 为因相同上下文已出过题被去重跳过、或同一知识点冷却（未作答/未到复习期）
    而跳过的数量（不重复计费），unavailable 为生成失败（无 LLM/校验失败/来源缺失
    且无已验证回退）的明确原因列表。
    生成失败的题绝不进入 pending/对外输出（不允许出现无选项/无答案的题目）。
    生成成功的题同时落库（status=ready），保证重启后可继续作答。"""
    project_root = _normalize_project_root(project_root)
    ranked = matcher.rank(ctx, top_n=top_n)
    matches, generated, skipped, unavailable = [], [], 0, []
    now = _now()
    store = _get_store_locked()
    pending_kps = _pending_kp_ids_locked(project_root)
    for r in ranked:
        q, score, hits = r["question"], r["score"], r["hits"]
        matches.append({"question": q, "score": score, "hits": hits,
                        "confidence": r["confidence"], "margin": r["margin"]})
        key = _match_hash(project_root, q["id"], ctx)
        if not _mark_seen_locked(key, caps["seen_cap"]):
            skipped += 1
            continue
        # 同 kp 冷却：有未作答题，或未到复习期，都不重复出题（避免短时间重复）
        if q["id"] in pending_kps:
            skipped += 1
            continue
        st = store.concept_state(q["id"])
        if st and st.get("next_review_at") and st["next_review_at"] > now:
            skipped += 1
            continue
        quiz = generator.generate(q, ctx)
        if quiz.get("available") is False:
            unavailable.append({
                "kp_id": q["id"],
                "knowledge_point": q["knowledge_point"],
                "reason": quiz.get("unavailable_reason") or "未知生成失败原因",
            })
            continue
        quiz.update({"_created_at": now, "_expires_at": now + caps["ttl"],
                     "_status": "ready"})
        quiz_id = uuid.uuid4().hex[:8]
        _state["quizzes"][quiz_id] = quiz
        store.create_quiz(
            quiz_id, q["id"], _quiz_snapshot(quiz), "ready",
            str(project_root), f"{source_summary} | kp={q['id']}"[:200],
            now, now + caps["ttl"])
        public = {"hits": list(hits), "score": score,
                  "confidence": r["confidence"], "margin": r["margin"],
                  **_public_quiz(quiz_id, quiz, project_root)}
        _state["pending"].append({
            "quiz_id": quiz_id,
            "kp_id": q["id"],
            "project_root": str(project_root),
            "status": "ready",
            "created_at": now,
            "delivered_at": None,
            "answered_at": None,
            "expires_at": now + caps["ttl"],
            **public,
        })
        generated.append(public)
    return matches, generated, skipped, unavailable


def _deliver_pending_locked(entry: dict, now=None):
    """原子交付单条 ready pending：内存状态、quiz 快照状态、SQLite 同步更新。
    调用方必须已持 _STATE_LOCK；整批交付由 report_ai_activity 在同一锁内完成。"""
    delivered_at = _now() if now is None else now
    entry["status"] = "delivered"
    entry["delivered_at"] = delivered_at
    quiz = _state["quizzes"].get(entry["quiz_id"])
    if quiz is not None:
        quiz["_status"] = "delivered"
    _get_store_locked().set_status(
        entry["quiz_id"], "delivered", delivered_at=delivered_at)
    return entry


def _deliver_generated_locked(generated: list):
    """把本次 report 生成的 ready 题在同一状态锁内直接交付。"""
    generated_ids = {g["quiz_id"] for g in generated}
    now = _now()
    for p in _state["pending"]:
        if p["quiz_id"] in generated_ids and p["status"] == "ready":
            _deliver_pending_locked(p, now=now)


def _cleanup_locked(caps: dict):
    """过期/超量题的确定性清理（调用方必须已持 _STATE_LOCK）。
    过期状态写通数据库（expired 持久化）；容量淘汰仅清内存，数据库保留历史。"""
    now = _now()
    store = _get_store_locked()
    # 1) 到期未作答：pending -> expired（写通 DB），quiz 一并移除
    for p in _state["pending"]:
        if p["status"] in ("ready", "delivered") and p["expires_at"] <= now:
            p["status"] = "expired"
            store.set_status(p["quiz_id"], "expired")
    for p in [x for x in _state["pending"] if x["status"] == "expired"]:
        _state["quizzes"].pop(p["quiz_id"], None)
    _state["pending"] = [x for x in _state["pending"] if x["status"] != "expired"]
    # 2) quizzes 中无 pending 记录的历史过期题一并扫除。
    #   DB 侧仅淘汰未作答的（过期=放弃作答）；已作答行永久保留 answered，
    #   作为复习快照来源与学习历史（复习可能安排在 TTL 之后到期）。
    for qid, q in list(_state["quizzes"].items()):
        if q.get("_expires_at", float("inf")) <= now:
            _state["quizzes"].pop(qid, None)
            if q.get("_status") != "answered":
                store.set_status(qid, "expired")
    # 3) 已作答记录从 pending 移除（quiz 留待容量淘汰）
    _state["pending"] = [x for x in _state["pending"] if x["status"] != "answered"]
    # 4) pending 超量：按 created_at 最旧确定性淘汰（连带 quiz）
    if len(_state["pending"]) > caps["max_pending"]:
        _state["pending"].sort(key=lambda p: (p["created_at"], p["quiz_id"]))
        drop = _state["pending"][: len(_state["pending"]) - caps["max_pending"]]
        for p in drop:
            _state["quizzes"].pop(p["quiz_id"], None)
        _state["pending"] = _state["pending"][len(drop):]
    # 5) quizzes 超量：按 _created_at 最旧确定性淘汰（连带 pending 引用）
    if len(_state["quizzes"]) > caps["max_quizzes"]:
        ordered = sorted(_state["quizzes"].items(),
                         key=lambda kv: (kv[1].get("_created_at", 0.0), kv[0]))
        drop_ids = {qid for qid, _ in
                    ordered[: len(_state["quizzes"]) - caps["max_quizzes"]]}
        for qid in drop_ids:
            _state["quizzes"].pop(qid, None)
        _state["pending"] = [x for x in _state["pending"]
                             if x["quiz_id"] not in drop_ids]


def _refresh(project_root: str, description: str = "",
             deliver_generated: bool = False) -> dict:
    """后台监测与工具调用共用的受锁刷新流程：
    采集上下文 -> 匹配 -> 去重生成 -> 清理。
    返回 info/generated/matches/skipped/unavailable；白名单校验失败时
    返回 ok=False + error（不进行任何采集与生成）。deliver_generated=True
    供 report_ai_activity 使用：生成与 ready->delivered 状态更新在同一锁内完成。
    """
    caps = _caps()
    with _STATE_LOCK:
        info = _collect_context(description, project_root)
        if info.get("error"):
            _cleanup_locked(caps)
            return {"ok": False, "error": info["error"], "info": info,
                    "generated": [], "matches": [], "skipped": 0,
                    "unavailable": []}
        if not info["context"].strip():
            _cleanup_locked(caps)
            return {"info": info, "generated": [], "matches": [],
                    "skipped": 0, "unavailable": []}
        source_summary = " / ".join(info.get("sources") or [])[:200]
        matches, generated, skipped, unavailable = _generate_matches_locked(
            info["context"], str(info["project_root"]),
            cfg["max_quizzes_per_hit"], caps, source_summary)
        if deliver_generated and generated:
            _deliver_generated_locked(generated)
        _cleanup_locked(caps)
    return {"info": info, "generated": generated, "matches": matches,
            "skipped": skipped, "unavailable": unavailable}


def _first_ready_locked(project_root: str):
    """取指定项目 oldest-first 的 ready 题（调用方必须已持 _STATE_LOCK）。

    先 realpath + normcase 归一化 project_root，只返回作用域完全一致的题；
    其他项目预生成的 ready 题不得跨项目交付。"""
    scope = _normalize_project_root(project_root)
    for p in sorted(_state["pending"], key=lambda x: (x["created_at"], x["quiz_id"])):
        if (p["status"] == "ready"
                and _normalize_project_root(p.get("project_root")) == scope):
            return p
    return None


def _deliver_due_review_locked(root: str, prefer, only_prefer: bool, caps: dict):
    """到期复习交付（调用方必须已持 _STATE_LOCK）：按 concept_state.next_review_at
    到期顺序，重建该知识点最近一次作答题目的快照（确定性重练，不调用 LLM、
    不重复计费）。返回公开题目 dict 或 None。

    规则：当前项目未作答 pending 中的 kp 跳过（避免同一题重复推送）；
    其他项目的未作答 pending 不参与本项目的冷却。学习历史/复习计划按知识点
    全局共享，但这里只可从已作答快照重建题目，不会直接搬运其他项目 pending。
    only_prefer=True 时只在 prefer 集合内出题（「上下文相关的到期复习」优先）；
    否则 prefer 内 kp 排前、其余兜底（保证错题总会回来）。
    """
    root = _normalize_project_root(root)
    store = _get_store_locked()
    now = _now()
    due = store.due_kps(now)
    if not due:
        return None
    pending_kps = _pending_kp_ids_locked(root)
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
        quiz["review"] = True
        quiz.update({"_created_at": now, "_expires_at": now + caps["ttl"],
                     "_status": "delivered"})
        quiz_id = uuid.uuid4().hex[:8]
        _state["quizzes"][quiz_id] = quiz
        public = _public_quiz(quiz_id, quiz, root)
        _state["pending"].append({
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


def _poll_once():
    """后台监测：执行一次共享刷新流程（只生成 ready 题，不交付用户）"""
    if desktop_running():
        return
    _refresh("", "")


def _monitor_loop(interval: int = 30):
    """daemon 线程：定期轮询信号源，「AI 干活→自动出题」的触发引擎"""
    while True:
        try:
            _poll_once()
        except Exception as e:
            print(f"[monitor] 轮询异常: {e.__class__.__name__}: {e}", file=sys.stderr)
        time.sleep(interval)


@mcp.tool()
@forward_to_desktop
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
            "verified_fallback_count": verified_fallback_count(),
            "hint": "用 generate_quiz(knowledge_point_id, context) 针对某知识点出题"}


@mcp.tool()
@forward_to_desktop
def report_ai_activity(description: str = "", project_root: str = "") -> dict:
    """【手动报告接口】显式报告一次 AI 活动并立即出题（自动监测由后台完成，
    日常应优先使用 check_pending_quiz）。可带 description 描述刚做的事，
    缺省时自动采集会话记录+文件变动；命中知识点则生成情景题(不含答案)。
    待答队列与未作答冷却按 project_root 隔离；作答历史/复习计划全局共享，
    但本接口只交付本次 project_root 下新生成的题，不会搬运其他项目 pending。
    相同 (项目, 知识点, 上下文) 只出一次题，重复报告不会重复生成。
    生成失败时 quizzes 为空，并在 unavailable 字段给出明确原因（绝不返回
    无选项/无答案的题目）。用户作答后调用 submit_answer(quiz_id, answer) 判分。
    隐私保证：project_root 须在 config.json 白名单内；上下文已自动脱敏
    （密钥/令牌不会出现在任何返回字段中）。
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
                          "可调用 open_config_window 配置 LLM 后重试")
    if res["skipped"]:
        result["note"] = f"相同上下文已出过题，跳过 {res['skipped']} 道重复题目（防重复计费）"
    return result


@mcp.tool()
@forward_to_desktop
def check_pending_quiz(project_root: str = "") -> dict:
    """【自动出题检查】检查训练器待答测试题；若无待答，立即同步采集当前上下文
    匹配并生成一次（不再等待 30 秒后台轮询，规避时序竞态）。
    自动调用时机、排除条件、去重和作答交互以当前项目的唯一规则源
    `.trae/rules/quiz-check.md` 为准；本工具只执行一次检查，不应在一轮任务中
    轮询或重复调用。参数 project_root 可选：指定采集文件变动的项目目录，
    必须位于 config.json project_roots 白名单内；白名单外/不存在/路径穿越返回
    明确错误（隐私保护，绝不采集白名单外目录）。缺省用 config.json 配置的默认项目。
    若返回待答题目：把题干和选项完整呈现给用户，等待用户作答后调用
    submit_answer(quiz_id, 用户答案) 判分并转述解析；同一题只推送一次，
    已推送未作答的题仍可通过 submit_answer 正常作答，不会重复推送。
    若返回无待答题目：说明本次活动未命中题库，无需向用户提及本工具。
    作用域：ready 队列和未作答知识点冷却按归一化 project_root 隔离；作答记录、
    掌握度和复习计划全局共享。复习题只从已作答历史重建，不会把其他项目尚未
    作答的 pending 题干推送到当前项目。
    """
    root_path, err = _resolve_project_root((project_root or "").strip())
    if err:
        return {"ok": False, "has_pending": False, "error": err,
                "note": "project_root 请传入 project_roots 白名单内的目录，或留空使用 config.json 的默认项目目录"}
    scope_root = str(root_path)
    with _STATE_LOCK:
        caps = _caps()
        _cleanup_locked(caps)
        # 0) 已有 ready 题直接交付（后台预生成复用）
        ready = _first_ready_locked(scope_root)
        if ready:
            _deliver_pending_locked(ready)
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
            review = _deliver_due_review_locked(
                str(root_path), prefer=matched_ids, only_prefer=True, caps=caps)
            if review:
                return {"ok": True, "has_pending": True, "quiz": review,
                        "project_root": scope_root, "scope_note": _PROJECT_SCOPE_NOTE,
                        "next": "把题目与选项呈现给用户作答，然后用 submit_answer(quiz_id, 用户答案) 判分并转述解析"}
            # 2) 当前上下文匹配到的新知识点（同步生成，受锁保护不并发重复生成）
            matches, generated, skipped, unavailable = _generate_matches_locked(
                info["context"], str(info["project_root"]),
                cfg["max_quizzes_per_hit"], caps,
                " / ".join(info.get("sources") or [])[:200])
            _cleanup_locked(caps)
            ready = _first_ready_locked(scope_root)
            if ready:
                _deliver_pending_locked(ready)
                return {"ok": True, "has_pending": True, "quiz": _public_pending(ready),
                        "project_root": scope_root, "scope_note": _PROJECT_SCOPE_NOTE,
                        "next": "把题目与选项呈现给用户作答，然后用 submit_answer(quiz_id, 用户答案) 判分并转述解析"}
        else:
            matches, generated, skipped, unavailable = [], [], 0, []
        # 3) 上下文无关的到期复习兜底：保证错题总会在更短周期内再次出现
        review = _deliver_due_review_locked(
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


@mcp.tool()
@forward_to_desktop
def open_config_window() -> dict:
    """弹出本地「AI 接口配置」图形窗口，让用户填写 base_url/API Key/模型。
    窗口内可点「测试连接」验证 key，保存后即时生效。适合用户想要图形界面的场景。
    """
    if _open_config_window():
        return {"ok": True, "note": "配置窗口已打开：填好接口地址与 API Key，点「测试连接」验证，再点「保存」。保存后即时生效，无需重启。"}
    return {"ok": False, "note": "窗口打开失败。可在会话内改用 configure_llm 直接配置"}


@mcp.tool()
@forward_to_desktop
def configure_llm(base_url: str = "", api_key: str = "", model: str = "", timeout_sec: int = 0) -> dict:
    """【配置 LLM 接口】用户说「配置 AI 接口/配置 LLM/接入 API」之类的话时调用本工具：
    从用户消息中提取 base_url 与 api_key（用户只给 key 时,base_url 默认
    https://api.deepseek.com/v1、model 默认 deepseek-chat）。先发一个最小请求验证
    key 有效性，验证通过才保存并即时生效；验证失败返回具体原因供转述，不保存。
    隐私保证：api_key 只写入系统凭据管理器（keyring），config.json 绝不保存明文，
    返回值也不回显 Key；也可改设环境变量 BAGU_LLM_API_KEY。
    用户想清空配置(回退题库原题模式)时传空 api_key。
    """
    url = (base_url or "https://api.deepseek.com/v1").strip()
    key = (api_key or "").strip()
    name = (model or "deepseek-chat").strip()
    timeout = timeout_sec or 60
    if not key:
        update_llm({"base_url": url, "api_key": "", "model": name, "timeout_sec": timeout},
                   clear_api_key=True)
        fallback_total = verified_fallback_count()
        return {"ok": True, "configured": False,
                "note": f"已清空 api_key：合成出题停用；当前离线可出 {fallback_total} 道已验证选择题，其余知识点将明确提示不可用（不会出空题）"}
    result = test_llm(url, key, name, min(timeout, 15))
    if not result.startswith(("连接成功", "HTTP 200")):
        return {"ok": False, "configured": False, "reason": result,
                "note": "验证失败未保存配置。请把 reason 转述给用户，让用户检查 key/地址后重试"}
    stored = update_llm({"base_url": url, "api_key": key, "model": name, "timeout_sec": timeout})
    if stored == "fallback":
        return {"ok": True, "configured": True, "test": result,
                "note": ("接口参数已保存并生效，但系统凭据管理器不可用、Key 未落盘（不写明文）。"
                         "请改为设置环境变量 BAGU_LLM_API_KEY 后重启 MCP 服务")}
    return {"ok": True, "configured": True, "test": result,
            "note": "配置已保存并即时生效（api_key 已安全存入系统凭据管理器，config.json 不含明文），后续出题将结合真实场景合成情景题"}


@mcp.tool()
@forward_to_desktop
def generate_quiz(knowledge_point_id: str, context: str = "") -> dict:
    """按知识点 ID 生成一道题。context 可附带当前场景（如相关代码片段），未提供时用会话记录。
    传入的 context 在进入 LLM 前统一脱敏（密钥/令牌自动替换为 [REDACTED]）并限制长度。
    生成失败（未配置 LLM、LLM 输出校验失败、来源正文缺失且无已验证选择题回退）时
    返回 ok=False 与明确原因，绝不返回无选项/无答案的题目。"""
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
    if quiz.get("available") is False:
        return {"ok": False, "knowledge_point_id": knowledge_point_id,
                "reason": quiz.get("unavailable_reason") or "题目生成失败",
                "note": "不提供无法作答的题目。可调用 open_config_window 配置 LLM 后重试"}
    quiz_id = uuid.uuid4().hex[:8]
    with _STATE_LOCK:
        caps = _caps()
        now = _now()
        quiz.update({"_created_at": now, "_expires_at": now + caps["ttl"],
                     "_status": "delivered"})
        _state["quizzes"][quiz_id] = quiz
        _get_store_locked().create_quiz(
            quiz_id, quiz.get("kp_id") or knowledge_point_id,
            _quiz_snapshot(quiz), "delivered", str(PROJECT_ROOT),
            f"手动 generate_quiz | kp={knowledge_point_id}",
            now, now + caps["ttl"], delivered_at=now)
        _cleanup_locked(caps)
    return {"ok": True, **_public_quiz(quiz_id, quiz, str(PROJECT_ROOT)),
            "next": "用 submit_answer(quiz_id, 用户答案) 判分并获取解析"}


@mcp.tool()
@forward_to_desktop
def submit_answer(quiz_id: str, answer: str) -> dict:
    """提交答案，返回对错 + 解析 + 复习安排（schedule）。
    学习闭环：单事务落库 attempt 与 concept_state（掌握度/下次复习时间由
    SM-2 变体确定性计算），quiz 标记 answered。
    幂等性：同一 quiz 只允许提交一次，重复提交明确拒绝（不重复计数）。"""
    with _STATE_LOCK:
        now = _now()
        quiz = _state["quizzes"].get(quiz_id)
        if not quiz:
            store = _get_store_locked()  # 懒建库时会先水合 DB 中未完成的题
            quiz = _state["quizzes"].get(quiz_id)
            if not quiz:
                row = store.get_quiz(quiz_id)
                if row and row["status"] == "answered":
                    return {"ok": False, "note": f"quiz_id {quiz_id} 已作答，重复提交已拒绝（不重复计数）"}
                return {"ok": False, "note": f"quiz_id {quiz_id} 不存在或已过期"}
        if quiz.get("_status") == "answered":
            return {"ok": False,
                    "note": f"quiz_id {quiz_id} 已作答，重复提交已拒绝（不重复计数）"}
        if (quiz.get("_expires_at") or 0) <= now:
            _get_store_locked().set_status(quiz_id, "expired")
            _state["quizzes"].pop(quiz_id, None)
            _state["pending"] = [p for p in _state["pending"]
                                 if p["quiz_id"] != quiz_id]
            return {"ok": False, "note": f"quiz_id {quiz_id} 不存在或已过期"}
        grade = scorer.grade(quiz, answer)
        store = _get_store_locked()
        prev = store.concept_state(quiz.get("kp_id"))
        state = next_state(grade["correct"], prev, now)
        # 单事务：attempt + concept_state + quiz 状态
        store.record_attempt(
            quiz_id, quiz.get("kp_id") or "", grade["your_answer"],
            grade["normalized_answer"], grade["correct"], now, state)
        for p in _state["pending"]:
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


@mcp.tool()
@forward_to_desktop
def get_learning_stats() -> dict:
    """【学习统计·只读】返回学习闭环聚合数据：已作答题数、总作答次数、
    正确率、待复习数、薄弱知识点（掌握度低于 0.6 且至少作答一次）。
    不返回任何会话原文/代码内容，只有聚合数字与知识点名称。"""
    with _STATE_LOCK:
        store = _get_store_locked()
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


if __name__ == "__main__":
    _startup_log()
    # 后台监测线程：定期采集双信号源并预生成题目（「AI 干活→自动出题」的引擎）
    threading.Thread(target=_monitor_loop, daemon=True, name="bagu-monitor").start()
    # 未配置 LLM 时自动弹出配置窗口（用户点开/刷新本 MCP 即触发，一次填好免手改文件）
    if not desktop_running() and not _llm_configured():
        print("[bagu-trainer] 未检测到 LLM 配置，自动弹出 AI 接口配置窗口…", file=sys.stderr)
        _open_config_window()
    mcp.run(transport="stdio")
