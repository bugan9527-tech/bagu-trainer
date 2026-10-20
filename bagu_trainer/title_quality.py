# -*- coding: utf-8 -*-
"""题库标题清洗与噪声拒绝。

目标：
  - 清理 Markdown 链接、HTML 链接、装饰 emoji 和多余空白；
  - 保留正常 Java 注解（如 @Transactional）和短技术标题（如“什么是堆”）；
  - 拒绝 GitHub 评论、纯 URL、站点/公众号元信息、泛化短语、长代码残留。

clean_title() 只负责判断单条标题；调用方必须把拒绝原因写入清理报告，
不得静默丢弃。
"""
import html
import re
import unicodedata


JAVA_ANNOTATIONS = {
    "After", "Async", "Autowired", "Bean", "Before", "Cacheable",
    "Component", "Configuration", "Controller", "Deprecated",
    "FunctionalInterface",
    "Inject", "Lazy", "Mock", "MockBean", "Override", "Qualifier",
    "Repository", "RequestMapping", "Resource", "Service", "SuppressWarnings",
    "Test", "Transactional", "Value",
}

_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK_RE = re.compile(
    r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\([^)]*\)")
_HTML_LINK_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.I | re.S)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_PURE_URL_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.I)
_AT_PREFIX_RE = re.compile(r"^@([A-Za-z_][A-Za-z0-9_.]*)\b")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_DECOR_PREFIX_RE = re.compile(
    r"^[\s\u2600-\u27bf\u2b50\u2b55\U0001f000-\U0001faff\ufe0f]+")
_SPACE_RE = re.compile(r"\s+")
_PROMO_RE = re.compile(r"公众号")
_SITE_META_RE = re.compile(
    r"(?:JavaGuide|本站).*(?:PDF|付费|订阅|公众号|作者|网站|安装|怎么用)",
    re.I,
)
_CODE_START_RE = re.compile(
    r"^(?:if|for|while|let|const|var|def|class|public|private|protected|"
    r"import|from|#include|define|function)\b",
    re.I,
)

# 规范化（去掉标点和空白）后精确匹配，避免误伤“什么是堆”等正常短标题。
GENERIC_TITLES = {
    "怎么上手", "怎么安装", "如何加入", "如何获取", "如何选择",
    "到底怎么选", "具体怎么学", "分层怎么放", "怎么维护", "是否收费",
    "如何求路径", "怎么组合", "个人理解", "也可以这样", "两种解答",
    "2种解决", "98道题", "使用生成器", "只针对本题", "两个变式",
    "减小规模", "action行动你做了什么",
}


def _loose_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)


def _is_mostly_english(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) > 0.6


def clean_title(raw: str) -> dict:
    """返回 {cleaned, rejected, reason, original}。"""
    original = str(raw or "")
    text = html.unescape(original)
    text = _ZERO_WIDTH_RE.sub("", text).strip()
    text = _DECOR_PREFIX_RE.sub("", text).strip()
    text = _MD_IMAGE_RE.sub(r"\1", text)
    text = _HTML_LINK_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    text = text.replace("**", "").replace("__", "")
    text = _SPACE_RE.sub(" ", text).strip()

    if not text:
        return {"cleaned": "", "rejected": True,
                "reason": "empty", "original": original}
    if _PURE_URL_RE.fullmatch(text):
        return {"cleaned": text, "rejected": True,
                "reason": "url_only", "original": original}

    match = _AT_PREFIX_RE.match(text)
    if match and match.group(1) not in JAVA_ANNOTATIONS:
        return {"cleaned": text, "rejected": True,
                "reason": "github_comment", "original": original}

    loose = _loose_key(text)
    if loose in GENERIC_TITLES:
        return {"cleaned": text, "rejected": True,
                "reason": "generic_title", "original": original}
    if _PROMO_RE.search(text) or _SITE_META_RE.search(text):
        return {"cleaned": text, "rejected": True,
                "reason": "site_meta", "original": original}
    if len(text) > 190 or (
            len(text) > 100
            and len(re.findall(r"[?？]", text)) >= 3
            and not _is_mostly_english(text)):
        return {"cleaned": text, "rejected": True,
                "reason": "too_long", "original": original}
    if len(text) > 40 and _CODE_START_RE.match(text) and re.search(r"[{}();=]", text):
        return {"cleaned": text, "rejected": True,
                "reason": "code_fragment", "original": original}

    return {"cleaned": text, "rejected": False,
            "reason": "ok", "original": original}
