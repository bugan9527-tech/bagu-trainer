"""Learning workflow Interface shared by the desktop adapters.

The implementation deliberately keeps the existing engine algorithms and
session lock/transaction ordering. Callers express learning intent here; the
session, generation revision and SQLite connection remain private to this
module's adapter to the transitional engine implementation.
"""

from bagu_trainer.infrastructure.paths import canonical_project_root as _canonical_project_root
from bagu_trainer.domain.quizzes import admission_failure
from bagu_trainer.domain.quizzes import generation_eligible
from bagu_trainer.domain.quizzes import is_expired
from bagu_trainer.domain.quizzes import quiz_deadline
from bagu_trainer.domain.review import next_state


class Learning:
    """One application's learning workflow and lifecycle."""

    def __init__(self, engine, session=None):
        self.__engine = engine
        self.__session = session
        if session is not None:
            session._engine = engine

    def _attach_compat_session(self, session):
        """Engine-only bridge for historical ``engine.session`` callers."""
        self.__session = session
        session._engine = self.__engine

    @property
    def _compat_session(self):
        """Engine-only bridge; desktop callers use the workflow Interface."""
        return self.__session

    def has_question_bank(self):
        return not bool(getattr(self.__engine.kb, "explicit_empty", False))

    def question_count(self):
        return len(self.__engine.kb.all())

    def observe_project(self, root, *, reader_factory=None):
        return self.__engine.observe_project(root, reader_factory=reader_factory)

    def apply_project_scope(self, roots):
        """Apply the already resolved project allowlist to generation.

        Session scope changes retire generation work for removed projects while
        preserving their persisted learning history.
        """
        scopes = list(dict.fromkeys(_canonical_project_root(root) for root in roots))
        self.__session.set_watch_roots(scopes)

    def forget_projects(self, roots):
        self.__session.forget_projects(roots)


    def generation_window(self):
        """Create a one-use activity operation bound to the current content.

        The opaque closure captures the session generation internally, so a
        desktop adapter never handles tokens or decides whether a result is
        stale. Capture it before filesystem scanning and invoke it afterwards.
        """
        token = self.__session._generation_token()

        def generate(root, info, *, stop=None, retry_failed_ids=()):
            result = self.__engine.generate_activity(
                root, info, retry_failed_ids=retry_failed_ids,
                revision=token, stop=stop)
            with self.__session._lock:
                scope = self.__session._watch_roots
                in_scope = scope is None or _canonical_project_root(root) in set(scope)
                current = (not self.__session._closed
                           and token == (self.__session, self.__session._revision))
            return {"result": result, "current": current and in_scope}

        return generate

    def _generate_matches(self, ctx, project_root, top_n, caps, source_summary="",
                          *, retry_failed_ids=(), revision=None, stop=None,
                          deliver_generated=False):
        """Reserve, deduplicate, cool down and persist generated questions.

        The model request runs without the session or Store lock. Its result is
        admitted only after rechecking the session revision, active bank,
        project allowlist, pending cooldown and replacement reservation.
        """
        import copy
        import uuid
        from bagu_trainer.application.learning import engine as legacy

        normalize = _canonical_project_root
        project_root = normalize(project_root)
        owner = self.__session

        def in_active_scope_locked():
            # The applied runtime scope is authoritative once the application
            # has committed one. Config is only the compatibility fallback for
            # callers that still use the module-level engine directly.
            if owner._watch_roots is not None:
                return project_root in owner._watch_roots
            return project_root in {normalize(root) for root in legacy._project_roots()}

        with owner._lock:
            if revision is not None and not owner._accepts_result(revision):
                return [], [], 0, []
            owner._ensure_open()
            bank = self.__engine.kb
            revision = revision or owner._generation_token()
            ranked = copy.deepcopy(self.__engine.matcher.rank(ctx, top_n=top_n))
            model = self.__engine.generator
        matches, generated, skipped, unavailable = [], [], 0, []
        for rank in ranked:
            if stop is not None and stop.is_set():
                break
            question, score, hits = rank["question"], rank["score"], rank["hits"]
            if retry_failed_ids and question["id"] not in retry_failed_ids:
                continue
            matches.append({"question": question, "score": score, "hits": hits,
                            "confidence": rank["confidence"], "margin": rank["margin"]})
            key = legacy._match_hash(project_root, question["id"], ctx)
            slot = (project_root, question["id"])
            reservation = object()
            with owner._lock:
                if (self.__session is not owner or self.__engine.kb is not bank
                        or not owner._accepts_result(revision)):
                    break
                owner._cleanup_locked(caps)
                store = owner._get_store_locked()
                if not in_active_scope_locked() or getattr(self.__engine.kb, "explicit_empty", False):
                    skipped += 1
                    continue
                fresh = owner._mark_seen_locked(key, caps["seen_cap"])
                state = store.concept_state(question["id"])
                if not generation_eligible(
                        now=legacy._now(), next_review_at=(state or {}).get("next_review_at"),
                        reserved=slot in owner._generations, fresh=fresh,
                        explicit_retry=question["id"] in retry_failed_ids,
                        pending=question["id"] in owner._pending_kp_ids_locked(project_root)):
                    skipped += 1
                    continue
                owner._generations[slot] = reservation
            try:
                try:
                    quiz = model.generate(question, ctx)
                    quiz["sources"] = question.get("sources", [])
                except Exception as error:
                    quiz = {"available": False, "failure_kind": "service",
                            "unavailable_reason": f"出题失败（{type(error).__name__}），请检查模型配置后重试。"}
                with owner._lock:
                    if (self.__session is not owner or self.__engine.kb is not bank
                            or not owner._accepts_result(revision)
                            or owner._generations.get(slot) is not reservation):
                        skipped += 1
                        continue
                    if not in_active_scope_locked() or getattr(self.__engine.kb, "explicit_empty", False):
                        skipped += 1
                        continue
                    owner._cleanup_locked(caps)
                    store = owner._get_store_locked()
                    state = store.concept_state(question["id"])
                    if not generation_eligible(now=legacy._now(),
                            pending=question["id"] in owner._pending_kp_ids_locked(project_root),
                            next_review_at=(state or {}).get("next_review_at")):
                        skipped += 1
                        continue
                    failure = admission_failure(quiz, require_review=True)
                    if failure:
                        unavailable.append({
                            "kp_id": question["id"],
                            "knowledge_point": question["knowledge_point"],
                            "reason": failure,
                            "failure_kind": ("quality" if quiz.get("failure_kind") == "quality"
                                             else "service"),
                        })
                        continue
                    now = legacy._now()
                    status = "delivered" if deliver_generated else "ready"
                    delivered_at = now if deliver_generated else None
                    deadline = quiz_deadline(now, caps["ttl"])
                    quiz.update({"_created_at": now, "_expires_at": deadline,
                                 "_status": status})
                    quiz_id = uuid.uuid4().hex[:8]
                    store.create_quiz(
                        quiz_id, question["id"], legacy._quiz_snapshot(quiz), status,
                        str(project_root), f"{source_summary} | kp={question['id']}"[:200],
                        now, deadline, delivered_at=delivered_at)
                    owner._state["quizzes"][quiz_id] = quiz
                    public = {"hits": list(hits), "score": score,
                              "confidence": rank["confidence"], "margin": rank["margin"],
                              **legacy._public_quiz(quiz_id, quiz, project_root)}
                    owner._state["pending"].append({
                        "quiz_id": quiz_id, "kp_id": question["id"],
                        "project_root": str(project_root), "status": status,
                        "created_at": now, "delivered_at": delivered_at,
                        "answered_at": None, "expires_at": deadline, **public,
                    })
                    generated.append(public)
                    owner._cleanup_locked(caps)
            finally:
                with owner._lock:
                    if owner._generations.get(slot) is reservation:
                        owner._generations.pop(slot, None)
        return matches, generated, skipped, unavailable

    def generate_for_activity(self, root, info, *, stop=None, retry_failed_ids=(),
                              generation_window=None):
        """Generate for observed activity; generation reservations and stale
        result checks remain in the learning implementation.
        """
        operation = generation_window or self.generation_window()
        return operation(root, info, stop=stop, retry_failed_ids=retry_failed_ids)

    def pending_questions(self):
        if not self.has_question_bank():
            return []
        return self.__session.list_pending()

    def enter_question(self, quiz_id):
        if not self.has_question_bank():
            return None
        return self.__session.enter_quiz(quiz_id)

    def resume_practice(self):
        if not self.has_question_bank():
            return None
        return self.__session.resume_quiz()

    def take_next_question(self):
        if not self.has_question_bank():
            return None
        return self.__session.take_quiz()

    def submit_answer(self, quiz_id, answer):
        from bagu_trainer.application.learning import engine as legacy

        with self.__engine.content_maintenance():
            now = legacy._now()
            quiz = self.__session._state["quizzes"].get(quiz_id)
            if not quiz:
                store = self.__session._get_store_locked()
                quiz = self.__session._state["quizzes"].get(quiz_id)
                if not quiz:
                    row = store.get_quiz(quiz_id)
                    if row and row["status"] == "answered":
                        return {"ok": False, "note": f"quiz_id {quiz_id} 已作答，重复提交已拒绝（不重复计数）"}
                    return {"ok": False, "note": f"quiz_id {quiz_id} 不存在或已过期"}
            if quiz.get("_status") == "answered":
                return {"ok": False,
                        "note": f"quiz_id {quiz_id} 已作答，重复提交已拒绝（不重复计数）"}
            failure = admission_failure(quiz, require_review=not quiz.get("review"))
            if failure:
                self.__session._cleanup_locked(legacy._caps())
                return {"ok": False, "note": failure}
            caps = legacy._caps()
            if is_expired(quiz.get("_expires_at") or 0, now=now, ttl_seconds=caps["ttl"]):
                self.__session._get_store_locked().set_status(quiz_id, "expired")
                self.__session._state["quizzes"].pop(quiz_id, None)
                self.__session._state["pending"] = [
                    item for item in self.__session._state["pending"]
                    if item["quiz_id"] != quiz_id]
                return {"ok": False, "note": f"quiz_id {quiz_id} 不存在或已过期"}
            grade = self.__engine.scorer.grade(quiz, answer)
            store = self.__session._get_store_locked()
            previous = store.concept_state(quiz.get("kp_id"))
            state = next_state(grade["correct"], previous, now)
            # One Store transaction preserves the existing duplicate-submit guard.
            recorded = store.record_attempt(
                quiz_id, quiz.get("kp_id") or "", grade["your_answer"],
                grade["normalized_answer"], grade["correct"], now, state, once=True)
            if recorded is False:
                quiz["_status"] = "answered"
                return {"ok": False, "note": "该题已作答，重复提交已拒绝（不重复计数）"}
            for item in self.__session._state["pending"]:
                if item["quiz_id"] == quiz_id:
                    item["status"] = "answered"
                    item["answered_at"] = now
            quiz["_status"] = "answered"
        return {
            "ok": True, **grade,
            "schedule": {
                "next_review_at": state["next_review_at"],
                "interval_days": state["interval_days"],
                "mastery": state["mastery"],
            },
        }

    def repractice_history(self, attempt_id):
        import uuid
        from bagu_trainer.application.learning import engine as legacy

        with self.__engine.content_maintenance():
            if not self.has_question_bank():
                return {"ok": False, "note": "当前无题库，请重新下载并构建来源后练习；历史快照仍可查看。"}
            detail = self.__engine.get_answer_detail(attempt_id)
            if not detail.get("ok") or not detail.get("can_retry"):
                return {"ok": False, "note": detail.get("retry_reason") or detail.get("note")}
            store = self.__session._get_store_locked()
            now, caps = legacy._now(), legacy._caps()
            row = store.unfinished_repractice(int(attempt_id), now, expire=caps["ttl"] > 0)
            if row is None:
                quiz = dict(detail["snapshot"])
                quiz.update(review=True, manual_review=True,
                            retry_of_attempt_id=int(attempt_id),
                            retry_of_quiz_id=detail["quiz_id"])
                quiz["kp_id"] = detail.get("kp_id") or quiz.get("kp_id") or ""
                quiz_id = uuid.uuid4().hex
                deadline = quiz_deadline(now, caps["ttl"])
                store.create_quiz(quiz_id, quiz["kp_id"], quiz, "delivered",
                                  detail.get("project_root"), detail.get("source_summary"),
                                  now, deadline, delivered_at=now)
                row = store.get_quiz(quiz_id)
            quiz_id, quiz = row["quiz_id"], dict(row["snapshot"])
            quiz.update(_status=row["status"], _created_at=row["created_at"],
                        _expires_at=row["expires_at"])
            self.__session._state["quizzes"][quiz_id] = quiz
            if not any(item["quiz_id"] == quiz_id for item in self.__session._state["pending"]):
                self.__session._state["pending"].append({
                    **row, **legacy._public_quiz(quiz_id, quiz, row["project_root"])})
            return {"ok": True, "quiz": {
                **legacy._public_quiz(quiz_id, quiz, row["project_root"]),
                "manual_review": True}}

    def history(self, **filters):
        return self.__engine.get_answer_history(**filters)

    def history_detail(self, attempt_id):
        return self.__engine.get_answer_detail(attempt_id)

    def learning_stats(self):
        return self.__engine.get_learning_stats()

    def update_settings(self, values):
        """Update effective learning limits without exposing the config dict."""
        self.__engine.cfg.update(values)

    def replace_question_bank(self):
        self.__engine.reload_bank()

    def invalidate_content(self):
        self.__engine.invalidate_content()

    def content_unavailable(self):
        """Stop learning after incomplete maintenance until a successful reload.

        Recovery can still replace content under the maintenance lock. A
        terminally closed session stays closed; this never initializes one.
        """
        with self.__session.maintenance():
            self.__session._recovery_required = True
            self.__session._clear()

    def close(self):
        self.__session.close()

    def reopen(self):
        from bagu_trainer.application.learning.engine import EngineSession
        if self.__session._closed:
            self._attach_compat_session(EngineSession(engine=self.__engine))
