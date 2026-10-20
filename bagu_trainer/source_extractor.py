# -*- coding: utf-8 -*-
"""来源正文提取与质量判定。

提取器只读取 interview-banks/repos 内的真实 Markdown。路径解析后必须仍在
repos 根目录内，阻断 ../、绝对路径和符号链接越界；找不到标题时绝不跨文件
拼接正文，只在同一来源文件内尝试可靠的父章节。

标题识别覆盖：
  - Markdown ATX / Setext 标题、HTML 标题；
  - 行首加粗标题；
  - 1. / （一） / 一、 / 第 1 章 等编号标题；
  - 标题里的行内代码、HTML 锚点和 markdown 链接。

质量判定与命中状态分离：
  - source_status=ok 只表示“找到了可提取的章节”；
  - usable_excerpt / excerpt_quality 才决定是否允许进入 LLM。
纯链接、图片、代码残片、推广文案和不足最小有效长度的正文均标记为 weak。
"""
import html
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from .config import PROJECT_ROOT

REPOS_DEFAULT = PROJECT_ROOT / "interview-banks" / "repos"
EXCERPT_LIMIT_DEFAULT = 1500

# 有效正文最少字符数（只统计中文、英文和数字）。低于该阈值的片段不进入 LLM。
MIN_USABLE_EXCERPT_CHARS = 50
MIN_VALID_EXCERPT_CHARS = MIN_USABLE_EXCERPT_CHARS
MIN_LLM_EXCERPT_CHARS = MIN_USABLE_EXCERPT_CHARS

_HEAD_RE = re.compile(r"^(#{1,6})(?:[ \t]+|$)(.*?)(?:[ \t]+#+)?[ \t]*$")
_HTML_HEAD_RE = re.compile(
    r"^<h([1-6])\b[^>]*>(.*?)</h\1>[ \t]*$", re.I | re.S)
_SETEXT_RE = re.compile(r"^[ \t]*(=+|-+)[ \t]*$")
_BOLD_HEAD_RE = re.compile(r"^(?:\*\*|__)(.+?)(?:\*\*|__)[ \t]*$")
_NUMBERED_HEAD_RE = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+){0,3}[.、．)]"
    r"|[一二三四五六七八九十百]{1,3}[、.．)]"
    r"|[（(][一二三四五六七八九十百0-9]{1,3}[）)]"
    r"|第[一二三四五六七八九十百0-9]{1,4}[章节部分篇]"
    r")\s*\S"
)
_NUMBER_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十百0-9]{1,4}[章节部分篇]"
    r"|[（(][一二三四五六七八九十百0-9]{1,3}[）)]"
    r"|\d+(?:\.\d+){0,3}[.、．)]"
    r"|[一二三四五六七八九十]{1,3}[、.．)]"
    r")\s*"
)

_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.I)
_ANCHOR_RE = re.compile(r"</?a\b[^>]*>", re.I)
_HTML_FULL_ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.I | re.S)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BARE_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_FENCED_CODE_RE = re.compile(
    r"(```|~~~)[^\n]*\n.*?(?:\1|$)", re.S)
_BADGE_LINE_RE = re.compile(
    r"^\s*(?:!\[[^\]]*\]\([^)]*\)\s*)+$|"
    r"^\s*(?:<img\b[^>]*>\s*)+$|"
    r"^\s*\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)\s*$",
    re.I,
)
_PROMO_RE = re.compile(
    r"关注.{0,12}(?:公众号|微信|账号)|扫码|二维码|点赞|收藏|转发|打赏|赞助"
    r"|广告|推广|加群|QQ\s*群|微信群|知识星球|付费|购买|订阅|RSS"
    r"|支持作者|商务合作|联系作者|版权|转载|欢迎关注|点击.{0,8}(?:阅读|查看)"
    r"|Star.{0,12}(?:支持|收藏)|最新更新|推荐.{0,8}订阅",
    re.I,
)


def _strip_anchor_markup(text: str) -> str:
    return _ANCHOR_RE.sub("", text or "")


def normalize_title(text: str) -> str:
    """标题归一化：去编号、链接/加粗/代码/HTML/锚点/标点/空白，英文小写。"""
    t = html.unescape(str(text or ""))
    t = unicodedata.normalize("NFKC", t)
    t = _HTML_COMMENT_RE.sub("", t)
    t = _IMAGE_RE.sub("", t)
    t = _LINK_RE.sub(r"\1", t)
    t = _strip_anchor_markup(t)
    t = _HTML_TAG_RE.sub("", t)
    t = t.replace("`", "")
    t = re.sub(r"^\s*#{1,6}\s*", "", t)
    t = re.sub(r"\s*#+\s*$", "", t)
    t = _NUMBER_PREFIX_RE.sub("", t, count=1)
    t = re.sub(r"\{[#.][^}]*\}", "", t)  # Kramdown / MDX 标题锚点
    # 仅移除标点和空白；保留 +、/ 等具有技术含义的符号。
    t = "".join(
        ch for ch in t
        if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )
    return t.casefold()


def safe_repo_file(repos_root, rel_path):
    """把 source_path 解析为 repos 根下的真实文件；越界/不存在返回 None。"""
    root = Path(repos_root).resolve()
    p = Path(str(rel_path).replace("\\", "/"))
    if p.is_absolute() or p.drive:
        return None
    resolved = (root / p).resolve()
    if resolved.is_file() and resolved != root and root in resolved.parents:
        return resolved
    return None


def _heading_from_line(line: str):
    """解析单行标题，返回 (level, title, kind) 或 None。"""
    body = line.strip()
    if not body:
        return None
    m = _HTML_HEAD_RE.match(body)
    if m:
        return int(m.group(1)), normalize_title(m.group(2)), "html"
    m = _HEAD_RE.match(body)
    if m:
        title = normalize_title(m.group(2))
        return (len(m.group(1)), title, "markdown") if title else None
    m = _BOLD_HEAD_RE.match(body)
    if m and not re.search(r"[。！？.!?]\s*$", m.group(1)):
        title = normalize_title(m.group(1))
        return (7, title, "bold") if title else None
    if (len(body) <= 100 and _NUMBERED_HEAD_RE.match(body)
            and not re.search(r"[。；;]\s*$", body)):
        title = normalize_title(body)
        return (8, title, "numbered") if title else None
    return None


def _parse_headings(lines: list) -> list:
    headings = []
    skip = False
    for i, raw in enumerate(lines):
        if skip:
            skip = False
            continue
        body = raw.strip()
        if not body:
            continue
        # Setext 标题：孤立短正文行 + === / --- 下划线
        if i + 1 < len(lines):
            underline = _SETEXT_RE.match(lines[i + 1])
            prev_blank = i == 0 or not lines[i - 1].strip()
            if (underline and prev_blank and len(body) <= 120
                    and "|" not in body and not _HEAD_RE.match(body)):
                level = 1 if underline.group(1).startswith("=") else 2
                title = normalize_title(body)
                if title:
                    headings.append((i, level, title, "setext"))
                    skip = True
                    continue
        parsed = _heading_from_line(raw)
        if parsed:
            level, title, kind = parsed
            if title:
                headings.append((i, level, title, kind))
    return headings


def _length_threshold(text: str) -> int:
    """中英文标题的可靠包含匹配最小长度。"""
    return 4 if re.search(r"[\u4e00-\u9fff]", text) else 8


def _match_score(candidate: str, target: str):
    """返回匹配度：4=精确，2=具体小节，1=父章节，0=不匹配。"""
    if not candidate or not target:
        return 0
    if candidate == target:
        return 4
    min_len = _length_threshold(target)
    if len(candidate) >= min_len and candidate in target:
        coverage = len(candidate) / max(1, len(target))
        if coverage >= 0.28:
            return 1
    if len(target) >= min_len and target in candidate:
        coverage = len(target) / max(1, len(candidate))
        if coverage >= 0.50:
            return 2
    return 0


def _body_parent(lines: list, headings: list, target: str):
    """标题未精确命中时，定位正文中的题目标题，再回到同一文件的最近父章节。"""
    if len(target) < 4:
        return None
    heading_indexes = {h[0] for h in headings}
    for i, raw in enumerate(lines):
        if i in heading_indexes:
            continue
        plain = normalize_title(raw)
        if not plain or target not in plain:
            continue
        parent = None
        for h in headings:
            if h[0] >= i:
                break
            parent = h
        if parent:
            return parent
    return None


def _find_heading_match(lines: list, headings: list, target: str):
    exact = [h for h in headings if h[2] == target]
    if exact:
        # 目录里的编号行也像标题；同标题重复时优先真正的 Markdown 标题，
        # 其次加粗标题，最后才用编号行。
        priority = {"markdown": 3, "html": 3, "setext": 3, "bold": 2,
                    "numbered": 1}
        return max(exact, key=lambda h: (priority.get(h[3], 0), h[0])), "exact"
    parent = _body_parent(lines, headings, target)
    if parent:
        return parent, "body_parent"
    scored = []
    for h in headings:
        score = _match_score(h[2], target)
        if score > 0:
            scored.append((score, h[1], h[0], h))
    if not scored:
        return None, "none"
    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return scored[0][3], "parent"


def _is_terminating_heading(level: int, kind: str, found_level: int,
                            found_kind: str) -> bool:
    if found_level <= 6:
        # Markdown 章节中的加粗/编号行通常是子标题或列表，不作为章节边界。
        return kind in ("markdown", "html", "setext") and level <= found_level
    if found_level == 7:
        return kind in ("markdown", "html", "setext", "bold")
    return kind in ("markdown", "html", "setext", "bold", "numbered")


def _looks_like_code_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith(("```", "~~~", "$ ", ">>> ", "... ")):
        return True
    if re.match(
        r"^(?:def|class|function|const|let|var|import|from|public|private|"
        r"protected|static|if|for|while|return|SELECT|INSERT|UPDATE|DELETE|"
        r"CREATE|ALTER|curl|pip|npm|docker|git|kubectl|redis-cli|mysql|"
        r"python|java|go\s|#include|using\s+namespace|package\s)",
        s, re.I,
    ):
        return not re.search(r"[\u4e00-\u9fff]", s)
    if re.search(r"[;{}]\s*$", s) and not re.search(r"[\u4e00-\u9fff]", s):
        return True
    if (re.match(r"^[A-Za-z_][A-Za-z0-9_.]*\s*=\s*", s)
            and not re.search(r"[\u4e00-\u9fff]", s)):
        return True
    if (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*\([^{};]*\)\s*;?", s)
            and not re.search(r"[\u4e00-\u9fff]", s)):
        return True
    if s.startswith(("{", "}", "[", "]")) and not re.search(
            r"[\u4e00-\u9fff]", s):
        return True
    return False


def _plain_line_for_quality(line: str):
    """返回 (可计入说明文的文本, 代码字符数, 链接字符数)。"""
    raw = html.unescape(line or "")
    code_chars = sum(len(s) for s in _INLINE_CODE_RE.findall(raw))
    link_chars = sum(len(s) for s in _BARE_URL_RE.findall(raw))
    link_chars += sum(len(m.group(0)) for m in _LINK_RE.finditer(raw))
    html_links = list(_HTML_FULL_ANCHOR_RE.finditer(raw))
    link_chars += sum(len(m.group(0)) for m in html_links)
    s = _HTML_COMMENT_RE.sub("", raw)
    s = _HTML_IMAGE_RE.sub(" ", s)
    s = _IMAGE_RE.sub(" ", s)
    s = _HTML_FULL_ANCHOR_RE.sub(" ", s)
    s = _LINK_RE.sub(" ", s)          # 链接文字也不算解释性正文
    s = _BARE_URL_RE.sub(" ", s)
    s = _strip_anchor_markup(s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = _INLINE_CODE_RE.sub(" ", s)
    s = re.sub(r"^\s*>\s?", "", s)
    s = re.sub(r"^\s*(?:[-+*]|\d+[.)、])\s+", "", s)
    s = re.sub(r"[*_~`#>|]+", " ", s)
    return s.strip(), code_chars, link_chars


def assess_excerpt(text: str, min_chars: int = MIN_USABLE_EXCERPT_CHARS) -> dict:
    """判定片段是否包含足够且可用的解释性正文。

    返回 usable / quality / reason / effective_chars / code_chars /
    link_chars / promo_chars。quality 为 strong、weak 或 empty。
    """
    raw = str(text or "")
    if not raw.strip():
        return {"usable": False, "quality": "empty", "reason": "empty",
                "effective_chars": 0, "code_chars": 0, "link_chars": 0,
                "promo_chars": 0, "min_chars": min_chars}

    fenced = [m.group(0) for m in _FENCED_CODE_RE.finditer(raw)]
    code_chars = sum(len(s) for s in fenced)
    body = _FENCED_CODE_RE.sub("\n", raw)
    prose_lines, promo_lines = [], []
    question_chars = 0
    link_chars = 0
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if _BADGE_LINE_RE.match(s):
            continue
        if _looks_like_code_line(s):
            code_chars += len(s)
            continue
        cleaned, line_code, line_links = _plain_line_for_quality(s)
        code_chars += line_code
        link_chars += line_links
        if not cleaned:
            continue
        if _PROMO_RE.search(cleaned):
            promo_lines.append(cleaned)
            continue
        prose_lines.append(cleaned)
        if re.search(r"[?？]\s*$", cleaned):
            question_chars += len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]",
                                             cleaned))

    prose = "\n".join(prose_lines)
    effective_chars = len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", prose))
    promo_chars = sum(len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", s))
                      for s in promo_lines)
    result = {
        "usable": False, "quality": "weak", "reason": "too_short",
        "effective_chars": effective_chars, "code_chars": code_chars,
        "link_chars": link_chars, "promo_chars": promo_chars,
        "question_chars": question_chars, "min_chars": min_chars,
    }
    if effective_chars == 0:
        if link_chars:
            result["reason"] = "link_only"
        elif code_chars:
            result["reason"] = "code_only"
        elif promo_chars:
            result["reason"] = "promo_only"
        else:
            result["reason"] = "image_or_markup_only"
        return result
    if effective_chars < min_chars:
        if promo_lines and promo_chars >= effective_chars:
            result["reason"] = "promo_only"
        return result
    if (question_chars >= effective_chars * 0.60
            and effective_chars - question_chars < min_chars):
        result["reason"] = "question_list"
        return result
    if code_chars >= 40 and code_chars / max(1, code_chars + effective_chars) > 0.75:
        result["reason"] = "code_dominant"
        return result
    if len(prose_lines) == 0:
        result["reason"] = "not_explanatory"
        return result
    result.update({"usable": True, "quality": "strong", "reason": "ok"})
    return result


def _extract_body(lines: list, start: int, found_level: int,
                  found_kind: str, limit: int) -> str:
    parts, chars = [], 0
    for raw in lines[start + 1:]:
        body = raw.strip()
        if body:
            parsed = _heading_from_line(body)
            if parsed and _is_terminating_heading(
                    parsed[0], parsed[2], found_level, found_kind):
                break
            if _BADGE_LINE_RE.match(body) or _IMAGE_RE.match(body):
                continue
            if _HTML_IMAGE_RE.fullmatch(body) or _ANCHOR_RE.fullmatch(body):
                continue
            if _PROMO_RE.search(body) and len(body) < 120:
                continue
        parts.append(raw.rstrip())
        chars += len(raw) + 1
        if chars >= limit:
            break
    return "\n".join(parts).strip()[:limit].strip()


def _extract_from_parsed(lines: list, headings: list, target: str,
                         limit: int) -> dict:
    """在一篇已解析标题的文档中完成定位与正文提取。"""
    if not target:
        return {"excerpt": "", "match": "none", "match_title": "",
                "match_level": 0, "match_kind": "", "match_line": -1}
    found, match = _find_heading_match(lines, headings, target)
    if not found:
        return {"excerpt": "", "match": "none", "match_title": "",
                "match_level": 0, "match_kind": "", "match_line": -1}
    line_i, level, heading_title, kind = found
    excerpt = _extract_body(lines, line_i, level, kind, limit)
    return {
        "excerpt": excerpt,
        "match": match,
        "match_title": heading_title,
        "match_level": level,
        "match_kind": kind,
        "match_line": line_i,
    }


def extract_excerpt_details(md_text: str, title: str,
                            limit: int = EXCERPT_LIMIT_DEFAULT) -> dict:
    """在单篇 Markdown 中定位标题并返回正文及匹配元数据。"""
    lines = str(md_text or "").splitlines()
    return _extract_from_parsed(
        lines, _parse_headings(lines), normalize_title(title), limit)


def extract_excerpt(md_text: str, title: str,
                    limit: int = EXCERPT_LIMIT_DEFAULT) -> str:
    """在单篇 Markdown 文本中定位标题并提取正文片段；找不到返回空串。"""
    return extract_excerpt_details(md_text, title, limit=limit)["excerpt"]


@lru_cache(maxsize=512)
def _read_text_cached(path_str: str) -> str:
    try:
        return Path(path_str).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


@lru_cache(maxsize=512)
def _read_lines_cached(path_str: str) -> tuple:
    return tuple(_read_text_cached(path_str).splitlines())


@lru_cache(maxsize=512)
def _headings_cached(path_str: str) -> tuple:
    return tuple(_parse_headings(list(_read_lines_cached(path_str))))


def excerpt_for_detailed(repos_root, source_paths, title,
                         limit: int = EXCERPT_LIMIT_DEFAULT) -> dict:
    """遍历声明来源，返回带命中/质量元数据的提取结果。

    先在同一文件内精确命中；没有精确命中时才使用该文件内的可靠父章节。
    多个来源均精确命中时优先有效正文更长的一个，但绝不把 A 文件的标题与
    B 文件的正文拼接。
    """
    paths = list(source_paths or [])
    if not paths:
        return {"excerpt": "", "status": "none", "used": None,
                "quality": "empty", "usable": False, "reason": "no_source",
                "match": "none", "match_title": "", "effective_chars": 0}

    exact_results, parent_results = [], []
    for rel in paths:
        fp = safe_repo_file(repos_root, rel)
        if fp is None:
            continue
        lines = _read_lines_cached(str(fp))
        if not lines:
            continue
        detail = _extract_from_parsed(
            list(lines), list(_headings_cached(str(fp))),
            normalize_title(title), limit)
        if not detail["excerpt"]:
            continue
        quality = assess_excerpt(detail["excerpt"])
        item = {
            "excerpt": detail["excerpt"],
            "status": "ok",
            "used": rel,
            "quality": quality["quality"],
            "usable": quality["usable"],
            "reason": quality["reason"],
            "match": detail["match"],
            "match_title": detail["match_title"],
            "effective_chars": quality["effective_chars"],
            "quality_detail": quality,
        }
        if detail["match"] == "exact":
            exact_results.append(item)
        else:
            parent_results.append(item)

    pool = exact_results or parent_results
    if not pool:
        return {"excerpt": "", "status": "missing", "used": None,
                "quality": "empty", "usable": False, "reason": "title_not_found",
                "match": "none", "match_title": "", "effective_chars": 0}
    pool.sort(key=lambda x: (
        x["usable"], x["effective_chars"], len(x["excerpt"]),
        1 if x["match"] == "exact" else 0,
    ), reverse=True)
    return pool[0]


def excerpt_for(repos_root, source_paths, title,
                limit: int = EXCERPT_LIMIT_DEFAULT):
    """兼容接口：返回 (excerpt, status, used_rel_path)。

    status: ok（找到章节）/ missing（路径或标题未命中）/ none（无来源路径）。
    片段是否可用必须另行检查 excerpt_quality / usable_excerpt。
    """
    detail = excerpt_for_detailed(repos_root, source_paths, title, limit=limit)
    return detail["excerpt"], detail["status"], detail["used"]
