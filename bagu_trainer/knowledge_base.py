# -*- coding: utf-8 -*-
"""题库模块：加载 data/questions.json。

题目 JSON schema（每道题一个知识点）：
  id               知识点/题目 ID（如 "http-001"）
  knowledge_point  知识点名称
  tags             标签（如 ["http", "缓存"]）
  keywords         匹配关键词（中英文均可，含代码术语）
  question         题干
  options          {"A": ...} 四选项（题库导入题为空 {}，由出题模块合成）
  answer           正确选项字母（题库导入题为空）
  analysis         解析/来源说明
  difficulty       easy / medium / hard
  source_paths     来源 Markdown 相对路径（interview-banks/repos 之内）
  source_excerpt   从来源提取的正文片段（有长度上限；找不到为空）
  source_status    ok / missing / none
  source_match     exact / body_parent / parent / none（是否精确命中或可靠父章节）
  excerpt_quality  strong / weak / empty（与 source_status 分离的质量判定）
  usable_excerpt   是否允许进入 LLM（纯链接/图片/代码/推广/过短/题目清单为 false）
  excerpt_source   实际提取所用的来源路径
  knowledge_domain        知识点所属领域
  knowledge_tags          该题考察的知识点标签，最多 3 个
  primary_knowledge_tag   主要知识点
  knowledge_tag_source    标签来源：title/source_path/repo_default
  knowledge_tag_version   标签规则版本
"""
import json
from pathlib import Path

from .config import data_dir

BANK_PATH = data_dir() / "questions.json"


class KnowledgeBase:
    def __init__(self, path=None):
        from .bank_storage import active_bank_dir
        path = Path(path) if path is not None else active_bank_dir() / "questions.json"
        self.concepts_path = path.parent / "question_concepts.json"
        if not path.exists():
            raise FileNotFoundError(
                f"题库不存在：{path}。源码发布版不附带第三方题库；"
                "请按项目根目录 QUESTION_BANK.md 的步骤在本地构建后再启动。")
        self.questions = json.loads(path.read_text(encoding="utf-8"))
        record = path.parent / "sources.json"
        if record.is_file():
            from .provenance import question_sources
            entries = json.loads(record.read_text(encoding="utf-8")).get("sources", [])
            for question in self.questions:
                question["sources"] = question_sources(question, entries)
        self.explicit_empty = not self.questions and (path.parent / "empty-bank.json").is_file()
        self._by_id = {q["id"]: q for q in self.questions}

    def all(self) -> list:
        return list(self.questions)

    def get(self, qid: str):
        return self._by_id.get(qid)

    def list_knowledge_points(self) -> list:
        """按知识点聚合（id/名称/标签/难度/题数），供 list_knowledge_points 工具使用"""
        return [
            {
                "id": q["id"],
                "name": q["knowledge_point"],
                "tags": q["tags"],
                "knowledge_domain": q.get("knowledge_domain", ""),
                "knowledge_tags": q.get("knowledge_tags", []),
                "primary_knowledge_tag": q.get("primary_knowledge_tag", ""),
                "difficulty": q["difficulty"],
                "question_count": 1,
            }
            for q in self.questions
        ]
