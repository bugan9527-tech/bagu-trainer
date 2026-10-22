# -*- coding: utf-8 -*-
"""
从 interview-banks/repos/<repo> 中提取「题目」与「知识点」条目，
先做仓库内/跨仓库精确去重，再做近重复（前缀桶 + Jaccard）剔除，
输出到 interview-banks/deduped/。

用法: python scripts/dedupe_questions.py --repos JavaGuide
只处理显式选中的本地仓库；仅在传 --fetch-issues 时访问 GitHub Issues。
"""
import argparse
import json
import logging
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from bagu_trainer.config import USER_DATA_ROOT

BANKS = USER_DATA_ROOT / "interview-banks"
REPOS_DIR = BANKS / "repos"
OUT_DIR = BANKS / "deduped"

TEXT_EXTS = {".md", ".mdown", ".markdown", ".txt", ".html", ".htm", ".rst"}
SKIP_DIR_PARTS = {".git", "node_modules", "assets", "image", "images", "img",
                  "pics", "picture", "media", "video", "videos", "fonts",
                  "static", "styles", "css", "js"}

REPO_STARS = {
    "CS-Notes": 186270, "JavaGuide": 159108, "fucking-algorithm": 135921,
    "advanced-java": 79154, "leetcode-master": 62696, "architect-awesome": 60843,
    "DeepLearning-500-questions": 57669, "leetcode": 55723, "interview": 38245,
    "interview_internal_reference": 37262, "Daily-Interview-Question": 27348,
    "fe-interview": 26263, "CS-Interview-Knowledge-Map": 18226,
    "interview_python": 17403, "llm_interview_note": 15254,
}

STOP_TITLES = {
    "readme", "license", "contributing", "table of contents", "toc",
    "home", "introduction", "overview", "reference", "references",
    "bibliography", "appendix", "about", "contact", "donate",
    "sponsor", "changelog", "faq", "acknowledgements", "目录", "总目录",
    "前言", "导论", "导读", "参考资料", "附录", "致谢", "关于", "联系",
    "说明", "使用指南", "概览", "大标题", "思维导图", "公众号", "版权声明",
    # 题解类模板小标题（非知识点）
    "题目描述", "问题描述", "描述", "示例", "输入输出", "输入", "输出",
    "思路", "解题思路", "解法", "解", "解法一", "解法二", "解法三",
    "代码", "代码实现", "复杂度分析", "复杂度", "提示", "注意",
    "注意事项", "注意点", "分析", "总结", "扩展", "延伸", "思考",
    "相关题目", "类似题目", "include", "content", "main", "source",
    "undefined", "truncated", "题目", "题解", "答案", "解析", "答案与解析",
    "详见", "更多内容", "欢迎关注", "四、总结", "五、参考资料", "参考",
}

# ---- 文本清洗 ----

def clean_md(text: str) -> str:
    """去掉 markdown 语法符号，尽量留下纯文本标题。"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # 图片
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)      # 链接
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)             # 内联 html 标签
    text = re.sub(r"[`*_~#>|]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize(text: str) -> str:
    """NFKC + 小写 + 只保留中英字母数字，用于精确/近重复比较。"""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]+", "", text)
    text = re.sub(r"^\s*(?:第?[0-9]+[.、)）\s]+|[一二三四五六七八九十百]+[、点题章])+", "", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)
    return text


def shingles(norm: str) -> set:
    """中文按 3 字窗、英文按词拆分，作近重复特征。"""
    spaced = re.sub(r"[a-z0-9]+", lambda m: " %s " % m.group(), norm)
    toks = [t for t in re.split(r"\s+", spaced) if t]
    out = set()
    for t in toks:
        if re.fullmatch(r"[\u4e00-\u9fff]+", t):
            for i in range(len(t) - 2):
                out.add(t[i:i + 3])
        else:
            out.add(t)
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---- 条目提取 ----

QUESTION_HINT = re.compile(r"[?？]|什么是|为何|为什么|如何|怎样|怎么|区别|优缺点|谈谈|讲一下|介绍一下|的作用|的原理|是否|哪些|多少")

READABLE = re.compile(r"[\u4e00-\u9fff]|[a-z]", re.I)


def is_question_title(title: str) -> bool:
    t = title.rstrip("。.")
    return bool(QUESTION_HINT.search(t))


def collect_entry(entries, repo, rel_path, idx, kind, text):
    t = clean_md(text)
    if not t:
        return
    # 长度/内容过滤
    if len(t) < 4 or len(t) > 200:
        return
    if not READABLE.search(t):
        return
    low = t.lower()
    if low in STOP_TITLES:
        return
    if re.fullmatch(r"[\d.:：、\s\-—]+", t):  # 纯序号/分隔线
        return
    norm = normalize(t)
    if len(norm) < 4:
        return
    # 纯英文/数字短标题（this、include 之类）与代码碎片
    if re.fullmatch(r"[a-z0-9 ._\-+*/=<>!&|]+", t.lower()) and len(norm) < 12:
        return
    if t.startswith(("=", "{", "func", "def ", "class ", "var ", "int ", "void ")):
        return
    entries.append({
        "repo": repo, "path": rel_path, "idx": idx, "kind": kind,
        "text": t, "norm": norm,
    })


TITLE_MD = re.compile(r"^(#{1,4})\s*(.+?)\s*#*\s*$")
QA_PREFIX = re.compile(r"^\s*(?:Q|问|题目|question)\s*[:：]\s*(.{4,200})$", re.I)
NUM_Q = re.compile(r"^\s*(?:\d+[.、\)]\s*|[-*+]\s+)(.{4,250}[?？])\s*$")
H_TAG = re.compile(r"<h([1-4])[^>]*>(.*?)</h\1>", re.S | re.I)


def extract_file(entries, repo: str, rel_path: str, text: str):
    ext = Path(rel_path).suffix.lower()
    if ext in (".html", ".htm"):
        for m in H_TAG.finditer(text):
            collect_entry(entries, repo, rel_path, m.start(), "html-title", m.group(2))
        return
    for ln, line in enumerate(text.splitlines()):
        m = TITLE_MD.match(line)
        if m:
            title = m.group(2)
            kind = "question" if is_question_title(title) else "knowledge"
            collect_entry(entries, repo, rel_path, ln, kind, title)
            continue
        m = QA_PREFIX.match(line) or NUM_Q.match(line)
        if m:
            collect_entry(entries, repo, rel_path, ln, "question", m.group(1))


def walk_repo(entries, repo: str, repos_dir=None, checkpoint=lambda: None, path_aliases=None):
    root = Path(repos_dir or REPOS_DIR) / repo
    original_paths = {stored: original for original, stored in (path_aliases or {}).items()}
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        checkpoint()
        if not p.is_file():
            continue
        if p.suffix.lower() not in TEXT_EXTS:
            continue
        rel = p.relative_to(root).as_posix()
        logical = original_paths.get(f"{repo}/{rel}", f"{repo}/{rel}")
        rel = logical[len(repo) + 1:]
        if set(Path(rel).parts) & SKIP_DIR_PARTS:
            continue
        try:
            raw = p.read_bytes()
        except Exception:
            continue
        if len(raw) > 3_000_000:  # 跳过超大文件
            continue
        if b"\x00" in raw[:1024]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gbk", errors="ignore")
            except Exception:
                continue
        extract_file(entries, repo, rel, text)


# ---- 去重 ----

def dedupe(entries, checkpoint=lambda: None):
    """返回 (保留条目列表, 剔除条目列表)。"""
    entries.sort(key=lambda e: (REPO_STARS.get(e["repo"], 0), e["repo"]), reverse=True)
    kept = []
    removed = []
    by_norm = {}
    for e in entries:
        checkpoint()
        e["srcs"] = [e["repo"]]
        key = e["norm"]
        if key in by_norm:  # 精确重复
            if e["repo"] not in by_norm[key]["srcs"]:
                by_norm[key]["srcs"].append(e["repo"])
            removed.append(dict(e, why="exact", dup_of=by_norm[key]))
            continue
        by_norm[key] = e
        e["sh"] = shingles(key)
        e["bucket"] = key[:14]
        kept.append(e)
    # 前缀桶内近重复
    buckets = defaultdict(list)
    for e in kept:
        buckets[e["bucket"]].append(e)
    seen_drop = set()
    for b, group in buckets.items():
        checkpoint()
        if len(group) < 2:
            continue
        group.sort(key=lambda e: REPO_STARS.get(e["repo"], 0), reverse=True)
        survivors = []
        for e in group:
            if id(e) in seen_drop:
                continue
            dup = False
            for s in survivors:
                checkpoint()
                if jaccard(e["sh"], s["sh"]) >= 0.78:
                    if e["repo"] not in s["srcs"]:
                        s["srcs"].append(e["repo"])
                    removed.append(dict(e, why="near", dup_of=s))
                    seen_drop.add(id(e))
                    dup = True
                    break
            if not dup:
                survivors.append(e)
    kept = [e for e in kept if id(e) not in seen_drop]
    return kept, removed


# ---- 输出 ----

def write_md(path, title, items, fmt=lambda e: e["text"]):
    lines = ["# %s" % title, ""]
    for i, e in enumerate(items, 1):
        srcs = " | ".join(reversed(e.get("srcs", [e["repo"]])))
        lines.append("%d. %s  `[来源: %s]`" % (i, fmt(e), srcs))
        if e["kind"] == "question" and "answer" in e:
            lines.append("   - 答案/解析: %s" % e["answer"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return len(items)


def main(argv=None, *, repos_dir=None, out_dir=None, fetch_daily_issues=None):
    global_defaults = (Path(repos_dir or REPOS_DIR), Path(out_dir or OUT_DIR))
    parser = argparse.ArgumentParser(description="从显式选择的本地来源提取题库")
    parser.add_argument("--repos", nargs="+", choices=sorted(REPO_STARS), required=True,
                        help="已阅读来源条款并选定的本地仓库目录名（见 QUESTION_BANK.md）")
    parser.add_argument("--fetch-issues", action="store_true",
                        help="额外通过 gh 下载 Daily-Interview-Question 的 Issue 标题")
    args = parser.parse_args(argv)
    selected = set(args.repos)
    if args.fetch_issues and "Daily-Interview-Question" not in selected:
        parser.error("--fetch-issues 要求 --repos 包含 Daily-Interview-Question")
    missing = [repo for repo in sorted(selected) if not (global_defaults[0] / repo).is_dir()]
    if missing:
        parser.error("本地来源不存在，请先下载：" + ", ".join(missing))
    return extract_sources(selected, *global_defaults,
                           fetch_issues=fetch_daily_issues if args.fetch_issues else None)


def extract_sources(selected, repos_dir, out_dir, *, checkpoint=lambda: None, fetch_issues=None,
                    path_aliases=None):
    """Extract selected local snapshots without external tools or network access."""
    repos_dir, out_dir = Path(repos_dir), Path(out_dir)
    selected = set(selected)
    if not selected or any(not (repos_dir / repo).is_dir() for repo in selected):
        raise ValueError("所选来源不存在")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "by_repo").mkdir(exist_ok=True)
    all_entries = []
    repo_stats = {}
    for repo in sorted(selected, key=lambda r: (-REPO_STARS.get(r, 0), r)):
        ent = []
        walk_repo(ent, repo, repos_dir, checkpoint, path_aliases)
        repo_stats[repo] = len(ent)
        all_entries.extend(ent)
        logging.getLogger(__name__).info("[extract] %-32s %6d", repo, len(ent))

    # Daily-Interview-Question 的题目主体在 GitHub Issues 中，用 gh api 抓取
    n_issues = fetch_issues(all_entries) if fetch_issues is not None else 0
    if n_issues:
        repo_stats["Daily-Interview-Question"] += n_issues
        logging.getLogger(__name__).info("[issues] Daily-Interview-Question %6d", n_issues)

    logging.getLogger(__name__).info("[total] raw entries = %d", len(all_entries))
    kept, removed = dedupe(all_entries, checkpoint)
    logging.getLogger(__name__).info("[dedupe] kept = %d, removed = %d", len(kept), len(removed))

    # 每仓库去重后的题目清单
    per_repo = defaultdict(list)
    for e in kept:
        per_repo[e["repo"]].append(e)
    by_repo_dir = out_dir / "by_repo"
    for repo, items in sorted(per_repo.items()):
        items.sort(key=lambda e: e["path"])
        write_md(by_repo_dir / ("%s.md" % repo), "%s 去重后题目" % repo, items)

    # 跨仓库合并（高频题目排前）
    def merge_sort_key(e):
        n_src = len(e.get("srcs", [e["repo"]]))
        stars = max(REPO_STARS.get(r, 0) for r in e.get("srcs", [e["repo"]]))
        return (-n_src, -stars, e["norm"])

    questions = [e for e in kept if e["kind"] == "question"]
    knowledges = [e for e in kept if e["kind"] != "question"]
    write_md(out_dir / "merged_questions.md", "八股面试题库（%d 个来源去重合并）" % len(selected),
             sorted(questions, key=merge_sort_key))
    write_md(out_dir / "merged_knowledge.md", "面试知识点清单（%d 个来源去重合并）" % len(selected),
             sorted(knowledges, key=merge_sort_key))

    # Both question and knowledge headings are searchable training material.
    # Exporting only interrogative titles drops e.g. HTTP caching/connection
    # pools from reference-style sources. Import still validates each title and
    # excerpt before the generator may use it.
    export = []
    for i, e in enumerate(sorted(kept, key=merge_sort_key), 1):
        checkpoint()
        export.append({
            "id": "bank-%04d" % i,
            "knowledge_point": e["text"],
            "tags": ["interview-bank", e["repo"]],
            "keywords": [],
            "difficulty": "medium",
            "question": e["text"],
            "options": {},
            "answer": "",
            "analysis": "来源: %s" % ", ".join(e.get("srcs", [e["repo"]])),
            "source_paths": ["%s/%s" % (r, e["path"]) for r in e.get("srcs", [e["repo"]])],
            "src_count": len(e.get("srcs", [e["repo"]])),
        })
    (out_dir / "questions_export.json").write_text(
        json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")

    # 统计报告
    rep = ["# 题库去重报告", ""]
    rep.append("## 各仓库原始提取条目数")
    rep.append("| 仓库 | 原始条目 |")
    rep.append("|---|---|")
    for repo, n in sorted(repo_stats.items(), key=lambda kv: -kv[1]):
        rep.append("| %s | %d |" % (repo, n))
    rep.append("")
    rep.append("## 去重统计")
    rep.append("- 原始条目总数: %d" % len(all_entries))
    rep.append("- 保留条目: %d（题目 %d / 知识点 %d）" % (len(kept), len(questions), len(knowledges)))
    rep.append("- 剔除条目: %d（精确重复 %d / 近重复 %d）" % (
        len(removed),
        sum(1 for r in removed if r["why"] == "exact"),
        sum(1 for r in removed if r["why"] == "near")))
    rep.append("")
    multi = [e for e in kept if len(e.get("srcs", [e["repo"]])) > 1]
    rep.append("## 多仓库复现的高频知识点（跨仓库重复，已合并保留一份）: %d 条" % len(multi))
    for e in sorted(multi, key=lambda e: -len(e["srcs"]))[:200]:
        rep.append("- **%s**" % e["text"])
        rep.append("  - 出现于: %s" % ", ".join(e["srcs"]))
    (out_dir / "report.md").write_text("\n".join(rep), encoding="utf-8")

    # 原始条目与剔除明细
    with (out_dir / "removed_details.jsonl").open("w", encoding="utf-8") as f:
        for r in removed:
            f.write(json.dumps({
                "repo": r["repo"], "path": r["path"], "why": r["why"],
                "text": r["text"], "dup_of_repo": r["dup_of"]["repo"],
                "dup_of_text": r["dup_of"]["text"],
            }, ensure_ascii=False) + "\n")
    logging.getLogger(__name__).info("[done] outputs -> %s", out_dir)

    return export
