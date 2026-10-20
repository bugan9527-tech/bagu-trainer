# -*- coding: utf-8 -*-
"""source_extractor 单元测试：标题匹配、长度上限、missing 标记、路径穿越防护。"""
from pathlib import Path

from bagu_trainer.source_extractor import (
    extract_excerpt,
    excerpt_for,
    normalize_title,
    safe_repo_file,
)

MD = """# 数据库

前言内容。

## 读写分离

### 什么是读写分离？

读写分离主要是为了将数据库的读操作和写操作分散到不同的数据库节点上。
写请求进入主库，读请求分摊到只读副本。

### 如何实现读写分离？

1. 部署多台数据库，选一台作为主库。
2. 建立主从复制关系。
3. 写请求交主库，读请求交从库。

## 分库分表

分库分表是另一回事。
"""


def test_normalize_title():
    assert normalize_title("如何实现读写分离？") == "如何实现读写分离"
    assert normalize_title("**0-1 背包**为什么容量要倒序遍历？") == "01背包为什么容量要倒序遍历"


def test_extract_by_heading():
    ex = extract_excerpt(MD, "如何实现读写分离？")
    assert "部署多台数据库" in ex and "主从复制" in ex
    assert "分库分表是另一回事" not in ex  # 下一同级标题截断


def test_extract_limit_capped():
    ex = extract_excerpt(MD, "如何实现读写分离？", limit=30)
    assert len(ex) <= 30


def test_extract_missing_title_returns_empty():
    assert extract_excerpt(MD, "不存在的标题？") == ""
    assert extract_excerpt(MD, "") == ""


def test_safe_repo_file_traversal_blocked(tmp_path):
    repos = tmp_path / "repos"
    repos.mkdir()
    (repos / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "secret.md").write_text("s", encoding="utf-8")
    # 正常相对路径
    assert safe_repo_file(repos, "a.md") == (repos / "a.md").resolve()
    assert safe_repo_file(repos, "sub/../a.md") == (repos / "a.md").resolve()
    # 路径穿越
    assert safe_repo_file(repos, "../secret.md") is None
    assert safe_repo_file(repos, "../../outside.md") is None
    # 绝对路径与盘符路径
    assert safe_repo_file(repos, str((tmp_path / "secret.md").resolve())) is None
    assert safe_repo_file(repos, "C:/Windows/win.ini") is None
    # 不存在的文件
    assert safe_repo_file(repos, "nothing.md") is None


def test_excerpt_for_status(tmp_path):
    repos = tmp_path / "repos"
    (repos / "r1").mkdir(parents=True)
    (repos / "r1" / "a.md").write_text("## 缓存穿透\n正文内容\n", encoding="utf-8")
    (tmp_path / "outside.md").write_text("## 缓存穿透\n越界内容\n", encoding="utf-8")

    ex, st, used = excerpt_for(repos, ["r1/a.md"], "缓存穿透")
    assert st == "ok" and used == "r1/a.md" and "正文内容" in ex
    # 文件缺失 -> missing，不伪造
    ex, st, used = excerpt_for(repos, ["r1/no.md", "r1/a.md"], "缓存穿透")
    assert st == "ok"
    ex, st, used = excerpt_for(repos, ["r1/no.md"], "缓存穿透")
    assert st == "missing" and ex == "" and used is None
    # 越界路径同样 missing 且不读取
    ex, st, used = excerpt_for(repos, ["../outside.md"], "缓存穿透")
    assert st == "missing" and ex == ""
    # 无来源路径 -> none
    ex, st, used = excerpt_for(repos, [], "缓存穿透")
    assert st == "none" and ex == ""