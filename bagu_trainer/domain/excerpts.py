"""Pure assessment of explanatory source material; no file access."""
import html
import re

MIN_USABLE_EXCERPT_CHARS = 50

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
