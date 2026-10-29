"""Private session and persistence implementation for the learning module."""
import collections
import threading
import uuid

from bagu_trainer.infrastructure.store import Store
from bagu_trainer.domain.quizzes import admission_failure as _generation_failure
from bagu_trainer.domain.quizzes import is_expired
from bagu_trainer.domain.quizzes import quiz_deadline
from bagu_trainer.domain.review import review_candidates
from bagu_trainer.application.learning import engine as _legacy
from bagu_trainer.application.learning.engine import _ACTIVE_ENGINE
from bagu_trainer.application.learning.engine import _caps
from bagu_trainer.application.learning.engine import _db_path
from bagu_trainer.application.learning.engine import _engine
from bagu_trainer.application.learning.engine import _normalize_project_root
from bagu_trainer.application.learning.engine import _public_pending
from bagu_trainer.application.learning.engine import _public_quiz
from bagu_trainer.application.learning.engine import _project_roots
from bagu_trainer.application.learning.engine import _quiz_snapshot
from bagu_trainer.application.learning.engine import _resolve_project_root
from bagu_trainer.application.learning.engine import _using


def _now():
    # Resolve dynamically so legacy monkeypatches and the configured clock seam
    # remain effective during the transition.
    return _legacy._now()


class EngineSession:
    """Own delivery state and its SQLite lifetime, independent of pages/workers.

    Learning workflows share this session's reentrant lock; callers use the
    application Interface and never manipulate a connection or delivery row.
    """
    def __init__(self, *, database=None, clock=None, engine=None):
        self._engine = engine
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
        self._recovery_required = False

    def _now(self):
        return self._clock() if self._clock is not None else _now()

    def _ensure_open(self, *, maintenance=False):
        if self._closed:
            raise RuntimeError("学习会话已关闭")
        if self._recovery_required and not maintenance:
            raise RuntimeError("内容维护尚未恢复，请先重试恢复。")

    def maintenance(self):
        """Serialize bank/file maintenance with learning writes and generation."""
        return self._lock

    @property
    def database_path(self):
        if self._store is not None:
            return self._store.path
        if self._database is not None:
            return self._database
        if self._engine is not None and _ACTIVE_ENGINE.get() is not self._engine:
            with _using(self._engine):
                return _db_path()
        return _db_path()

    def _generation_token(self):
        with self._lock:
            self._ensure_open()
            return self, self._revision

    def _accepts_result(self, token):
        with self._lock:
            return (not self._closed and not self._recovery_required
                    and token == (self, self._revision))

    def _content_changed(self):
        """A bank switch keeps pending/history but retires old generation work."""
        with self._lock:
            self._ensure_open(maintenance=True)
            self._revision += 1
            self._generations.clear()
            self._recovery_required = False

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
            self._ensure_open(maintenance=True)
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
            roots = tuple(roots)
            previous = self._watch_roots
            if previous == roots:
                return
            previous = previous or ()
            self.forget_projects(set(previous) - set(roots))
            self._watch_roots = roots
            # Scope changes retire every operation window captured against the
            # prior allowlist. Existing reservations may finish, but their
            # results cannot be admitted; identity checks protect replacements.
            self._revision += 1
            self._generations.clear()

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
        """Restore unfinished work, applying expiry only for an explicit TTL."""
        now = self._now()
        for row in store.load_pending(now, expire=_caps()["ttl"] > 0):
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

    def _cleanup_locked(self, caps: dict):
        """过期/超量题的确定性清理（调用方必须已持 self._lock）。
        过期状态写通数据库（expired 持久化）；容量淘汰仅清内存，数据库保留历史。"""
        now = self._now()
        store = self._get_store_locked()
        # 1) 到期未作答：pending -> expired（写通 DB），quiz 一并移除
        for p in self._state["pending"]:
            quiz = self._state["quizzes"].get(p["quiz_id"], {})
            if p["status"] in ("ready", "delivered") and (
                    is_expired(p["expires_at"], now=now, ttl_seconds=caps["ttl"]) or
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
            if is_expired(q.get("_expires_at", float("inf")), now=now, ttl_seconds=caps["ttl"]):
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
        cands = review_candidates(due, pending_kps, prefer, only_prefer=only_prefer)
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
            quiz.update({"_created_at": now, "_expires_at": quiz_deadline(now, caps["ttl"]),
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
                "expires_at": quiz_deadline(now, caps["ttl"]),
                **public,
            })
            store.create_quiz(
                quiz_id, row["kp_id"], _quiz_snapshot(quiz), "delivered", root,
                f"到期复习 | kp={row['kp_id']} | 上次作答 {hist['answered_at']}"[:200],
                now, quiz_deadline(now, caps["ttl"]), delivered_at=now)
            return public
        return None

    def take_quiz(self):
        """Deliver the oldest ready quiz in the current configured scopes."""
        if getattr(_engine().kb, "explicit_empty", False):
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
        if getattr(_engine().kb, "explicit_empty", False):
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
        if getattr(_engine().kb, "explicit_empty", False):
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
        if getattr(_engine().kb, "explicit_empty", False):
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
