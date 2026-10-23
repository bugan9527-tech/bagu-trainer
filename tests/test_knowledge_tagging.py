# -*- coding: utf-8 -*-
"""Knowledge-point tagging and filtering regression tests."""
from bagu_trainer.knowledge_base import KnowledgeBase
from bagu_trainer.knowledge_taxonomy import classify_question


def _q(title, repo="JavaGuide", path="docs/java/basis/java-basic.md"):
    return {
        "id": "x", "question": title, "knowledge_point": title,
        "tags": ["八股", repo], "source_paths": [path],
    }


def test_redis_cache_labels():
    labels = classify_question(_q(
        "Redis 缓存穿透和空值缓存怎么处理？",
        path="docs/database/redis/cache-basics.md"))
    assert labels["primary_knowledge_tag"] == "Redis"
    assert "缓存" in labels["knowledge_tags"]
    assert labels["knowledge_domain"] == "数据库"


def test_java_hashmap_is_collection():
    labels = classify_question(_q("HashMap 的扩容机制是什么？"))
    assert labels["primary_knowledge_tag"] == "Java 集合"
    assert labels["knowledge_domain"] == "Java"


def test_rag_and_agent_labels():
    labels = classify_question(_q(
        "RAG 中 Agent 应该如何做召回和重排？",
        repo="llm_interview_note", path="rag/agent-rag.md"))
    assert "RAG 与检索增强" in labels["knowledge_tags"]
    assert "AI Agent" in labels["knowledge_tags"]


def test_unknown_question_has_repository_fallback():
    labels = classify_question(_q(
        "完全无法识别的标题", repo="fe-interview", path="unknown.md"))
    assert labels["knowledge_tags"] == ["前端开发"]
    assert labels["knowledge_tag_source"] == "repo_default"


def test_windows_source_paths_are_normalized():
    labels = classify_question(_q(
        "TCB 和 PCB 有什么关系？",
        path="JavaGuide/docs\\cs-basics\\operating-system\\os.md"))
    assert labels["primary_knowledge_tag"] == "操作系统"
    assert "source_path" in labels["knowledge_tag_source"]


def test_http_method_comparison_is_tagged_as_http():
    labels = classify_question(_q(
        "Post 和 Get 的区别",
        path="CS-Interview-Knowledge-Map/Network/Network-zh.md"))
    assert labels["primary_knowledge_tag"] == "HTTP 与 Web"


def test_all_questions_have_labels_and_filter_works():
    kb = KnowledgeBase()
    questions = kb.all()
    assert questions
    assert all(q.get("knowledge_tags") for q in questions)
    assert all(len(q["knowledge_tags"]) <= 3 for q in questions)

    redis = [
        k for k in kb.list_knowledge_points()
        if "Redis" in k.get("knowledge_tags", [])
    ]
    assert redis


def test_server_tag_filter_uses_knowledge_tags(env):
    import bagu_trainer.engine as engine

    result = engine.list_knowledge_points(tag="Redis")
    assert result["total"] > 0
    assert all("Redis" in k["knowledge_tags"] for k in result["knowledge_points"])
