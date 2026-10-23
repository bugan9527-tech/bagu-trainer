# -*- coding: utf-8 -*-
"""匹配模块：把 AI 工作上下文映射到题库知识点（可解释检索，不使用 LLM）。

方案（带 IDF 的多字段加权 BM25 相关性检索，离线、不使用 LLM）：
  - 检索文档不再只有题干：每题为 (字段, 权重) 加权索引——
      title(1.0) = knowledge_point + question；
      tag：主标签 2.0 / 次标签 1.6（repo_default 兜底标签降为 1.0）；
      domain(0.3) 只做温和领域信号，不硬过滤；
      concept(concept_terms 1.2 / concept_summary 0.8，来自概念档案
      data/question_concepts.json，由 scripts/build_concept_profiles.py
      离线确定性生成，不落完整答案/来源正文)
    组合规则：title/tag/domain 相加；concept 只对题干与标签之外的
    新 gram 提供证据（纯召回扩展，不重复计权），纯英文概念词不进入
    concept 字段。标签与概念词对「题干未覆盖」的考点权重高于普通摘要词，
    但兜底宽泛标签降权，避免压过具体技术词。
  - 中文：字符 2/3-gram；英文：词边界切分，camelCase/snake_case 再拆分
  - 噪声控制：
    * gram 级停用表（什么/如何/区别/实现/代码/修改/为什么/怎么…）
    * 任何包含停用双字组（如「如何」「实现」「解决」）的 gram 整体丢弃
    * 包含纯函数字符（了的呢吗啊吧）的 gram 丢弃（如「对方的」「调用了」）
    * IDF 封顶（idf_cap），避免 df≈1 的噪声 gram 以极端权重霸榜
  - BM25（k1=1.5, b=0.75），query 侧每个词条只计一次权重
  - 分数做长度鲁棒的 logistic 归一化：score = raw / (raw + pivot) ∈ [0,1]，
    其中 raw 为「已匹配词条 IDF 贡献总和」。相比「除以 query 总 IDF 权重」的
    覆盖率归一化，logistic 不随上下文长度膨胀（真实上下文=会话记录+diff
    可达数千字，覆盖率归一化会把一切得分稀释到阈值以下）
  - 证据门控：除加权 raw 外，还要求已匹配词条的纯 IDF 总和（evidence）
    达到 raw_min，防止单个罕见 gram 借字段权重孤证过线。
  - 可插拔语义检索：SemanticRetriever（默认 None=纯离线 BM25+概念增强）。
    启用时 BM25 与语义分先各自 min-max 归一化再加权重排，不跨尺度直接相加。
  - use_tags=False / use_concepts=False 时索引退化为旧版纯题干 BM25
    （用于「修改前/后」同口径评测对比）。
  - 任务意图过滤：README/注释/格式化等纯文档任务，以及颜色/间距/CSS
    等纯视觉微调，不进入知识点检索；混合任务若包含明确故障或领域信号则保留
  - 领域门控：识别 Java、C/C++、前端、数据库、网络等显式领域；候选题的
    强领域与查询领域不相交时剔除，避免「空指针」「锁」「事务」等同词跨域误命中
  - 门控（都通过才出题，宁缺毋滥）:
    1) 置信：top1 归一化分 < conf_min     -> 空
    2) 证据：top1 已匹配 IDF 总量 < raw_min -> 空（单一巧合 gram 不算数）
    3) 区分：top1 与「次一等候选群」的分差 < margin_min 且置信未达
       conf_strong -> 空（同题族的并列不算分差不足，见 _cluster_margin）
    4) 跨域：top1 与近似分的异域强候选并列，且置信未达 conf_strong -> 空
    5) Java NPE：只接受异常相关且非 C/C++ 的候选，否则返回空

接口（保持兼容）：
  Matcher.match(text, top_n=3, min_score=1) -> [(question_dict, score, hits)]
  Matcher.rank(text, top_n=3, min_score=1)  -> [{question, id, score,
                                                 confidence, margin, hits}]

min_score 兼容规则：旧版分值域为整数命中次数（min_score=1 表示「至少命中一次」）；
新版分值归一化到 [0,1] 后，min_score >= 1 一律按 0 处理（相关性由门控把关），
0 < min_score < 1 时作为归一化分阈值。
"""
import math
import re
from collections import Counter
from functools import lru_cache
from typing import Optional, Protocol

from .concepts import load_concept_profiles

_RE_WORD = re.compile(r"[A-Za-z0-9]+")
_RE_CJK = re.compile(r"[\u4e00-\u9fff]+")

# 英文停用词（与 scripts/import_questions.py 的 STOP_EN 一致）
STOP_EN = {
    "the", "and", "you", "your", "how", "what", "why", "for", "are", "its",
    "our", "that", "this", "have", "has", "will", "can", "not", "was",
    "with", "from", "use", "using", "does", "when", "where", "which",
    "who", "into", "about", "than", "then", "them", "they", "here",
    "there", "also", "any", "get", "let", "make", "most", "some", "more",
    "out", "all", "one", "two", "way", "new", "per", "did", "do", "is",
    "it", "of", "to", "in", "on", "a", "an", "or", "if", "as", "at",
    "by", "be", "no", "so", "up", "we", "he", "she", "i",
}

# gram 级停用表：整词等于其中之一即丢弃（低信息泛词）
STOP_GRAMS = {
    "什么", "如何", "区别", "实现", "代码", "修改", "为什么", "怎么", "哪些",
    "使用", "方法", "作用", "问题", "原理", "流程", "机制", "理解",
    "介绍", "了解", "优点", "缺点", "优势", "好处", "方案", "设计", "处理",
    "解决", "避免", "导致", "原因", "情况", "方式", "步骤", "过程", "特点",
    "类型", "示例", "相关", "常见", "常用", "概念", "面试", "能否", "是否",
    "一下", "以及", "等等", "没有", "不会", "不能", "可以", "应该", "需要",
    "必须", "认为", "觉得", "一般", "通常", "主要", "基本", "比较", "各种",
    "一些", "表示", "分别", "怎样", "怎么样",
}

# 停用双字组：gram 内部包含其中任意一个即整体丢弃
# （覆盖 3-gram 粘连与部分匹配，如「何实现」含「实现」、「为什么」含「什么」）
STOP_BIGRAMS = (
    "什么", "如何", "区别", "实现", "代码", "修改", "怎么", "为何",
    "哪些", "怎样", "什么样", "解决",
)

# 纯函数字符：gram 中出现任意一个即丢弃（「对方的」「调用了」
# 以及「何实」这类泛词边界粘连均为噪声组合）
JUNK_CHARS = set("了的呢吗啊吧把被何怎哪")

# 热路径表：停用双字组子串/函数字符检查用集合成员判断代替逐项正则匹配
# （索引构建对 ~百万字符做 2/3-gram 过滤，正则 search 约占一半构建耗时）
_STOP_BIGRAMS_SET = set(STOP_BIGRAMS)

# ---- 多字段检索权重 ----
# BM25F 风格：每个字段独立 BM25 贡献（cap ≤1.0）后乘以字段权重，
# title/tag/domain/concept 各字段相加；标签权重高于题干词；repo_default
# 兜底标签统一降到 1.0，避免宽泛仓库标签压过具体技术词。concept 字段
# 权重与组合（sum/max、英文词开关、饱和 damp 等）经 dev/held-out 评测
# 标定，见 Matcher 构造参数；默认值 = 评测通过的组合。
_W_TITLE = 1.0
_W_DOMAIN = 0.3
_W_TAG_PRIMARY = 2.0
_W_TAG_OTHER = 1.6
_W_TAG_FALLBACK = 1.0

# ---- 任务意图与领域信号 ----
_RE_DOC_CONTEXT = re.compile(
    r"\breadme\b|\bchangelog\b|\bdocstring\b|文档|注释|使用说明|安装步骤"
    r"|用户手册|api\s*手册|发布说明|变更日志|版权头",
    re.I,
)
_RE_EDIT_ACTION = re.compile(
    r"更新|补充|补上|补全|修改|改成|换成|调整|添加|增加|删除|整理|润色|修正"
    r"|重写|替换|排版|重排|配置|设置|统一|微调|修复|转成|加|写",
)
_RE_CONCEPT_QUERY = re.compile(
    r"为什么|为何|原理|区别|底层|工作机制"
    r"|如何(?:实现|工作|排查|定位|调优|选择|避免|解决|设计)"
    r"|怎么(?:实现|工作|排查|定位|选择|避免|解决|设计)"
    r"|怎样(?:实现|工作|排查|定位|选择|避免|解决|设计)",
    re.I,
)
_RE_ENGINEERING_PROBLEM = re.compile(
    r"(?:修复|排查|定位|解决|处理|避免|治理|优化).{0,24}"
    r"(?:空指针异常|异常抛出|抛出异常|发生异常|报错|崩溃|死锁|超时"
    r"|内存泄漏|栈溢出|内存溢出|不生效|失败|OOM|NPE|NullPointerException)"
    r"|(?:空指针异常|抛出异常|发生异常|报错|崩溃|死锁|内存泄漏"
    r"|栈溢出|内存溢出|OOM|NPE|NullPointerException)",
    re.I,
)
_RE_FORMATTING_ONLY = re.compile(
    r"代码格式|代码风格|格式化|缩进|空行|prettier|ruff\s+(?:format|check)"
    r"|eslint\s+--fix|gofmt|black\s+",
    re.I,
)
# 配置文件微调：改配置项本身不是可考知识点（概念类提问/工程故障除外）
_RE_CONFIG_TWEAK = re.compile(
    r"配置文件|config\s*文件|config\.(?:json|yaml|yml|toml|ini)"
    r"|\.yaml\b|\.yml\b|\.env\b|\.toml\b|\.properties\b|\.ini\b",
    re.I,
)
_RE_VISUAL_CONTEXT = re.compile(
    r"\bcss\b|\bscss\b|\bsass\b|\bless\b|tailwind|样式|颜色|色值|间距|边距"
    r"|\bmargin\b|\bpadding\b|圆角|阴影|渐变|字号|字体|主题|栅格|布局"
    r"|overflow|\bflex\b|\bgrid\b|背景色|宽度|高度|列宽|边框|图标|图片|截图"
    r"|报表|表格|标题",
    re.I,
)
_RE_VISUAL_EDIT = re.compile(
    r"调整|修改|微调|改成|换成|转换|转成|设置|配置|统一|修复|替换|加|删除"
    r"|改深|改浅|加粗",
)
_RE_NON_TECHNICAL_CONTEXT = re.compile(
    r"文案|提示语|壁纸|锁屏|鼠标指针|邮件|会议|桌面|待办|群消息|聊天"
    r"|票据|播放器|版权|拼写|截图",
    re.I,
)
_RE_GENERIC_EDIT = re.compile(
    r"修(?:复)?(?:一下)?(?:这个|这段)?\s*(?:bug|问题|错误)"
    r"|\bfix\s+(?:a\s+)?bug\b|优化(?:一下)?性能|性能优化"
    r"|重构(?:一下)?(?:这段)?代码|改(?:一下)?(?:这段)?代码|代码(?:写得)?更优雅"
    r"|变量名|函数名|参数校验|提取常量|魔法数字|拼写错误|测试.{0,12}断言"
    r"|升级依赖|依赖版本|兼容性告警"
    r"|重排目录|目录结构|移动(?:文件|代码)|import\s+路径"
    r"|环境变量加载|本地开发环境",
    re.I,
)
_RE_EXPLICIT_CPP = re.compile(r"\bc\+\+|\bcpp\b", re.I)
_RE_JAVA_NPE = re.compile(r"nullpointerexception|\bnpe\b|空指针异常", re.I)
_RE_EXCEPTION_TOPIC = re.compile(
    r"exception|throwable|异常|空指针|npe|nullpointerexception", re.I,
)

# NPE 查询的门控明确要求 Java/异常主题。标题或主标签直接表达异常属于
# 真实意图证据，不通过降低 raw_min/margin 来放行。
_NPE_INTENT_EVIDENCE_DIRECT = 6.0
_NPE_INTENT_EVIDENCE_EXCEPTION = 4.5

# 强信号表示明确语言/产品或技术栈；弱信号表示可能有多个领域含义的概念词。
_DOMAIN_STRONG = {
    "java": re.compile(
        r"\bjava\b|\bjdk\b|\bjvm\b|\bspring(?:boot)?\b|\bmybatis\b"
        r"|\bhibernate\b|\bthreadlocal\b|\bhashmap\b|\bconcurrenthashmap\b"
        r"|\barraylist\b|\blinkedlist\b|\bsynchronized\b|\breentrantlock\b"
        r"|\baqs\b|\bclassloader\b|\bjstack\b|\bcompletablefuture\b"
        r"|@transactional|nullpointerexception|\bnpe\b|空指针异常",
        re.I,
    ),
    "cpp": re.compile(
        r"\bc\+\+|\bcpp\b|\bstd::|\bshared_ptr\b|\bunique_ptr\b"
        r"|\bmake_shared\b|悬空指针|野指针|智能指针|析构函数|空指针|指针",
        re.I,
    ),
    "frontend": re.compile(
        r"\breact\b|\bvue\b|\bangular\b|javascript|typescript|\bcss\b"
        r"|\bscss\b|\bhtml\b|tailwind|浏览器|\bdom\b|\bnode(?:\.js)?\b"
        r"|webpack|\bvite\b|\bbfc\b|前端",
        re.I,
    ),
    "database": re.compile(
        r"\bmysql\b|\binnodb\b|\bpostgres(?:ql)?\b|\boracle\b|\bmongodb\b"
        r"|\bredis\b|\bsqlite\b|sql\s*server|数据库|分库分表|\bmvcc\b"
        r"|\bexplain\b|缓存穿透|缓存击穿|缓存雪崩",
        re.I,
    ),
    "network": re.compile(
        r"\btcp\b|\budp\b|\bhttp\b|\bhttps\b|\btls\b|\bssl\b|\bdns\b"
        r"|\bnginx\b|\bsocket\b|\bepoll\b|网关|三次握手|状态码|\b50[234]\b",
        re.I,
    ),
    "messaging": re.compile(
        r"\bkafka\b|\brocketmq\b|\brabbitmq\b|消息队列", re.I,
    ),
    "operations": re.compile(
        r"\bdocker\b|\bkubernetes\b|\bk8s\b|容器|镜像", re.I,
    ),
    "algorithm": re.compile(
        r"leetcode|二分查找|动态规划|回溯算法|二叉树|\bdfs\b|\bbfs\b",
        re.I,
    ),
    "os": re.compile(
        r"\blinux\b|操作系统|虚拟内存|页表|缺页|页面置换|\bbelady\b"
        r"|进程调度|文件系统",
        re.I,
    ),
}

_DOMAIN_WEAK = {
    "java": re.compile(
        r"线程池|垃圾回收|\bfull\s*gc\b|异常|\bbean\b|\bioc\b|\baop\b"
        r"|反射|序列化|volatile|\bcas\b|\boom\b|内存泄漏|接口|并发|线程|锁",
        re.I,
    ),
    "cpp": re.compile(r"内存泄漏|引用计数|模板|内存管理", re.I),
    "frontend": re.compile(
        r"事件循环|组件|样式|布局|网页|页面|\bkey\b", re.I,
    ),
    "database": re.compile(
        r"事务|索引|锁|执行计划|连接池|表结构|字段|\bsql\b|缓存|一致性",
        re.I,
    ),
    "network": re.compile(r"握手|超时|连接|协议|响应", re.I),
    "messaging": re.compile(r"重平衡|幂等|消费组|消息", re.I),
    "operations": re.compile(r"容器|日志|部署", re.I),
    "algorithm": re.compile(r"排序|查找|树|图|复杂度|递归", re.I),
}


@lru_cache(maxsize=20000)
def _domain_profile(text: str):
    """返回 (强领域集合, 全部领域集合)，用于解释和执行跨领域门控。

    进程内按文本缓存：题库内存在大量近重复题干/标签组合，
    领域扫描（每文本 ~18 次正则）占索引构建相当比例。
    """
    strong, all_domains = set(), set()
    for domain, pattern in _DOMAIN_STRONG.items():
        if pattern.search(text):
            strong.add(domain)
            all_domains.add(domain)
    for domain, pattern in _DOMAIN_WEAK.items():
        if pattern.search(text):
            all_domains.add(domain)
    # “空指针”在 C/C++ 题里是强信号；Java NPE 查询中除非显式写 C++，
    # 否则不把同一个中文词当作 C/C++ 领域声明。
    if _RE_JAVA_NPE.search(text) and not _RE_EXPLICIT_CPP.search(text):
        strong.discard("cpp")
        all_domains.discard("cpp")
    return strong, all_domains


def _skip_as_non_knowledge_task(text: str, profile) -> bool:
    """过滤没有可测试知识点的文档、样式、格式化和泛化修改任务。"""
    strong, _all_domains = profile
    concept = bool(_RE_CONCEPT_QUERY.search(text))
    engineering = bool(_RE_ENGINEERING_PROBLEM.search(text))

    if _RE_FORMATTING_ONLY.search(text):
        return True

    if _RE_CONFIG_TWEAK.search(text):
        # 改配置项/配置文件本身不是知识点；概念类提问或工程故障保留
        return not concept and not engineering

    if _RE_DOC_CONTEXT.search(text) and _RE_EDIT_ACTION.search(text):
        return not concept and not engineering

    if _RE_VISUAL_CONTEXT.search(text) and _RE_VISUAL_EDIT.search(text):
        if concept:
            return False
        # 样式改动与后端/运行时故障混在一起时，保留可检索的工程问题。
        if engineering and (strong - {"frontend", "operations"}):
            return False
        return True

    if _RE_NON_TECHNICAL_CONTEXT.search(text) and not strong:
        return True

    if _RE_GENERIC_EDIT.search(text) and not _all_domains:
        return True
    return False


_RE_CAMEL_BREAK1 = re.compile(r"([a-z0-9])([A-Z])")
_RE_CAMEL_BREAK2 = re.compile(r"([A-Z])([A-Z][a-z])")
_RE_WORD_SPLIT = re.compile(r"[_\-]+")


def _english_tokens(text: str) -> list:
    """英文词边界切分 + camelCase/snake_case 拆词；丢弃停用词与单字符"""
    out = []
    for w in _RE_WORD.findall(text):
        for part in _RE_WORD_SPLIT.split(w):
            if not part:
                continue
            # camelCase 拆分（原来大小写信息在 lower 前保留）
            part = _RE_CAMEL_BREAK1.sub(r"\1 \2", part)
            part = _RE_CAMEL_BREAK2.sub(r"\1 \2", part)
            for sub in part.split():
                low = sub.lower()
                if len(low) >= 2 and low not in STOP_EN:
                    out.append(low)
    return out


def _cjk_grams(text: str) -> list:
    """中文字符 2/3-gram，过滤低信息 gram"""
    out = []
    for run in _RE_CJK.findall(text):
        n = len(run)
        for i in range(n - 1):
            g = run[i:i + 2]
            if g in STOP_GRAMS or g in _STOP_BIGRAMS_SET:
                continue
            if g[0] in JUNK_CHARS or g[1] in JUNK_CHARS:
                continue
            out.append(g)
        for i in range(n - 2):
            g = run[i:i + 3]
            if g in STOP_GRAMS:
                continue
            if g[:2] in _STOP_BIGRAMS_SET or g[1:] in _STOP_BIGRAMS_SET:
                continue
            if (g[0] in JUNK_CHARS or g[1] in JUNK_CHARS
                    or g[2] in JUNK_CHARS):
                continue
            out.append(g)
    return out


def tokenize(text: str) -> list:
    """把文本切成检索词条（中文 n-gram + 英文词），保留重复以统计词频"""
    if not text:
        return []
    return _english_tokens(text) + _cjk_grams(text)


# 索引构建专用缓存：标签/知识域/概念词等短文本在 5443 题中大量重复，
# 逐题重复分词会明显拉长构建时间；只读使用（调用方不得修改返回值）。
_tokenize_cached = lru_cache(maxsize=30000)(tokenize)


def _doc_text(q: dict) -> str:
    """题目主文本：知识点名 + 题干（两者相同时只取一次，避免词频虚高）"""
    kp = q.get("knowledge_point") or ""
    question = q.get("question") or ""
    return kp if kp == question else kp + " " + question


def _domain_text(q: dict) -> str:
    """候选领域判定文本：题干 + 标签 + 领域（概念摘要不参与，避免正文噪声）"""
    parts = [_doc_text(q)]
    parts.extend(t for t in (q.get("knowledge_tags") or []) if t)
    domain = q.get("knowledge_domain") or ""
    if domain and domain not in parts:
        parts.append(domain)
    return " ".join(parts)


class SemanticRetriever(Protocol):
    """可插拔语义检索后端（默认不启用；不启用即纯离线 BM25+概念增强）。

    启用条件：项目已具备本地 embedding 依赖且基准证明有收益；
    绝不下载大模型、不调用外部 embedding API。
    available() 为 False 时 Matcher 忽略该后端；
    retrieve() 返回 [{id, score}]，score 允许任意尺度——
    与 BM25 混合时统一 min-max 归一化再加权重排（见 _blend_semantic）。
    """
    def available(self) -> bool: ...

    def retrieve(self, query: str, top_k: int = 20) -> list: ...


class Matcher:
    """相关性检索匹配器。默认阈值经 scripts/eval_matcher.py 评测标定。"""

    def __init__(self, knowledge_base, k1: float = 1.5, b: float = 0.75,
                 idf_cap: float = 7.0, conf_min: float = 0.05,
                 raw_min: float = 10.0, margin_min: float = 0.01,
                 conf_strong: float = 0.4, pivot: float = 25.0,
                 use_tags: bool = True, use_concepts: bool = True,
                 semantic: Optional[SemanticRetriever] = None,
                 semantic_weight: float = 0.3,
                 concept_mode: str = "sum",
                 skip_english_concepts: bool = False,
                 w_concept_terms: float = 1.8,
                 w_concept_summary: float = 0.8,
                 concept_damp: float = 1.0,
                 concept_damp_floor: float = 5.0,
                 concept_gram_cap: Optional[float] = None,
                 concept_overlap_discount: float = 0.5):
        self.kb = knowledge_base
        self.k1 = k1
        self.b = b
        self.idf_cap = idf_cap
        self.conf_min = conf_min
        self.raw_min = raw_min
        self.margin_min = margin_min
        self.conf_strong = conf_strong
        self.pivot = pivot
        self.use_tags = use_tags
        self.use_concepts = use_concepts
        self.semantic = semantic
        self.semantic_weight = semantic_weight
        # 概念字段组合参数（索引构建时生效；评测标定）
        self.concept_mode = concept_mode
        self.skip_english_concepts = skip_english_concepts
        self.w_concept_terms = w_concept_terms
        self.w_concept_summary = w_concept_summary
        self.concept_damp = concept_damp
        self.concept_damp_floor = concept_damp_floor
        self.concept_gram_cap = concept_gram_cap
        self.concept_overlap_discount = concept_overlap_discount
        self.questions = list(self.kb.all())
        self._concepts = (
            load_concept_profiles(getattr(self.kb, "concepts_path", None)) if use_concepts else {}
        )
        self._build_index()

    # ---- 索引构建 ----
    def _index_fields(self, q: dict, prof: dict) -> list:
        """把题目展开为 (kind, text, weight) 字段列表（见模块 docstring 权重表）。

        repo_default 兜底标签一律降权：宽泛仓库标签不得压过具体技术词。
        """
        fields = [("title", _doc_text(q), _W_TITLE)]
        if not self.use_tags:
            return fields
        primary = q.get("primary_knowledge_tag") or ""
        knowledge_tags = [t for t in (q.get("knowledge_tags") or []) if t]
        fallback = "repo_default" in (q.get("knowledge_tag_source") or "")
        if primary:
            fields.append(("tag", primary,
                           _W_TAG_FALLBACK if fallback else _W_TAG_PRIMARY))
        others = [t for t in knowledge_tags if t != primary]
        if others:
            fields.append(("tag", " ".join(others),
                           _W_TAG_FALLBACK if fallback else _W_TAG_OTHER))
        domain = q.get("knowledge_domain") or ""
        if domain:
            # 领域只作温和加权信号，不进入硬过滤（跨领域题不会被完全排除）
            fields.append(("domain", domain, _W_DOMAIN))
        if not self.use_concepts or not prof:
            return fields
        terms = [str(t) for t in (prof.get("concept_terms") or []) if t]
        if terms:
            fields.append(("concept", " ".join(terms),
                           self.w_concept_terms))
        summary = (prof.get("concept_summary") or "").strip()
        if summary:
            # 单独标记摘要字段：排序仍按 concept 合并计分，但可对
            # “摘要偶然提到查询词”做精度降权。
            fields.append(
                ("concept_summary", summary, self.w_concept_summary)
            )
        return fields

    def _build_index(self):
        """多字段加权 BM25 索引：预计算每词条对每题的贡献 c。

        c = Σ_fields w * min(1.0, (f*k1p1)/(f + norm_field))，组合规则：
          - title/tag/domain 与 concept 字段相加（标签/概念是题干之外的
            加权信号，repo_default 兜底标签降权）；
          - concept 类内按 concept_mode 取值：sum（terms+summary 相加）
            或 max（terms 与 summary 同源取最大）；
          - skip_english_concepts=True 时纯英文概念词不进入 concept 字段
            （英文词 IDF 高、概念句来源噪声大）。
        查询时 raw = Σ IDF(t) * c[doc][t]（一次 dict 命中/词条）。
        use_tags/use_concepts 关闭时等价于旧版纯题干 BM25（评测同口径对比）。
        """
        n = len(self.questions)
        kind_sums = {
            "title": 0.0, "tag": 0.0, "domain": 0.0,
            "concept": 0.0, "concept_summary": 0.0,
        }
        kind_counts = {
            "title": 0, "tag": 0, "domain": 0,
            "concept": 0, "concept_summary": 0,
        }
        docs = []
        for q in self.questions:
            prof = (self._concepts.get(q.get("id")) or {}
                    if self.use_concepts else {})
            prof = prof or q.get("concept_profile") or {}
            tokenized_fields = []
            for kind, text, w in self._index_fields(q, prof):
                tf = Counter(_tokenize_cached(text))
                kind_sums[kind] += sum(tf.values())
                kind_counts[kind] += 1
                tokenized_fields.append((kind, w, tf))
            docs.append(tokenized_fields)
        kind_avg = {k: kind_sums[k] / max(1, kind_counts[k])
                    for k in kind_sums}
        self._avgdl = sum(kind_sums.values()) / max(
            1, sum(kind_counts.values()))
        k1p1 = self.k1 + 1.0
        self._contrib = []
        self._concept_contrib = []
        self._summary_contrib = []
        self._base_contrib = []
        df = Counter()
        for doc in docs:
            base = {}
            concept = {}
            summary_contrib = {}
            for kind, w, tf in doc:
                dl = sum(tf.values()) or 1.0
                norm = self.k1 * (1.0 - self.b
                                  + self.b * dl / kind_avg[kind])
                for t, f in tf.items():
                    factor = min(1.0, (f * k1p1) / (f + norm))
                    c = w * factor
                    if kind in ("concept", "concept_summary"):
                        if self.skip_english_concepts and not _RE_CJK.search(t):
                            # 纯英文概念词跳过：英文词 IDF 高、摘要句噪声大
                            continue
                        if self.concept_mode == "max":
                            concept[t] = max(concept.get(t, 0.0), c)
                        else:
                            concept[t] = concept.get(t, 0.0) + c
                        if kind == "concept_summary":
                            summary_contrib[t] = (
                                summary_contrib.get(t, 0.0) + c
                            )
                    else:
                        base[t] = base.get(t, 0.0) + c
            total = {}
            for t, bc in base.items():
                total[t] = bc + concept.get(t, 0.0)
            for t, cc in concept.items():
                total[t] = base.get(t, 0.0) + cc
            for t in total:
                df[t] += 1
            self._contrib.append(total)
            self._concept_contrib.append(concept)
            self._summary_contrib.append(summary_contrib)
            self._base_contrib.append(base)
        # IDF 封顶：df 极低的噪声 gram 不再获得极端权重
        self._idf = {t: min(self.idf_cap,
                            math.log(1.0 + (n - c + 0.5) / (c + 0.5)))
                     for t, c in df.items()}
        # 纯英文题干标记：仅靠英文词命中的英文题按弱匹配处理
        # （题库以中文语境为主，不应让英文论文题压过中文主题候选）
        self._has_cjk = [bool(_RE_CJK.search(_doc_text(q)))
                         for q in self.questions]
        # 候选领域画像：题干 + 标签 + 领域（概念摘要不参与，避免正文噪声）
        self._domain_profiles = [_domain_profile(_domain_text(q))
                                 for q in self.questions]
        self._tag_token_cache = [
            [(t, set(_tokenize_cached(t)))
             for t in (q.get("knowledge_tags") or []) if t]
            for q in self.questions
        ]
        # NPE 查询里 null/pointer 等词往往误命中普通异常题的摘要提及。
        # 预计算这些查询 gram，排序时只压低候选的“摘要证据”。
        self._npe_query_tokens = set(tokenize(
            "NullPointerException NPE null pointer 空指针异常 空指针"
        ))
        self._npe_direct_core = []
        self._exception_direct_core = []
        for q in self.questions:
            core = " ".join(filter(None, (
                _doc_text(q),
                q.get("primary_knowledge_tag") or "",
                " ".join(q.get("knowledge_tags") or []),
            )))
            self._npe_direct_core.append(bool(_RE_JAVA_NPE.search(core)))
            self._exception_direct_core.append(
                bool(_RE_EXCEPTION_TOPIC.search(core))
            )

    # ---- 检索主流程 ----
    def rank(self, text: str, top_n: int = 3, min_score: float = 1):
        """返回候选列表（含 score/confidence/margin/hits）；门控不过返回空。

        门控：非知识点任务 / top1 领域不兼容 / 置信不足 / 证据量不足 /
        与次等候选或异域强候选区分度不足时返回空列表，不强行出题。
        """
        text = text or ""
        query_profile = _domain_profile(text)
        if _skip_as_non_knowledge_task(text, query_profile):
            return []
        q_terms = sorted(t for t in set(tokenize(text)) if t in self._idf)
        if not q_terms:
            return []
        floor = min_score if 0 < min_score < 1 else 0.0
        java_npe = bool(_RE_JAVA_NPE.search(text)
                        and not _RE_EXPLICIT_CPP.search(text))
        scored = []
        for i, (q, contrib) in enumerate(zip(self.questions, self._contrib)):
            raw = 0.0
            raw_concept = 0.0
            evidence = 0.0
            contrib_hits = []
            concept_contrib = self._concept_contrib[i]
            base_contrib = self._base_contrib[i]
            npe_penalty = 0.0
            for t in q_terms:
                c = contrib.get(t)
                if not c:
                    continue
                part = self._idf[t] * c
                raw += part
                # 证据量：已匹配词条的纯 IDF 总和（不乘字段权重）。
                # 标签/概念权重放大后，单个罕见 gram 也能把加权 raw 推过
                # raw_min；证据量要求至少 2 个左右独立词条，恢复旧版语义。
                evidence += self._idf[t]
                cc = concept_contrib.get(t)
                part_concept = 0.0
                if cc:
                    part_concept = self._idf[t] * cc
                    if self.concept_gram_cap is not None:
                        # 概念 gram 贡献封顶：不得超过同 gram 题干匹配的
                        # concept_gram_cap 倍（题干匹配=idf*1.0）。
                        capped = self._idf[t] * self.concept_gram_cap
                        if part_concept > capped:
                            part_concept = capped
                    # 与题干/标签重复的概念 gram 打折：同一词条不因
                    # 「题干+概念词」双重携带而线性加倍
                    if base_contrib.get(t, 0.0) > 0.0:
                        share = self.concept_overlap_discount * part_concept
                        part -= share
                        part_concept -= share
                        raw -= share
                    raw_concept += part_concept
                # Java NPE 意图下，普通异常题概念档案里的 NPE/null/pointer
                # 只是来源正文/摘要的偶然共现：概念证据降到 10% 权重，
                # 不能压过标题/主标签直接表达 NPE 或异常主题的候选。
                if (java_npe and not self._npe_direct_core[i]
                        and t in self._npe_query_tokens):
                    concept_c = concept_contrib.get(t, 0.0)
                    if concept_c > 0:
                        penalty = min(
                            part,
                            self._idf[t] * concept_c * 0.9,
                        )
                        part -= penalty
                        npe_penalty += penalty
                contrib_hits.append((t, part))
            if npe_penalty:
                raw -= npe_penalty
                raw_concept = max(0.0, raw_concept - npe_penalty)
            intent_evidence = 0.0
            if java_npe:
                if self._npe_direct_core[i]:
                    intent_evidence = _NPE_INTENT_EVIDENCE_DIRECT
                elif self._exception_direct_core[i]:
                    intent_evidence = _NPE_INTENT_EVIDENCE_EXCEPTION
                raw += intent_evidence
                evidence += intent_evidence
            if raw <= 0:
                continue
            # 概念证据饱和：概念贡献不超过「题干/标签基础贡献」的
            # concept_damp 倍（保留 concept_damp_floor 下限，供纯概念题
            # 仍可获得召回证据）。防止稀缺概念 gram 在自身题干几乎无
            # 匹配时以极端 IDF 霸榜，破坏此前评测标定的排序。
            raw_base = raw - raw_concept
            if (self.concept_damp is not None and raw_concept > 0):
                cap = max(self.concept_damp_floor,
                          self.concept_damp * raw_base)
                if raw_concept > cap:
                    raw = raw_base + cap
                    raw_concept = cap
            # 纯英文题干按弱匹配处理
            if not self._has_cjk[i]:
                raw *= 0.5
                raw_concept *= 0.5
                evidence *= 0.5
            s = raw / (raw + self.pivot)
            if s < floor:
                continue
            scored.append((s, raw, q, contrib_hits, i, raw_concept, evidence))
        # 主排序：归一化分降序；同分按题干长度升序（确定性 tie-break，
        # 简洁题干优先——评分并列时避免任意 doc 顺序决定 top1）
        scored.sort(key=lambda x: (-x[0], len(_doc_text(x[2]))))
        if not scored:
            return []
        # 语义路径（默认关闭）：BM25 与语义分各自归一化后加权重排
        if self.semantic is not None and self._semantic_available():
            scored = self._blend_semantic(scored, text)
        query_strong = query_profile[0]
        if query_strong:
            filtered = self._domain_filter(
                scored, query_strong, java_npe=java_npe,
            )
            if not filtered:
                return []
            # 同分词面上的异域强候选若几乎并列，阈值又不够强，宁可不出题。
            if (not java_npe
                    and scored[0][0] < self.conf_strong
                    and self._cross_domain_close(scored, query_strong)):
                return []
            scored = filtered
        s1, raw1, ev1 = scored[0][0], scored[0][1], scored[0][6]
        margin = self._cluster_margin(scored)
        # 门控：置信 / 证据量 / 区分度
        raw_min = min(self.raw_min, 6.0) if java_npe else self.raw_min
        if (s1 < self.conf_min or raw1 < raw_min
                or ev1 < raw_min
                or (margin < self.margin_min and s1 < self.conf_strong)):
            return []
        out = []
        for i, (s, raw, q, contrib_hits, _qi, raw_concept, _ev) in enumerate(
                scored[:max(1, top_n)]):
            contrib_hits.sort(key=lambda x: -x[1])
            if i == 0:
                m = margin
            else:
                rest = scored[i + 1:] if i + 1 < len(scored) else []
                m = s - rest[0][0] if rest else s
            matched = {t for t, _p in contrib_hits}
            matched_tags = [
                tag for tag, toks in self._tag_token_cache[_qi]
                if toks & matched
            ]
            prof = (self._concepts.get(q.get("id")) or {}
                    or q.get("concept_profile") or {})
            matched_concept_terms = [
                str(t) for t in (prof.get("concept_terms") or [])
                if set(tokenize(str(t))) & matched
            ]
            if java_npe:
                if self._npe_direct_core[_qi]:
                    npe_precision = "direct_npe"
                elif self._exception_direct_core[_qi]:
                    npe_precision = "direct_exception"
                elif any(
                        self._summary_contrib[_qi].get(t, 0.0) > 0
                        for t in self._npe_query_tokens):
                    npe_precision = "summary_incidental"
                else:
                    npe_precision = "general"
            else:
                npe_precision = None
            out.append({
                "question": q,
                "id": q["id"],
                "score": round(s, 4),
                "raw_score": round(raw, 4),
                "confidence": round(s, 4),
                "margin": round(m, 4),
                "hits": [f"{t}({p:.2f})" for t, p in contrib_hits[:8]],
                "matched_tags": matched_tags,
                "matched_concept_terms": matched_concept_terms,
                "concept_boost": round(raw_concept / raw, 4) if raw else 0.0,
                "npe_precision": npe_precision,
            })
        return out

    def _semantic_available(self) -> bool:
        checker = getattr(self.semantic, "available", None)
        if checker is None:
            return True
        try:
            return bool(checker())
        except Exception:
            return False

    def _blend_semantic(self, scored: list, text: str) -> list:
        """BM25 与语义检索结果归一化后加权重排（不做跨尺度直接相加）。

        双方分数先各自 min-max 归一化到 [0,1]，
        final = (1-w)*bm25_norm + w*semantic_norm，按 final 重排。
        语义后端缺失的候选保留 BM25 归一化分（不降为 0）。
        """
        try:
            sem_items = self.semantic.retrieve(text, top_k=len(scored) * 3)
        except Exception:
            return scored
        sem = {}
        for item in sem_items or []:
            try:
                sem[str(item["id"])] = float(item["score"])
            except (KeyError, TypeError, ValueError):
                continue
        if not sem:
            return scored
        b_scores = [x[0] for x in scored]
        b_min, b_max = min(b_scores), max(b_scores)
        s_vals = list(sem.values())
        s_min, s_max = min(s_vals), max(s_vals)

        def norm(v, lo, hi):
            return 1.0 if hi - lo <= 0 else (v - lo) / (hi - lo)

        blended = []
        for s, raw, q, contrib, i, raw_concept, evidence in scored:
            nb = norm(s, b_min, b_max)
            sv = sem.get(q["id"])
            final = nb
            if sv is not None:
                final = (1.0 - self.semantic_weight) * nb \
                    + self.semantic_weight * norm(sv, s_min, s_max)
            blended.append((final, raw, q, contrib, i, raw_concept, evidence))
        blended.sort(key=lambda x: -x[0])
        return blended

    def _domain_filter(self, scored: list, query_strong: set,
                       java_npe: bool = False) -> list:
        """剔除与查询强领域不相交的候选，保留同域题和无强领域标记题。"""
        out = []
        for item in scored:
            idx = item[4]
            candidate_strong, candidate_all = self._domain_profiles[idx]
            q = item[2]
            if java_npe:
                wrong_domain = candidate_strong & {
                    "cpp", "frontend", "database", "network", "messaging",
                    "operations", "algorithm", "os",
                }
                if wrong_domain or not _RE_EXCEPTION_TOPIC.search(_doc_text(q)):
                    continue
                out.append(item)
                continue
            if (candidate_strong
                    and not (candidate_strong & query_strong)
                    and candidate_all):
                continue
            out.append(item)
        return out

    def _cross_domain_close(self, scored: list, query_strong: set) -> bool:
        """top1 的近似候选来自另一个强领域时，视为跨域区分不足。"""
        top_s, _raw, _q, _hits, top_i, _rc, _ev = scored[0]
        top_strong = self._domain_profiles[top_i][0]
        if not top_strong or top_strong & query_strong:
            return False
        for s, _raw, _q, _hits, idx, _rc, _ev in scored[1:]:
            if s < top_s * 0.95:
                break
            other_strong = self._domain_profiles[idx][0]
            if (other_strong
                    and not (other_strong & top_strong)
                    and other_strong & query_strong):
                return True
        return False

    @staticmethod
    def _cluster_margin(scored: list) -> float:
        """top1 与「次一等候选群」的分差。

        与 top1 得分差距 <5% 的候选视为同一题族（题库大量近重复题），
        分差应跨出题族计算，避免同族并列被误判为「区分度不足」。
        matcher 之 top1/top2 原始分差可通过 rank 输出的 margin 获得。
        """
        s1 = scored[0][0]
        for s, _raw, _q, _c, _i, _rc, _ev in scored[1:]:
            if s < s1 * 0.95:
                return s1 - s
        return s1

    # ---- 兼容旧接口 ----
    def match(self, text: str, top_n: int = 3, min_score: float = 1):
        """返回 [(question_dict, score, hits)]，与旧版三元组结构完全一致。

        score 为归一化分值（0..1 浮点，替代旧版整数命中次数）；
        门控不过（无相关信号/证据不足/区分度不足）时返回空列表。
        """
        return [(r["question"], r["score"], r["hits"])
                for r in self.rank(text, top_n=top_n, min_score=min_score)]
