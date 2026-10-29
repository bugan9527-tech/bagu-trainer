# -*- coding: utf-8 -*-
"""为每道题生成离线概念档案（concept profile），全程确定性、无 LLM。

规则（按顺序）：
  1. 概念候选优先级：primary_knowledge_tag、knowledge_tags、标题技术词、
     来源正文概念词；摘要只做补充；
  2. 从 source_excerpt / 真实 analysis（非「来源:」占位）中选出与
     标签词或题干词同现的解释性句子；主库没有答案时，该题命中的
     离线验证回退题（fallback_bank.find_fallback ≥80 分）的解析作为其
     已有 answer/analysis 参与提取——只抽同现句与概念词，不落完整答案；
     跨域次级标签与来源主题不进入档案；
  3. gram 级过滤「应用场/用场景/框架中/介绍一/么设计」等泛词、助词与
     标点粘连片段，长词优先、子 gram 去重；
  4. 概念摘要只保存与主标签或标题核心概念明确共现的解释句（<=300 字符）；
  5. 用受控标签拆词/同义词补足 3-10 个相关词；仍无可靠来源时标记 low。

输出 data/question_concepts.json（按题目 ID 为键）与 data/concept_profile_report.json。
不修改 data/questions.json。
"""
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from bagu_trainer.infrastructure.config import USER_DATA_ROOT

PROJECT = USER_DATA_ROOT

from bagu_trainer.infrastructure.concepts import validate_profiles  # noqa: E402
from bagu_trainer.infrastructure.fallback_bank import find_fallback  # noqa: E402
from bagu_trainer.domain.knowledge_taxonomy import RULES
from bagu_trainer.domain.knowledge_taxonomy import TAG_DOMAIN  # noqa: E402
from bagu_trainer.infrastructure.matcher import STOP_GRAMS
from bagu_trainer.infrastructure.matcher import tokenize  # noqa: E402

CONCEPTS_PATH = PROJECT / "data" / "question_concepts.json"
REPORT_PATH = PROJECT / "data" / "concept_profile_report.json"

# 泛词：不进入概念词表（STOP_GRAMS 之外再补一批低信息词）。
# 全词拒收只处理“数据/系统/接口”等无上下文词，避免误伤“数据库/操作系统”等复合术语。
GENERIC_TERMS = set(STOP_GRAMS) | {
    "场景", "上下文", "内容", "正文", "问题", "原因", "解决", "方案", "方法",
    "方式", "步骤", "流程", "机制", "原理", "作用", "区别", "联系", "关系",
    "概念", "知识", "技术", "业务", "系统", "数据", "用户", "接口", "服务",
    "应用", "代码", "实现", "设计", "处理", "使用", "需要", "可以", "不要",
    "不能", "没有", "不会", "是否", "什么", "如何", "哪些", "为什么", "怎么",
    "我们", "他们", "自己", "一个", "一些", "一种", "这个", "那个", "这样",
    "那样", "以及", "或者", "但是", "因为", "所以", "如果", "就是", "还是",
    "不是", "针对", "相关", "不同", "相同", "主要", "之间", "很多", "一定",
    "可能", "情况", "部分", "结果", "说明", "第一", "基础", "常见", "常用",
    "一般", "进行", "通过", "存在", "发生", "出现", "导致", "保证", "避免",
    "条件", "参数", "字段", "记录", "业务侧",
    "实际", "倾向", "题目", "项目", "判断", "选择", "时候", "适合",
    "回答", "下面", "以下", "上面", "大家", "默认", "必要", "建议",
    "推荐", "直接", "很少", "几乎", "我们", "自带",
}

EXTRA_GENERIC_EN = {
    "my", "db", "question", "qa", "type", "those", "there", "where",
}

# gram 级噪声：只要词条包含这些问句/泛化片段就整体丢弃。
# 这里刻意覆盖「应用场 / 用场景 / 框架中 / 么设计」等切词残片，
# 不能只过滤完整短语。
_GENERIC_FRAGMENTS = (
    "应用", "场景", "用场", "框架", "介绍", "如下", "一种", "这个",
    "那个", "这样", "那样", "么设计", "些好处", "么慢", "些典型",
    "建议直", "际应用", "种情况", "比如", "你更倾", "于使用",
    "使用场", "典型使", "型使用", "些需要", "么工", "么实", "么选",
    "么处", "何设", "何实", "何工", "些类", "些优", "样设", "个问",
    "问题", "原因", "方案", "方法", "方式", "步骤", "流程",
    "绍一", "介一", "下动", "你更", "景有", "景如", "常见应",
    "典型", "默认", "认使", "架中", "更倾", "必要", "要时", "型使",
    "有什", "是什", "为什", "才用", "时才", "实际", "倾向", "题目",
    "项目", "判断", "选择", "时候", "适合", "回答", "下面", "以下",
    "上面", "大家", "向于", "于使", "际应",
    "不建", "议直", "接使", "直接使", "不推", "荐使", "们很",
    "很少", "几乎", "要原",
    "自带",
)

# 助词/标点/代码符号粘连的 gram 为噪声（「与数据」「或方案」「调用(`x`)」）。
# 加号与井号单独放行，用于 C++ / C# 这类合法语言名。
_JUNK_TERM_CHARS = set(
    "的了呢吗啊吧呀哦把被这那哪么些种样者与及和或为了在是至"
    "()（）[].，。:：;；、/\\|=<>!$%^&*{}"
)

# 受控标签拆词/同义词。顺序即标签规范化优先级；这些词只与主标签或标题相关时使用。
TAG_SYNONYMS = {
    "MCP 与工具调用": ("MCP", "工具调用", "结构化输出"),
    "AI Agent": ("AI Agent", "智能体", "任务编排"),
    "AI 编程与研发效能": ("AI 编程", "研发效能", "代码生成"),
    "RAG 与检索增强": ("RAG", "向量检索", "文档召回"),
    "提示工程与上下文": ("提示工程", "上下文", "提示词"),
    "AI 评测与可观测性": ("AI 评测", "基准测试", "可观测性"),
    "AI 安全与治理": ("AI 安全", "提示注入", "权限控制"),
    "语音 AI": ("语音识别", "语音合成", "实时语音"),
    "大模型基础与训练": ("大模型", "Transformer", "模型训练"),
    "机器学习与深度学习": ("机器学习", "深度学习", "神经网络"),
    "Redis": ("Redis", "缓存", "键值数据库"),
    "MySQL": ("MySQL", "InnoDB", "关系数据库"),
    "MongoDB": ("MongoDB", "文档数据库", "索引"),
    "分布式事务": ("分布式事务", "两阶段提交", "最终一致性"),
    "分布式锁": ("分布式锁", "锁续期", "互斥"),
    "高并发与性能": ("高并发", "性能优化", "吞吐量"),
    "消息队列": ("消息队列", "Kafka", "消费组"),
    "分布式系统与高可用": ("分布式系统", "高可用", "一致性"),
    "数据库基础": ("数据库", "SQL", "事务"),
    "缓存": ("缓存", "缓存一致性", "过期策略"),
    "TLS 与加密": ("TLS", "加密", "证书"),
    "Web 安全": ("Web 安全", "鉴权", "CSRF"),
    "HTTP 与 Web": ("HTTP", "Web", "状态码"),
    "TCP/IP": ("TCP", "UDP", "三次握手"),
    "DNS 与网络基础": ("DNS", "IP 地址", "网络层"),
    "浏览器与前端基础": ("浏览器", "DOM", "事件循环"),
    "JavaScript": ("JavaScript", "闭包", "原型链"),
    "React": ("React", "组件", "Hooks"),
    "Vue": ("Vue", "组件", "响应式"),
    "TypeScript": ("TypeScript", "类型系统", "类型推断"),
    "CSS 与布局": ("CSS", "布局", "Flex"),
    "Java 并发": ("Java", "并发", "线程池"),
    "JVM": ("JVM", "垃圾回收", "类加载"),
    "Java 集合": ("Java", "集合", "HashMap"),
    "Spring": ("Spring", "Bean", "IoC"),
    "MyBatis": ("MyBatis", "SQL 映射", "Mapper"),
    "Java 基础": ("Java", "JVM", "面向对象"),
    "C/C++": ("C语言", "C++", "类型系统"),
    "Python": ("Python", "GIL", "迭代器"),
    # "子问题" is rejected as a generic term; retain three valid technical
    # anchors even for reference headings whose body contains only code.
    "动态规划": ("动态规划", "状态转移", "最优子结构"),
    "图论与搜索": ("图论", "深度优先搜索", "广度优先搜索"),
    "排序与二分": ("排序", "二分查找", "时间复杂度"),
    "算法与数据结构": ("算法", "数据结构", "复杂度"),
    "操作系统": ("操作系统", "进程", "内存管理"),
    "构建工具": ("构建工具", "Maven", "Gradle"),
    "Linux 与运维": ("Linux", "Shell", "容器"),
    "测试与质量": ("测试", "单元测试", "覆盖率"),
    "Git 与协作": ("Git", "分支", "合并"),
    "系统设计与架构": ("系统设计", "架构设计", "高可用"),
    "简历与面试表达": ("面试表达", "简历", "STAR"),
    "前端开发": ("前端", "组件", "浏览器"),
    "后端面试基础": ("后端", "数据库", "网络"),
    "软件工程与协作": ("软件工程", "协作", "代码质量"),
    "计算机基础": ("计算机基础", "数据结构", "操作系统"),
    "Java 与后端": ("Java", "后端", "JVM"),
    "其他技术知识": ("技术知识", "编程", "工程实践"),
}

TAG_DOMAIN_EXTRA = {
    "前端开发": "前端",
    "后端面试基础": "后端与架构",
    "软件工程与协作": "工程实践",
    "计算机基础": "计算机基础",
    "Java 与后端": "Java",
    "其他技术知识": "其他",
}

_RE_NPE_CORE = re.compile(
    r"nullpointerexception|\bnpe\b|空指针异常", re.I,
)
_NPE_SOURCE_TERMS = {
    "null", "pointer", "npe", "nullpointerexception", "空指", "针异",
}

KEYWORD_DOMAIN = {}
for _rule_tag, _rule_domain, _rule_keywords in RULES:
    for _rule_keyword in _rule_keywords:
        KEYWORD_DOMAIN.setdefault(_rule_keyword.lower(), _rule_domain)

# 解释性句子标记：出现这些短语的句子更可能是概念解释
_EXPLAIN_MARKER = re.compile(
    r"是指|就是|所谓|指的|意味着|用于|用来|叫做|称为|因为|由于|为了"
    r"|起到|负责|本质上|好处|坏处|缺点|核心在于|关键在于"
)

_SENT_SPLIT = re.compile(r"[。！？!?；;\n]+")

_MAX_TERMS = 10
_MIN_TERMS = 3
_MAX_SUMMARY = 300
_MIN_SENT_CHARS = 10
_ANALYSIS_MIN_CHARS = 30

_TECH_SUFFIXES = (
    "缓存", "队列", "异常", "索引", "数据库", "协议", "模型", "算法",
    "线程", "进程", "函数", "对象", "接口", "组件", "模式", "系统",
    "服务", "文件", "网络", "指针", "引用", "类型", "语言", "表",
    "树", "锁", "池", "器",
)


def _normalize_term(t: str) -> str:
    t = re.sub(r"[`*_#>]+", "", str(t or "")).strip()
    t = re.sub(r"^\s*(?:\d+[.)、]\s*)", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" \t\r\n,，。:：;；-/\\")


def _keep_term(t: str) -> bool:
    """拒收泛化 gram、问句残片、助词/标点/代码符号粘连。"""
    t = _normalize_term(t)
    low = t.lower()
    if len(t) < 2 or len(t) > 32:
        return False
    if low in GENERIC_TERMS or low in STOP_GRAMS or low in EXTRA_GENERIC_EN:
        return False
    if t.isdigit() or re.fullmatch(r"[A-Za-z]", t):
        return False
    if any(fragment in low for fragment in _GENERIC_FRAGMENTS):
        return False
    if any(c in _JUNK_TERM_CHARS for c in t):
        return False
    if t[0] in "么些种样个" or t[-1] in "么些种样":
        return False
    if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", t):
        return False
    return True


def _tag_terms(tag: str) -> list:
    """标签规范化拆词；受控同义词缺失时退回标签自身的有效 gram。"""
    tag = (tag or "").strip()
    if not tag:
        return []
    mapped = list(TAG_SYNONYMS.get(tag, ()))
    if not mapped:
        mapped = [tag] + tokenize(tag)
    out = []
    for term in mapped:
        term = _normalize_term(term)
        if _keep_term(term) and term.lower() not in {x.lower() for x in out}:
            out.append(term)
    return out


def _tag_domain(tag: str, fallback: str = "") -> str:
    return TAG_DOMAIN.get(tag) or TAG_DOMAIN_EXTRA.get(tag) or fallback


def _contains_keyword(text: str, keyword: str) -> bool:
    """与 knowledge_taxonomy 的英文边界规则一致，避免 java 命中 javascript。"""
    text = (text or "").lower()
    keyword = (keyword or "").lower()
    if not keyword:
        return False
    if re.fullmatch(r"[a-z0-9_.+/ -]+", keyword):
        return re.search(
            r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])",
            text,
        ) is not None
    return keyword in text


@lru_cache(maxsize=30000)
def _known_terms(text: str) -> list:
    """按 taxonomy 抽取完整技术词，优先于字符 n-gram 片段。"""
    out = []
    for _tag, _domain, keywords in RULES:
        for keyword in keywords:
            if _contains_keyword(text, keyword):
                term = _normalize_term(keyword)
                if _keep_term(term):
                    out.append(term)
    return list(dict.fromkeys(out))


def _text_terms(text: str) -> list:
    known = _known_terms(text)
    toks = []
    seen = set()
    for order, raw in enumerate(tokenize(text)):
        term = _normalize_term(raw)
        if term.lower() in seen or not _keep_term(term):
            continue
        seen.add(term.lower())
        toks.append((order, term))
    # 技术全词优先；中文 gram 长词优先，避免固定四词预算被
    # 「不建/接使」这类问句碎片先占满。
    toks.sort(key=lambda x: (
        0 if re.search(r"[A-Za-z]", x[1]) else 1,
        -len(x[1]),
        x[0],
    ))
    out = list(known)
    out.extend(term for _order, term in toks)
    return list(dict.fromkeys(t for t in out if _keep_term(t)))


def _is_question_like(sentence: str) -> bool:
    s = sentence.strip()
    if s.endswith(("?", "？")):
        return True
    question_markers = (
        "为什么", "是什么", "有哪些", "怎么做", "如何做",
        "能不能", "怎么选", "如何", "是否",
    )
    return (s.startswith(("-", "*", "•"))
            and any(marker in s for marker in question_markers)
            and not _EXPLAIN_MARKER.search(s))


def _is_navigation_sentence(sentence: str) -> bool:
    s = sentence.strip()
    if s.endswith((":", "：")) and any(
            marker in s for marker in ("如下", "原因", "下面", "以下")):
        return True
    return bool(re.search(r"(?:常见|典型).{0,8}(?:场景|用法)如下", s))


def _blocked_source_fragments(tags: list) -> set:
    blocked = set()
    for tag in tags:
        blocked.add(tag.lower())
        for term in _tag_terms(tag):
            low = term.lower()
            blocked.add(low)
            if re.search(r"[\u4e00-\u9fff]", term):
                blocked.update(
                    t.lower() for t in tokenize(term) if len(t) >= 2
                )
    return {x for x in blocked if len(x) >= 2}


def _source_term_allowed(term: str, primary_domain: str,
                         blocked_fragments: set,
                         blocked_domains: set,
                         direct_npe: bool = False) -> bool:
    low = term.lower()
    if not direct_npe and (
            low in _NPE_SOURCE_TERMS
            or "nullpointer" in low
            or "空指针" in low):
        return False
    if low in blocked_fragments:
        return False
    if any(fragment in low for fragment in blocked_fragments):
        return False
    term_domain = KEYWORD_DOMAIN.get(low)
    if term_domain and term_domain in blocked_domains:
        return False
    # 跨域源正文常见的高风险主题碎片，不允许借 source_excerpt 混入。
    if primary_domain not in ("数据库", "后端与架构"):
        if any(x in low for x in ("数据库", "据库", "数据类", "redis", "sql")):
            return False
    return True


def _sentences(text: str) -> list:
    out = []
    # URL 中的 https/域名/路径不是概念词，先剥离，避免把链接残片写进档案。
    clean = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text or "")
    clean = re.sub(r"https?://[^\s)\]]+", " ", clean)
    for raw in _SENT_SPLIT.split(clean):
        s = raw.strip()
        if len(s) >= _MIN_SENT_CHARS:
            out.append(s)
    return out


def _real_analysis(q: dict) -> str:
    analysis = (q.get("analysis") or "").strip()
    if not analysis or analysis.startswith("来源:"):
        return ""
    if len(analysis) < _ANALYSIS_MIN_CHARS:
        return ""
    return analysis


def _fallback_analysis(q: dict) -> tuple:
    """主库没有答案时，用该题命中的离线验证回退题解析作为已有 answer/analysis。

    回退题库经严格人工验证（fallback_bank），只取与本题可靠关联（≥80 分）的
    一道；绝不把完整答案放入索引——仅返回解析切出的句子参与同现句提取。
    """
    try:
        fb = find_fallback(q)
    except Exception:
        return [], False
    if not fb:
        return [], False
    analysis = (fb.get("analysis") or "").strip()
    if not analysis or analysis.startswith("来源:"):
        return [], False
    return _sentences(analysis), True


def _score_sentence(s: str, primary_tokens: set, title_tokens: set,
                    required_tokens: set = frozenset()) -> float:
    toks = set(tokenize(s))
    primary_overlap = len(toks & primary_tokens)
    title_overlap = len(toks & title_tokens)
    if required_tokens and not (toks & required_tokens):
        return 0.0
    if not primary_overlap and not title_overlap:
        return 0.0
    score = 3.0 * primary_overlap + 2.0 * title_overlap
    if _EXPLAIN_MARKER.search(s):
        score += 1.0
    return score


def _anchor_tokens(terms: list) -> set:
    out = set()
    for term in terms:
        low = term.lower()
        if re.search(r"[a-z0-9]", low):
            out.add(low)
        out.update(t for t in tokenize(term) if _keep_term(t))
    return out


def _append_term(ordered: list, origins: dict, term: str,
                 source: str, source_detail: str = "") -> None:
    term = _normalize_term(term)
    if not _keep_term(term):
        return
    low = term.lower()
    inherited_sources = set()
    for existing in list(ordered):
        existing_low = existing.lower()
        if low == existing_low:
            origins[existing].add(source)
            if source_detail:
                origins[existing].add(source_detail)
            return
        both_cjk = (re.search(r"[\u4e00-\u9fff]", term)
                    and re.search(r"[\u4e00-\u9fff]", existing))
        if both_cjk and existing in term:
            ordered.remove(existing)
            inherited_sources.update(origins.pop(existing, ()))
            continue
        if both_cjk and term in existing:
            return
    ordered.append(term)
    origins[term] = set(inherited_sources)
    origins[term].add(source)
    if source_detail:
        origins[term].add(source_detail)


def _add_bucket(ordered: list, origins: dict, items, source: str,
                cap: int, source_detail: str = "") -> int:
    before = len(ordered)
    for term in items:
        if len(ordered) - before >= cap:
            break
        _append_term(ordered, origins, term, source, source_detail)
    return len(ordered) - before


def extract_profile(q: dict, *, use_fallback=True) -> dict:
    primary = (q.get("primary_knowledge_tag") or "").strip()
    knowledge_tags = list(dict.fromkeys(
        t for t in (q.get("knowledge_tags") or []) if t
    ))
    repo_fallback = "repo_default" in (q.get("knowledge_tag_source") or "")
    title = " ".join(filter(None, (
        q.get("knowledge_point") or "", q.get("question") or ""
    )))

    primary_terms = _tag_terms(primary)
    primary_domain = _tag_domain(
        primary, q.get("knowledge_domain") or "",
    )
    title_terms = _text_terms(title)
    primary_tokens = _anchor_tokens(primary_terms)
    title_tokens = _anchor_tokens(title_terms)
    primary_term_keys = {term.lower() for term in primary_terms}
    title_core_terms = [
        term for term in _known_terms(title)
        if _keep_term(term) and term.lower() not in primary_term_keys
    ]
    title_core_tokens = _anchor_tokens(title_core_terms)
    direct_npe = bool(_RE_NPE_CORE.search(
        " ".join([title, primary, " ".join(knowledge_tags)])
    ))

    # 次级标签只在同域，或标题明确提及时进入候选。避免 String 题因次级
    # Redis/数据库标签而混入跨域主题。
    secondary_tags, blocked_tags = [], []
    title_low = title.lower()
    for tag in knowledge_tags:
        if tag == primary:
            continue
        same_domain = _tag_domain(tag) == primary_domain
        in_title = tag.lower() in title_low
        if same_domain or in_title:
            secondary_tags.append(tag)
        else:
            blocked_tags.append(tag)
    blocked_fragments = _blocked_source_fragments(blocked_tags)
    blocked_domains = {
        domain for tag in blocked_tags
        if (domain := _tag_domain(tag))
    }

    excerpt = (q.get("source_excerpt") or "").strip()
    analysis = _real_analysis(q)
    fb_sentences, _has_fallback = _fallback_analysis(q) if use_fallback else ([], False)

    # 句子池只保留与主标签或标题核心概念共现、且不是问句/跨域主题的句子。
    pool = []
    for text, source in ((excerpt, "source_excerpt"),
                         (analysis, "analysis")):
        for s in _sentences(text):
            pool.append((s, source))
    for s in fb_sentences:
        pool.append((s, "analysis"))

    seen, unique = {}, []
    for order, (s, source) in enumerate(pool):
        if _is_question_like(s) or _is_navigation_sentence(s):
            continue
        low = s.lower()
        if any(fragment in low for fragment in blocked_fragments):
            continue
        sentence_domains = {
            KEYWORD_DOMAIN.get(term.lower())
            for term in _known_terms(s)
        } - {None}
        if sentence_domains & blocked_domains:
            continue
        sc = _score_sentence(
            s, primary_tokens, title_tokens,
            required_tokens=title_core_tokens,
        )
        if sc <= 0:
            continue
        if s in seen:
            existing = seen[s]
            existing[0].add(source)
            existing[1] = max(existing[1], sc)
            continue
        row = [set([source]), sc, order]
        seen[s] = row
        unique.append((s, row))
    unique.sort(key=lambda x: (-x[1][1], x[1][2]))

    selected, budget = [], 0
    for s, row in unique:
        extra = 1 if selected else 0
        if len(selected) >= 4 or budget + len(s) + extra > _MAX_SUMMARY:
            continue
        selected.append((s, row[0]))
        budget += len(s) + extra

    ordered, origins = [], {}
    # 1) 主标签规范化；2) 同域次级标签；3) 标题技术词；
    # 4) 来源正文概念词（低权重、限量）；5) 摘要补充；6) 受控同义词兜底。
    _add_bucket(ordered, origins, primary_terms, "tags", 3)
    secondary_terms = []
    for tag in secondary_tags:
        secondary_terms.extend(_tag_terms(tag))
    _add_bucket(ordered, origins, secondary_terms, "tags", 2)
    _add_bucket(ordered, origins, title_terms, "title", 4)

    source_counts = Counter()
    source_details = {}
    known_source = set()
    for s, row in unique:
        sentence_sources = row[0]
        detail = ("analysis" if "analysis" in sentence_sources
                  else "source_excerpt")
        known = _known_terms(s)
        known_source.update(t.lower() for t in known)
        rest = [t for t in _text_terms(s) if t not in known]
        for term in known + rest:
            if _source_term_allowed(term, primary_domain,
                                    blocked_fragments, blocked_domains,
                                    direct_npe):
                source_counts[term] += 1
                source_details.setdefault(term, set()).add(detail)

    def source_priority(term: str) -> tuple:
        low = term.lower()
        is_english = bool(re.search(r"[A-Za-z]", term))
        repeated = source_counts[term] >= 2
        technical_suffix = term.endswith(_TECH_SUFFIXES)
        return (
            0 if low in known_source else 1,
            0 if is_english else 1,
            0 if repeated else 1,
            0 if technical_suffix else 1,
            -len(term),
            low,
        )

    source_terms = sorted(source_counts, key=source_priority)
    source_added = 0
    for term in source_terms:
        if len(ordered) >= 9 or source_added >= 3:
            break
        before = len(ordered)
        details = source_details.get(term) or {"source_excerpt"}
        detail = "analysis" if "analysis" in details else "source_excerpt"
        _append_term(ordered, origins, term, "source", detail)
        if len(ordered) > before:
            source_added += 1

    summary_terms = []
    for s, sources in selected:
        summary_terms.extend(_known_terms(s))
    _add_bucket(ordered, origins, summary_terms, "summary", 2)

    synonym_pool = primary_terms + secondary_terms + title_terms
    if len(ordered) < _MIN_TERMS:
        _add_bucket(
            ordered, origins, synonym_pool, "synonyms",
            _MAX_TERMS - len(ordered),
        )

    terms = ordered[:_MAX_TERMS]
    term_origin_values = set()
    for term in terms:
        term_origin_values.update(origins.get(term, ()))

    used_sources = ["tags"]
    if "title" in term_origin_values:
        used_sources.append("title")
    if "source_excerpt" in term_origin_values:
        used_sources.append("source_excerpt")
    if "analysis" in term_origin_values:
        used_sources.append("analysis")
    if "synonyms" in term_origin_values:
        used_sources.append("synonyms")
    selected_source_values = set()
    for _s, sources in selected:
        selected_source_values.update(sources)
    if ("source_excerpt" in selected_source_values
            and "source_excerpt" not in used_sources):
        used_sources.append("source_excerpt")
    if ("analysis" in selected_source_values
            and "analysis" not in used_sources):
        used_sources.append("analysis")

    selected_chars = sum(len(s) for s, _src in selected)
    if len(selected) >= 2 and selected_chars >= 80 and not repo_fallback:
        confidence = "high"
    elif len(selected) >= 1:
        confidence = "medium"
    else:
        confidence = "low"

    summary = "。".join(s for s, _src in selected)[:_MAX_SUMMARY]
    return {
        "concept_terms": terms,
        "concept_summary": summary,
        "concept_source": "+".join(used_sources),
        "concept_confidence": confidence,
    }


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.with_name(
        f"{path.stem}_backup_pre_concept_rebuild_{stamp}{path.suffix}"
    )
    payload = path.read_bytes()
    target.write_bytes(payload)
    assert hashlib.sha256(target.read_bytes()).digest() == \
        hashlib.sha256(payload).digest()
    return target


def _atomic_json(path: Path, value) -> None:
    """同目录临时文件 + os.replace，避免中断时留下半份 JSON。"""
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)


def build_profiles(qs, *, checkpoint=lambda: None, use_fallback=False) -> tuple:
    """Build validated profiles in memory, without reading the installed bank."""
    if not qs:
        raise ValueError("题库为空，请先按 QUESTION_BANK.md 导入有效题目")
    profiles = {}
    source_counts = Counter()
    for q in qs:
        checkpoint()
        prof = extract_profile(q, use_fallback=use_fallback)
        profiles[q["id"]] = prof
        source_counts.update(prof["concept_source"].split("+"))

    stats, errors = validate_profiles([q["id"] for q in qs], profiles)
    assert not errors, f"概念档案校验失败: {errors[:10]}"
    assert stats["term_count_below_min"] == 0
    assert stats["term_count_above_max"] == 0
    assert stats["missing_ids_count"] == 0
    assert stats["extra_ids_count"] == 0
    assert stats["summary_over_limit"] == 0

    data = {
        "schema_version": 1,
        "generator": "scripts/build_concept_profiles.py",
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "profile_schema": list(profiles[qs[0]["id"]].keys()),
        "profiles": profiles,
    }
    term_lens = [len(p["concept_terms"]) for p in profiles.values()]
    report = {
        "built_at": data["built_at"],
        "total_questions": len(qs),
        "coverage": stats["covered"],
        "covered_ratio": round(stats["covered"] / max(1, len(qs)), 4),
        "confidence_distribution": stats["confidence"],
        "low_confidence_count": stats["confidence"]["low"],
        "term_shortage_ids": [
            qid for qid, prof in profiles.items()
            if len(prof.get("concept_terms") or []) < _MIN_TERMS
        ],
        "term_overflow_ids": [
            qid for qid, prof in profiles.items()
            if len(prof.get("concept_terms") or []) > _MAX_TERMS
        ],
        "term_count_below_min": stats["term_count_below_min"],
        "term_count_above_max": stats["term_count_above_max"],
        "summary_over_limit": stats["summary_over_limit"],
        "avg_term_count": round(sum(term_lens) / max(1, len(term_lens)), 2),
        "concept_sources": dict(source_counts.most_common()),
        "analysis_sourced_count": sum(
            1 for p in profiles.values()
            if "analysis" in (p.get("concept_source") or "")),
        "missing_ids_count": stats["missing_ids_count"],
        "extra_ids_count": stats["extra_ids_count"],
        "invalid_terms_count": stats["invalid_terms_count"],
        "backup": None,
        "errors": errors,
    }
    return data, report


def main() -> dict:
    from bagu_trainer.infrastructure.knowledge_base import KnowledgeBase
    qs = KnowledgeBase(PROJECT / "data" / "questions.json").all()
    data, report = build_profiles(qs, use_fallback=True)
    backup = _backup(CONCEPTS_PATH)
    report["backup"] = str(backup) if backup is not None else None
    _atomic_json(CONCEPTS_PATH, data)
    _atomic_json(REPORT_PATH, report)
    print("[concepts]", CONCEPTS_PATH)
    print("[report]", REPORT_PATH)
    print("[coverage]", f"{report['coverage']}/{len(qs)}")
    print("[confidence]", report["confidence_distribution"])
    print("[sources]", report["concept_sources"])
    return report


if __name__ == "__main__":
    main()
