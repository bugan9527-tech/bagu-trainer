# -*- coding: utf-8 -*-
"""学习闭环存储模块：SQLite 持久化 quizzes / attempts / concept_state。

schema（最小）：
  quizzes       quiz_id PK, kp_id, snapshot(JSON 题目快照), status,
                project_root, source_summary, created_at, expires_at,
                delivered_at, answered_at
  attempts      id, quiz_id, answer, normalized_answer, correct, answered_at
  concept_state kp_id PK, attempts, correct_count, mastery,
                last_reviewed_at, next_review_at, interval_days, ease, streak

隐私约束：仅保存脱敏后的来源摘要（source_summary，形如
"文件变动[git](3 个文件) | kp=bank-0001"），绝不保存上下文正文、会话记录
或代码内容；题目标快照只含题目/选项/答案/解析本身。

作用域：concept_state / attempts 是跨项目共享的学习历史，用于掌握度与复习
调度；未作答 quizzes 的 ready/delivered 队列及冷却由 server 按 project_root
隔离，复习题只能从已作答快照重建，不会直接搬运其他项目的 pending 题。

时间全部由调用方注入（epoch 秒），本模块自身不读墙钟，保证可用固定时间测试。
"""
import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS quizzes (
    quiz_id        TEXT PRIMARY KEY,
    kp_id          TEXT NOT NULL,
    snapshot       TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'ready',
    project_root   TEXT NOT NULL DEFAULT '',
    source_summary TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL,
    delivered_at   REAL,
    answered_at    REAL
);
CREATE TABLE IF NOT EXISTS attempts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id           TEXT NOT NULL,
    answer            TEXT NOT NULL,
    normalized_answer TEXT NOT NULL DEFAULT '',
    correct           INTEGER NOT NULL,
    answered_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_quiz ON attempts(quiz_id);
CREATE INDEX IF NOT EXISTS idx_attempts_history ON attempts(answered_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS concept_state (
    kp_id           TEXT PRIMARY KEY,
    attempts        INTEGER NOT NULL DEFAULT 0,
    correct_count   INTEGER NOT NULL DEFAULT 0,
    mastery         REAL NOT NULL DEFAULT 0.0,
    last_reviewed_at REAL,
    next_review_at  REAL,
    interval_days   REAL NOT NULL DEFAULT 0.0,
    ease            REAL NOT NULL DEFAULT 2.5,
    streak          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_state_next ON concept_state(next_review_at);
CREATE INDEX IF NOT EXISTS idx_quizzes_status_expires
    ON quizzes(status, expires_at);
"""

_INSERT_QUIZ = (
    "INSERT OR IGNORE INTO quizzes (quiz_id, kp_id, snapshot, status,"
    " project_root, source_summary, created_at, expires_at, delivered_at,"
    " answered_at) VALUES (?,?,?,?,?,?,?,?,?,?)")

_UPSERT_STATE = (
    "INSERT INTO concept_state (kp_id, attempts, correct_count, mastery,"
    " last_reviewed_at, next_review_at, interval_days, ease, streak)"
    " VALUES (?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(kp_id) DO UPDATE SET"
    " attempts=excluded.attempts, correct_count=excluded.correct_count,"
    " mastery=excluded.mastery, last_reviewed_at=excluded.last_reviewed_at,"
    " next_review_at=excluded.next_review_at,"
    " interval_days=excluded.interval_days, ease=excluded.ease,"
    " streak=excluded.streak")


class Store:
    """SQLite 学习数据存储。所有写路径带提交，读路径只读。"""

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False：连接跨工具线程复用（监控线程与工具调用线程），
        # 所有访问均在 self._lock 下串行，安全。
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        except Exception:
            self._conn.close()
            raise

    def close(self):
        with self._lock:
            self._conn.commit()
            self._conn.close()

    @staticmethod
    def _history_connection(path):
        # A separate read-only connection never waits for the engine generation
        # lock and never initializes schema, hydrates pending or expires quizzes.
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    @classmethod
    def history_page(cls, path, *, page=0, result="all", project=None, keyword=""):
        page = max(0, int(page))
        if result not in ("all", "wrong", "correct"):
            raise ValueError("Unknown history filter")
        empty = {"items": [], "total": 0, "page": page, "projects": []}
        if not Path(path).exists():
            return empty
        # Invalid legacy JSON is readable as a record with missing fields.
        name = "COALESCE(json_extract(CASE WHEN json_valid(q.snapshot) THEN q.snapshot ELSE '{}' END, '$.kp_name'), q.kp_id, '')"
        question = "COALESCE(json_extract(CASE WHEN json_valid(q.snapshot) THEN q.snapshot ELSE '{}' END, '$.question'), '')"
        conditions, values = [], []
        if result != "all":
            conditions.append("a.correct = ?")
            values.append(1 if result == "correct" else 0)
        if project is not None:
            conditions.append("COALESCE(q.project_root, '') = ?")
            values.append(project)
        if keyword.strip():
            # instr treats %, _ and quotes literally, with bound parameters.
            conditions.append(f"(instr(lower({name}), lower(?)) > 0 OR instr(lower({question}), lower(?)) > 0)")
            values.extend([keyword.strip(), keyword.strip()])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        join = " FROM attempts a LEFT JOIN quizzes q ON q.quiz_id = a.quiz_id"
        conn = cls._history_connection(path)
        try:
            conn.execute("BEGIN")  # count, page and projects share a consistent snapshot
            total = conn.execute("SELECT COUNT(*)" + join + where, values).fetchone()[0]
            page = min(page, max(0, (total - 1) // 20))
            rows = conn.execute(
                f"SELECT a.id, a.quiz_id, a.answered_at, a.correct, {name} AS knowledge_point,"
                " COALESCE(q.project_root, '') AS project_root" + join + where +
                " ORDER BY a.answered_at DESC, a.id DESC LIMIT ? OFFSET ?",
                [*values, 20, page * 20]).fetchall()
            projects = conn.execute(
                "SELECT DISTINCT COALESCE(q.project_root, '') AS project_root" + join +
                " ORDER BY project_root").fetchall()
            return {"items": [dict(row) for row in rows], "total": total, "page": page,
                    "projects": [r[0] for r in projects]}
        finally:
            conn.close()

    @classmethod
    def history_detail(cls, path, attempt_id):
        if not Path(path).exists():
            return None
        conn = cls._history_connection(path)
        try:
            row = conn.execute(
                "SELECT a.*, q.kp_id, q.snapshot, q.project_root, q.source_summary"
                " FROM attempts a LEFT JOIN quizzes q ON q.quiz_id=a.quiz_id WHERE a.id=?",
                (attempt_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        out = dict(row)
        try:
            snapshot = json.loads(out.pop("snapshot") or "{}")
        except (ValueError, TypeError):
            snapshot = {}
        out["snapshot"] = snapshot if isinstance(snapshot, dict) else {}
        return out

    def unfinished_repractice(self, attempt_id, now):
        with self._lock:
            row = self._conn.execute(
                "SELECT quiz_id FROM quizzes WHERE status IN ('ready','delivered') AND expires_at > ?"
                " AND json_extract(CASE WHEN json_valid(snapshot) THEN snapshot ELSE '{}' END,"
                " '$.retry_of_attempt_id') = ? ORDER BY created_at DESC LIMIT 1",
                (now, attempt_id)).fetchone()
        return self.get_quiz(row[0]) if row else None

    # ---- quizzes ----
    def create_quiz(self, quiz_id, kp_id, snapshot, status, project_root,
                    source_summary, created_at, expires_at,
                    delivered_at=None, answered_at=None):
        with self._lock:
            self._conn.execute(_INSERT_QUIZ, (
                quiz_id, kp_id or "",
                json.dumps(snapshot or {}, ensure_ascii=False),
                status, project_root or "", (source_summary or "")[:200],
                created_at, expires_at, delivered_at, answered_at))
            self._conn.commit()

    def set_status(self, quiz_id, status, delivered_at=None, answered_at=None):
        """更新状态；仅更新非 None 的时间字段（quiz 不存在时静默忽略）。"""
        with self._lock:
            sets, vals = ["status = ?"], [status]
            if delivered_at is not None:
                sets.append("delivered_at = ?")
                vals.append(delivered_at)
            if answered_at is not None:
                sets.append("answered_at = ?")
                vals.append(answered_at)
            vals.append(quiz_id)
            self._conn.execute(
                "UPDATE quizzes SET %s WHERE quiz_id = ?" % ", ".join(sets),
                vals)
            self._conn.commit()

    def get_quiz(self, quiz_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM quizzes WHERE quiz_id = ?", (quiz_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["snapshot"] = json.loads(out.get("snapshot") or "{}")
        return out

    def load_pending(self, now):
        """服务重启水合：返回未过期且未作答的题（按创建顺序）。
        同时把已过期的 ready/delivered 题写通为 expired。
        """
        with self._lock:
            self._conn.execute(
                "UPDATE quizzes SET status='expired'"
                " WHERE status IN ('ready','delivered') AND expires_at <= ?",
                (now,))
            rows = self._conn.execute(
                "SELECT * FROM quizzes"
                " WHERE status IN ('ready','delivered') AND expires_at > ?"
                " ORDER BY created_at, quiz_id", (now,)).fetchall()
            self._conn.commit()
        return [{**dict(r), "snapshot": json.loads(r["snapshot"] or "{}")}
                for r in rows]

    # ---- attempts + concept_state（作答事务）----
    def record_attempt(self, quiz_id, kp_id, answer, normalized_answer,
                       correct, answered_at, state, *, once=False):
        """单事务：写入 attempt + 更新 concept_state + 标记 quiz 已作答。"""
        with self._lock, self._conn:
            if once and self._conn.execute("SELECT 1 FROM attempts WHERE quiz_id=? LIMIT 1", (quiz_id,)).fetchone():
                return False
            self._conn.execute(
                "INSERT INTO attempts (quiz_id, answer, normalized_answer,"
                " correct, answered_at) VALUES (?,?,?,?,?)",
                (quiz_id, str(answer), str(normalized_answer or ""),
                 1 if correct else 0, answered_at))
            self._conn.execute(_UPSERT_STATE, (
                kp_id, state["attempts"], state["correct_count"],
                state["mastery"], state["last_reviewed_at"],
                state["next_review_at"], state["interval_days"],
                state["ease"], state["streak"]))
            self._conn.execute(
                "UPDATE quizzes SET status='answered', answered_at=?"
                " WHERE quiz_id=?", (answered_at, quiz_id))
        return True

    def attempt_count(self, quiz_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM attempts WHERE quiz_id = ?",
                (quiz_id,)).fetchone()
        return int(row["n"])

    def concept_state(self, kp_id):
        """知识点级学习历史：按 kp_id 全局共享，供跨项目复习调度使用。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM concept_state WHERE kp_id = ?",
                (kp_id,)).fetchone()
        return dict(row) if row else None

    # ---- 复习调度查询 ----
    def due_kps(self, now):
        """到期待复习知识点（next_review_at 最早优先）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kp_id, next_review_at FROM concept_state"
                " WHERE next_review_at IS NOT NULL AND next_review_at <= ?"
                " ORDER BY next_review_at, kp_id", (now,)).fetchall()
        return [dict(r) for r in rows]

    def latest_answered_snapshot(self, kp_id):
        """该知识点最近一次作答的题目快照（复习重建用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot, answered_at FROM quizzes"
                " WHERE kp_id = ? AND status = 'answered'"
                " ORDER BY answered_at DESC, quiz_id LIMIT 1",
                (kp_id,)).fetchone()
        if not row:
            return None
        return {"snapshot": json.loads(row["snapshot"] or "{}"),
                "answered_at": row["answered_at"]}

    # ---- 统计 ----
    def stats(self, now):
        with self._lock:
            a = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(correct), 0) AS c"
                " FROM attempts").fetchone()
            q = self._conn.execute(
                "SELECT COUNT(*) AS n FROM quizzes"
                " WHERE status = 'answered'").fetchone()
            d = self._conn.execute(
                "SELECT COUNT(*) AS n FROM concept_state"
                " WHERE next_review_at IS NOT NULL AND next_review_at <= ?",
                (now,)).fetchone()
        return {"total_attempts": int(a["n"]),
                "correct_count": int(a["c"]),
                "answered_quizzes": int(q["n"]),
                "due_reviews": int(d["n"])}

    def weak_kps(self, limit, weak_mastery):
        """薄弱知识点：至少作答一次且掌握度低于阈值，掌握度升序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM concept_state"
                " WHERE attempts > 0 AND mastery < ?"
                " ORDER BY mastery ASC, attempts DESC, kp_id LIMIT ?",
                (weak_mastery, limit)).fetchall()
        return [dict(r) for r in rows]
