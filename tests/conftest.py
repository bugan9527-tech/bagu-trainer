# -*- coding: utf-8 -*-
"""测试共享 fixture：全 fake 依赖，不触碰真实 .trae-cn 会话、真实文件监听、
真实 LLM、真实 keyring 凭据库与真实 config.json（可能含 API Key）。
"""
from pathlib import Path

# 在导入 engine 之前把 CONFIG_PATH 指向不存在的文件，
# 保证模块导入期 load_config() 不会读取真实 config.json
import bagu_trainer.config as config_mod  # noqa: E402
import bagu_trainer.secrets as secrets_mod  # noqa: E402

config_mod.CONFIG_PATH = Path(__file__).resolve().parent / "__no_config__.json"

import pytest  # noqa: E402

import bagu_trainer.engine as engine  # noqa: E402


class FakeDiffReader:
    """假文件变动源：只返回测试注入的固定结果，不进行真实文件系统监听"""

    def __init__(self):
        self.results = []

    def changes(self):
        if self.results:
            return self.results[-1]
        return {"source": "git", "files": [], "diff_text": ""}


class FakeMatcher:
    """固定命中一个知识点，与题库数据内容解耦；记录收到的上下文供断言"""

    KP = {
        "id": "test-kp-1",
        "knowledge_point": "测试知识点",
        "tags": ["cache"],
        "keywords": ["redis"],
        "difficulty": "easy",
        "question": "原题？",
        "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
        "answer": "A",
        "analysis": "解析",
    }

    def __init__(self):
        self.last_text = None

    def match(self, text, top_n=3, min_score=1):
        return [(r["question"], r["score"], r["hits"])
                for r in self.rank(text, top_n=top_n, min_score=min_score)]

    def rank(self, text, top_n=3, min_score=1):
        """与真实 Matcher.rank 同构：返回带 confidence/margin 的详情列表"""
        self.last_text = text
        if not (text or "").strip():
            return []
        return [{
            "question": dict(self.KP),
            "id": self.KP["id"],
            "score": 5,
            "raw_score": 5.0,
            "confidence": 1.0,
            "margin": 1.0,
            "hits": ["redis"],
        }][:max(1, top_n)]


class FakeGenerator:
    """假出题器：不调用任何 LLM；calls 计数用于断言生成次数，
    last_ctx 记录收到的上下文用于隐私断言"""

    def __init__(self):
        self.calls = 0
        self.last_ctx = None

    def generate(self, kp_question, context_text):
        self.calls += 1
        self.last_ctx = context_text
        return {
            "kp_id": kp_question["id"],
            "kp_name": kp_question["knowledge_point"],
            "difficulty": kp_question["difficulty"],
            "question": "场景题-%d" % self.calls,
            "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
            "answer": "A",
            "analysis": "解析",
            "generated_by": "fake",
        }


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    """每个用例重置服务内存态，并注入全 fake 依赖 + 独立临时 SQLite；
    同时禁用环境变量与真实 keyring，保证密钥解析 100% 确定"""
    engine._state["quizzes"].clear()
    engine._state["pending"].clear()
    engine._state["seen"].clear()
    engine._state["seen_set"].clear()
    engine._diff_readers.clear()
    engine._store = None  # 每个用例独立的临时学习数据库

    # 测试隔离：不读取用户本机环境变量里的 Key、不触碰真实凭据库
    monkeypatch.delenv(secrets_mod.ENV_KEY, raising=False)
    monkeypatch.setattr(secrets_mod, "_load_keyring", lambda: None)
    secrets_mod._HINTED["legacy"] = False
    secrets_mod._HINTED["fallback"] = False

    cfg = {
        "trae_home": None,
        "project_roots": [str(engine.PROJECT_ROOT)],
        "max_quizzes_per_hit": 1,
        "session_recent_minutes": 180,
        "session_ingest": False,
        "context_char_limit": 4000,
        "llm": None,  # 永不走真实 LLM 分支
    }
    caps = {
        "quiz_ttl_seconds": 3600,
        "max_pending_quizzes": 20,
        "max_quizzes": 200,
        "seen_hash_capacity": 100,
        "db_path": str(tmp_path / "trainer.db"),  # 临时库：不落真实用户数据目录
    }
    monkeypatch.setattr(engine, "cfg", cfg)
    monkeypatch.setattr(engine, "load_config", lambda: {**cfg, **caps})
    monkeypatch.setattr(engine, "find_trae_home", lambda cfg_home=None: None)
    fake_diff = FakeDiffReader()
    monkeypatch.setattr(engine, "DiffReader", lambda root: fake_diff)
    fake_gen = FakeGenerator()
    monkeypatch.setattr(engine, "generator", fake_gen)
    fake_matcher = FakeMatcher()
    monkeypatch.setattr(engine, "matcher", fake_matcher)

    ctx = {"cfg": cfg, "caps": caps, "diff": fake_diff,
           "gen": fake_gen, "matcher": fake_matcher}
    yield ctx
