"""Public-interface checks for lazy and instance-scoped engine assembly."""
import subprocess
import sys
from pathlib import Path

import pytest

from bagu_trainer import bootstrap
from bagu_trainer.application.learning.engine import EngineSession
from bagu_trainer.domain.quizzes import grounding_receipt
from bagu_trainer.domain.grading import Scorer
from bagu_trainer.desktop.startup import StartupCoordinator


class Bank:
    explicit_empty = False

    def __init__(self, name):
        self.name = name
        self.question = {
            "id": name, "knowledge_point": name, "difficulty": "easy",
            "question": "How should this isolated instance behave?",
            "tags": [], "keywords": [], "sources": [],
        }

    def list_knowledge_points(self):
        return [dict(self.question)]

    def all(self):
        return self.list_knowledge_points()

    def get(self, _kp_id):
        return dict(self.question) if _kp_id == self.name else None


class Matcher:
    def __init__(self, bank):
        self.bank = bank

    def rank(self, _text, top_n=3):
        question = self.bank.get(self.bank.name)
        return [{"question": question, "id": question["id"], "score": 5,
                 "raw_score": 5.0, "confidence": 1.0, "margin": 1.0,
                 "hits": [self.bank.name]}][:top_n]


class Generator:
    def generate(self, question, context):
        review = {"context_relevant": True, "source_supported": True,
                  "answer_unique": True, "no_answer_leak": True,
                  "correct_answer": "A", "reason": "synthetic review"}
        return {
            "kp_id": question["id"], "kp_name": question["knowledge_point"],
            "difficulty": "easy", "question": "Synthetic instance question?",
            "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
            "answer": "A", "analysis": "Synthetic analysis", "generated_by": "llm",
            "grounding": grounding_receipt(
                {"context_quote": context[:32], "source_quote": "synthetic source",
                 "rationale": "synthetic rationale"}, review, context,
                "synthetic source", "test-model"),
        }


class Unused:
    pass


def make_engine(tmp_path, name, project_root=None):
    project_root = project_root or str(tmp_path / "project")
    Path(project_root).mkdir(parents=True, exist_ok=True)
    cfg = {
        "project_roots": [project_root], "db_path": str(tmp_path / f"{name}.db"),
        "max_quizzes_per_hit": 1, "quiz_ttl_seconds": 3600,
        "max_pending_quizzes": 20, "max_quizzes": 200,
        "seen_hash_capacity": 100, "context_char_limit": 4000,
        "session_ingest": False, "llm": None,
    }
    return bootstrap.create_engine(
        cfg=cfg, kb=Bank(name), matcher=None, generator=Generator(),
        scorer=Scorer(), session=EngineSession(database=cfg["db_path"]),
        matcher_factory=Matcher)


def test_importing_engine_does_not_assemble_bank_or_session():
    root = Path(__file__).resolve().parents[1]
    code = (
        "from bagu_trainer.infrastructure.knowledge_base import KnowledgeBase; "
        "KnowledgeBase.__init__ = lambda *a, **k: (_ for _ in ()).throw(AssertionError('loaded')); "
        "import bagu_trainer.application.learning.engine; import bagu_trainer.bootstrap"
    )
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True, timeout=10)


def test_explicit_engine_instances_keep_dependencies_and_sessions_isolated(tmp_path):
    project = str(tmp_path / "project")
    first = make_engine(tmp_path, "first", project)
    second = make_engine(tmp_path, "second", project)
    try:
        assert first is not second
        assert first.session is not second.session
        assert first.kb is not second.kb
        assert first.list_knowledge_points()["knowledge_points"][0]["id"] == "first"
        assert second.list_knowledge_points()["knowledge_points"][0]["id"] == "second"
        info = {"context": "synthetic independent context", "sources": []}
        first_generated = first.generate_activity(project, info)[1]
        second_generated = second.generate_activity(project, info)[1]
        assert len(first_generated) == len(second_generated) == 1
        assert first.session.list_pending()[0]["quiz_id"] == first_generated[0]["quiz_id"]
        assert second.session.list_pending()[0]["quiz_id"] == second_generated[0]["quiz_id"]
        assert first.session.list_pending()[0]["quiz_id"] != second.session.list_pending()[0]["quiz_id"]
        assert first.session.database_path != second.session.database_path
    finally:
        first.close()
        second.close()


def test_learning_interface_isolates_workflows_and_reopens_after_close(tmp_path):
    project = str(tmp_path / "project")
    first = make_engine(tmp_path, "learning-first", project)
    second = make_engine(tmp_path, "learning-second", project)
    try:
        first.learning.apply_project_scope([project])
        second.learning.apply_project_scope([project])
        info = {"context": "synthetic learning interface context", "sources": []}
        first_result = first.learning.generate_for_activity(project, info)["result"]
        second_result = second.learning.generate_for_activity(project, info)["result"]
        first_quiz = first_result[1][0]["quiz_id"]
        second_quiz = second_result[1][0]["quiz_id"]
        assert [q["quiz_id"] for q in first.learning.pending_questions()] == [first_quiz]
        assert [q["quiz_id"] for q in second.learning.pending_questions()] == [second_quiz]
        assert first.learning.enter_question(first_quiz)["quiz_id"] == first_quiz
        assert first.learning.submit_answer(first_quiz, "A")["ok"]
        assert not first.learning.submit_answer(first_quiz, "A")["ok"]
        assert first.learning.history()["total"] == 1
        assert second.learning.history()["total"] == 0

        first.learning.close()
        with pytest.raises(RuntimeError, match="关闭"):
            first.learning.pending_questions()
        first.learning.reopen()
        assert first.learning.pending_questions() == []
    finally:
        first.close()
        second.close()


def test_learning_interface_discards_generation_after_project_removal(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    project = str(tmp_path / "project")
    instance = make_engine(tmp_path, "learning-removal", project)
    entered, release = threading.Event(), threading.Event()
    original = instance.generator.generate

    def delayed(question, context):
        entered.set()
        assert release.wait(3)
        return original(question, context)

    instance.generator.generate = delayed
    try:
        instance.learning.apply_project_scope([project])
        window = instance.learning.generation_window()
        with ThreadPoolExecutor(max_workers=1) as workers:
            result = workers.submit(
                instance.learning.generate_for_activity, project,
                {"context": "late result after project removal", "sources": []},
                generation_window=window)
            try:
                assert entered.wait(1)
                instance.learning.apply_project_scope([])
            finally:
                release.set()
            outcome = result.result(1)
        assert not outcome["current"]
        assert outcome["result"][1] == []
        assert instance.learning.pending_questions() == []
    finally:
        instance.close()


def test_default_bootstrap_is_shared_by_legacy_electron_and_tk_loaders():
    from bagu_trainer.application.learning import engine as legacy
    from bagu_trainer.desktop.service import DesktopService

    shared = bootstrap.get_engine()
    electron = DesktopService()
    tk = StartupCoordinator()
    try:
        assert bootstrap.get_engine() is shared
        assert legacy.kb is shared.kb
        assert legacy.session is shared.session
        assert legacy.learning is shared.learning
        assert electron.coordinator.engine_loader() is shared
        assert tk.engine_loader() is shared
    finally:
        electron.close()


def test_engine_close_and_initialize_keep_engine_identity_and_replace_session(tmp_path):
    instance = make_engine(tmp_path, "lifecycle")
    old_session = instance.session
    instance.close()
    try:
        with pytest.raises(RuntimeError, match="关闭"):
            old_session.list_pending()
        assert instance.initialize() is instance
        assert instance.session is not old_session
        assert instance.session.list_pending() == []
    finally:
        instance.close()
