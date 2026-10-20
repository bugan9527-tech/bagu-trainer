# -*- coding: utf-8 -*-
"""Deterministic knowledge-point tagging for the imported question bank.

The classifier combines visible question text with source paths and a small
repository fallback. It never calls an LLM, so the same question bank always
produces the same labels.
"""
import re


# Ordered rules. Earlier matches become the primary knowledge point.
RULES = [
    ("MCP 与工具调用", "AI 应用", (
        "mcp", "function calling", "tool calling", "工具调用",
        "agent skills", "skills", "结构化输出",
    )),
    ("AI Agent", "AI 应用", (
        "agent", "智能体", "subagent", "multi-agent", "多智能体",
        "agent loop", "harness", "任务编排", "badcase",
    )),
    ("AI 编程与研发效能", "AI 应用", (
        "ai 编程", "ai coding", "claude code", "cursor", "codex", "trae",
        "qoder", "研发效能",
    )),
    ("RAG 与检索增强", "AI 应用", (
        "rag", "检索增强", "向量数据库", "embedding", "召回", "rerank",
        "chunk", "pgvector", "知识库", "向量检索", "ann", "hnsw",
    )),
    ("提示工程与上下文", "AI 应用", (
        "prompt", "提示词", "提示工程", "context engineering",
        "上下文工程", "上下文窗口", "context window", "few-shot",
    )),
    ("AI 评测与可观测性", "AI 应用", (
        "评测", "eval", "golden set", "llm-as-judge", "可观测", "trace",
        "线上灰度", "hallucination", "幻觉率",
    )),
    ("AI 安全与治理", "AI 应用", (
        "prompt injection", "提示注入", "ai 安全", "数据外传",
        "租户隔离", "红队", "模型安全",
    )),
    ("语音 AI", "AI 应用", ("asr", "tts", "vad", "语音", "实时语音")),
    ("大模型基础与训练", "AI 应用", (
        "llm", "大模型", "transformer", "attention", "自注意力", "微调",
        "sft", "lora", "rlhf", "模型压缩", "量化", "蒸馏", "温度",
        "temperature", "top p", "pytorch", "tensorflow",
    )),
    ("机器学习与深度学习", "AI 应用", (
        "机器学习", "深度学习", "神经网络", "cnn", "rnn", "lstm", "gru",
        "svm", "pca", "lda", "聚类", "过拟合", "梯度", "损失函数",
        "激活函数", "目标检测", "人脸", "归一化", "batchsize", "auc",
        "roc", "特征工程", "强化学习", "监督学习", "无监督学习",
    )),
    ("Redis", "数据库", (
        "redis", "rdb", "aof", "哨兵", "redisson", "zset", "bigkey",
        "缓存穿透", "缓存击穿", "缓存雪崩",
    )),
    ("MySQL", "数据库", (
        "mysql", "innodb", "myisam", "b+树", "事务隔离", "mvcc",
        "read view", "binlog", "redo log", "undo log", "explain",
        "慢查询", "索引失效", "最左前缀", "覆盖索引", "回表",
    )),
    ("MongoDB", "数据库", ("mongodb", "mongo", "objectid")),
    ("分布式事务", "后端与架构", (
        "分布式事务", "2pc", "tcc", "saga", "本地消息表", "事务消息",
    )),
    ("分布式锁", "后端与架构", (
        "分布式锁", "redlock", "锁续期", "看门狗",
    )),
    ("高并发与性能", "后端与架构", (
        "高并发", "性能优化", "qps", "tps", "压测", "吞吐量",
        "cpu 飙高", "延迟优化",
    )),
    ("消息队列", "后端与架构", (
        "kafka", "rocketmq", "rabbitmq", "消息队列", "mq", "acks",
        "isr", "partition", "offset", "重复消费", "消息积压",
        "delayqueue",
    )),
    ("分布式系统与高可用", "后端与架构", (
        "分布式", "cap", "base", "consensus", "raft", "paxos",
        "服务发现", "注册中心", "微服务", "rpc", "api 网关",
        "负载均衡", "限流", "熔断", "降级", "重试", "脑裂",
        "主从复制", "一致性", "zookeeper",
    )),
    ("数据库基础", "数据库", (
        "数据库", "sql", "事务", "索引", "连接池", "分库分表",
        "读写分离", "主从延迟", "范式", "acid",
    )),
    ("缓存", "后端与架构", (
        "缓存", "cache", "etag", "304", "cdn", "双写",
        "过期策略", "缓存一致性",
    )),
    ("TLS 与加密", "网络与浏览器", (
        "tls", "ssl", "加密", "证书", "密钥交换", "rsa", "ecdhe",
        "前向安全", "https",
    )),
    ("Web 安全", "网络与浏览器", (
        "xss", "csrf", "sql注入", "sql 注入", "注入攻击", "越权",
        "鉴权", "认证", "授权", "oauth", "安全审计", "同源策略",
    )),
    ("HTTP 与 Web", "网络与浏览器", (
        "http", "rest", "cookie", "session", "jwt", "csrf", "cors",
        "状态码", "幂等", "websocket", "sse", "post 和 get",
        "get 和 post", "post与get", "get与post",
    )),
    ("TCP/IP", "网络与浏览器", (
        "tcp", "udp", "三次握手", "四次挥手", "拥塞控制", "nagle",
        "time_wait", "time-wait", "可靠传输",
    )),
    ("DNS 与网络基础", "网络与浏览器", (
        "dns", "arp", "ping", "ip地址", "网络层", "传输层", "osi",
        "tcp/ip", "host 头", "nat", "dhcp", "icmp",
    )),
    ("浏览器与前端基础", "前端", (
        "浏览器", "dom", "事件循环", "微任务", "宏任务", "同源",
        "跨域", "回流", "重绘", "前端",
    )),
    ("JavaScript", "前端", (
        "javascript", "es6", "闭包", "原型链", "promise", "async",
        "await", "var ", "let ", "const ", "深拷贝", "浅拷贝",
        "正则", "array.from", "array.prototype", "map 函数", "typeof",
        "instanceof", "parseint", "call", "apply", "bind", "prototype",
    )),
    ("React", "前端", ("react", "jsx", "hooks", "virtual dom", "redux")),
    ("Vue", "前端", ("vue", "vuex", "prop")),
    ("TypeScript", "前端", ("typescript", "interface", "类型系统")),
    ("CSS 与布局", "前端", (
        "css", "html", "bfc", "flex", "grid", "布局", "1px",
        "轮播", "tailwind", "伪类", "伪元素",
    )),
    ("Java 并发", "Java", (
        "线程池", "synchronized", "volatile", "cas", "aqs", "并发",
        "threadlocal", "jmm", "happens-before", "atomic",
        "reentrantlock", "future", "completablefuture", "线程安全",
        "悲观锁", "乐观锁", "死锁", "semaphore", "countdownlatch",
        "cyclicbarrier", "phaser", "forkjoin", "executor",
    )),
    ("JVM", "Java", (
        "jvm", "gc", "垃圾回收", "垃圾收集", "类加载", "双亲委派",
        "内存区域", "oom", "类文件", "逃逸分析", "jmap", "jstack",
        "minor gc", "full gc",
    )),
    ("Java 集合", "Java", (
        "arraylist", "linkedlist", "hashmap", "concurrenthashmap",
        "linkedhashmap", "treemap", "weakhashmap", "集合", "fail-fast",
        "iterator", "红黑树",
    )),
    ("Spring", "Java", (
        "spring", "bean", "ioc", "aop", "autowired", "transactional",
        "spring boot", "mvc",
    )),
    ("MyBatis", "Java", ("mybatis", "mapper", "sqlsession")),
    ("Java 基础", "Java", (
        "java", "jdk", "string", "equals", "hashcode", "泛型", "反射",
        "注解", "序列化", "异常", "值传递", "引用传递", "面向对象",
        "抽象类", "接口", "基本类型", "包装类型", "throwable",
    )),
    ("C/C++", "编程语言", (
        "c++", "c语言", "指针", "引用", "const", "malloc", "new/delete",
        "模板", "多态", "虚函数", "struct", "include", "头文件",
        "智能指针", "多重继承", "二义性", "typedef", "define",
        "隐式转换", "预编译", "宏", "动态特性", "动态链接", "静态链接",
        "动态库", "静态库",
        "共享内存", "重载", "重写", "覆盖", "结构体", "联合体",
        "局部变量", "全局变量", "strcat", "strncat", "strcpy",
        "memset", "memcpy", "sizeof", "static", "inline",
        "构造函数", "析构函数", "拷贝",
    )),
    ("Python", "编程语言", (
        "python", "gil", "装饰器", "生成器", "列表生成式", "urllib",
        "列表推导", "字典", "元组", "迭代器",
    )),
    ("动态规划", "算法", ("动态规划", "dp", "背包", "子序列")),
    ("图论与搜索", "算法", (
        "dfs", "bfs", "最短路", "拓扑排序", "dijkstra", "prim",
        "并查集", "回溯", "广度优先", "深度优先",
    )),
    ("排序与二分", "算法", (
        "排序", "二分", "top k", "双指针", "滑动窗口",
    )),
    ("算法与数据结构", "算法", (
        "算法", "数据结构", "链表", "数组", "栈", "队列", "树", "图",
        "堆", "哈希表", "trie", "跳表", "复杂度",
    )),
    ("操作系统", "计算机基础", (
        "操作系统", "虚拟内存", "页表", "内存管理", "文件系统",
        "进程", "线程", "协程", "死锁", "中断", "io多路复用", "bio",
        "nio", "aio", "epoll", "select", "poll", "tlb", "tcb", "pcb",
        "分页", "分段", "地址翻译", "系统调用", "内核",
    )),
    ("构建工具", "工程实践", (
        "maven", "gradle", "bom", "dependencymanagement", "构建工具",
        "npm", "包管理",
    )),
    ("Linux 与运维", "工程实践", (
        "linux", "shell", "awk", "grep", "ssh", "docker", "kubernetes",
        "k8s", "容器", "镜像", "数据卷", "文件描述符",
        "单引号", "双引号",
    )),
    ("测试与质量", "工程实践", (
        "测试", "单测", "集成测试", "自动化", "ci/cd", "验收",
        "质量保障", "代码覆盖率",
    )),
    ("Git 与协作", "工程实践", (
        "git", "rebase", "merge", "分支", "commit", "pull request",
    )),
    ("系统设计与架构", "后端与架构", (
        "系统设计", "架构设计", "高可用系统", "如何设计", "设计一个",
        "技术选型", "领域驱动", "ddd", "设计模式", "重构",
    )),
    ("简历与面试表达", "求职", (
        "简历", "star", "自我介绍", "项目经历", "求职", "跳槽",
        "晋升", "信息差", "嫡系", "反问",
    )),
]


TAG_DOMAIN = {tag: domain for tag, domain, _keywords in RULES}
TAG_ORDER = [tag for tag, _domain, _keywords in RULES]

SOURCE_RULES = [
    ("database/redis", "Redis"),
    ("database/mysql", "MySQL"),
    ("database/mongodb", "MongoDB"),
    ("database/", "数据库基础"),
    ("java/concurrent", "Java 并发"),
    ("java/jvm", "JVM"),
    ("java/collection", "Java 集合"),
    ("java/basis", "Java 基础"),
    ("java/", "Java 基础"),
    ("spring", "Spring"),
    ("mybatis", "MyBatis"),
    ("cs-basics/data-structure", "算法与数据结构"),
    ("cs-basics/network", "DNS 与网络基础"),
    ("cs-basics/operating-system", "操作系统"),
    ("cs-basics/computer-organization", "计算机基础"),
    ("cs-basics/algorithms", "算法与数据结构"),
    ("cs-basics/", "计算机基础"),
    ("high-performance/message-queue", "消息队列"),
    ("high-performance", "高并发与性能"),
    ("distributed-system", "分布式系统与高可用"),
    ("high-availability", "分布式系统与高可用"),
    ("high-quality-technical-articles", "简历与面试表达"),
    ("zhuanlan", "软件工程与协作"),
    ("open-source-project", "软件工程与协作"),
    ("system-design", "系统设计与架构"),
    ("interview-preparation", "简历与面试表达"),
    ("roadmap", "简历与面试表达"),
    ("about-the-author", "简历与面试表达"),
    ("tools/", "构建工具"),
    ("ai-coding", "AI 编程与研发效能"),
    ("ai/agent", "AI Agent"),
    ("ai/rag", "RAG 与检索增强"),
    ("ai/llm-basis", "大模型基础与训练"),
    ("ai/", "大模型基础与训练"),
    ("llm_interview_note/07.强化学习", "机器学习与深度学习"),
    ("llm_interview_note/04.", "大模型基础与训练"),
    ("llm_interview_note/05.", "大模型基础与训练"),
    ("llm_interview_note/01.", "大模型基础与训练"),
    ("llm_interview_note/02.", "大模型基础与训练"),
    ("llm_interview_note/", "大模型基础与训练"),
    ("deeplearning-500-questions/ch18", "系统设计与架构"),
    ("deeplearning-500-questions/ch19", "软件工程与协作"),
    ("deeplearning-500-questions/", "机器学习与深度学习"),
    ("leetcode/thinkings/dynamic-programming", "动态规划"),
    ("leetcode/thinkings", "算法与数据结构"),
    ("leetcode/problems", "算法与数据结构"),
    ("leetcode-master/problems", "算法与数据结构"),
    ("fucking-algorithm/", "算法与数据结构"),
    ("cs-notes/notes/leetcode", "算法与数据结构"),
    ("cs-interview-knowledge-map/network", "DNS 与网络基础"),
    ("cs-interview-knowledge-map/js", "JavaScript"),
    ("cs-interview-knowledge-map/browser", "浏览器与前端基础"),
    ("cs-interview-knowledge-map/safety", "Web 安全"),
    ("cs-interview-knowledge-map/mp", "计算机基础"),
    ("09.mysql篇", "MySQL"),
    ("10.redis篇", "Redis"),
    ("11.mongodb篇", "MongoDB"),
    ("12.zookeeper篇", "分布式系统与高可用"),
    ("cs-notes/notes/java 虚拟机", "JVM"),
    ("interview/", "计算机基础"),
]

REPO_DEFAULT = {
    "JavaGuide": ("Java 与后端", "Java"),
    "advanced-java": ("Java 与后端", "Java"),
    "interview_internal_reference": ("后端面试基础", "计算机基础"),
    "DeepLearning-500-questions": ("机器学习与深度学习", "AI 应用"),
    "Daily-Interview-Question": ("前端开发", "前端"),
    "llm_interview_note": ("大模型基础与训练", "AI 应用"),
    "leetcode-master": ("算法与数据结构", "算法"),
    "leetcode": ("算法与数据结构", "算法"),
    "fucking-algorithm": ("算法与数据结构", "算法"),
    "interview": ("计算机基础", "计算机基础"),
    "CS-Notes": ("计算机基础", "计算机基础"),
    "CS-Interview-Knowledge-Map": ("计算机基础", "计算机基础"),
    "interview_python": ("Python", "编程语言"),
    "architect-awesome": ("系统设计与架构", "后端与架构"),
    "fe-interview": ("前端开发", "前端"),
}


def _contains(text, keyword):
    keyword = keyword.lower()
    if re.fullmatch(r"[a-z0-9_.+/ -]+", keyword):
        return re.search(
            r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])",
            text,
        ) is not None
    return keyword in text


def _repo(question):
    tags = question.get("tags") or []
    return tags[-1] if len(tags) > 1 else "unknown"


def classify_question(question):
    title = " ".join(filter(None, (
        question.get("question") or "",
        question.get("knowledge_point") or "",
    ))).lower()
    paths = " ".join(question.get("source_paths") or []).replace("\\", "/").lower()

    tags, sources = [], []
    for tag, _domain, keywords in RULES:
        if any(_contains(title, keyword) for keyword in keywords):
            tags.append(tag)
            sources.append("title")

    for path_part, tag in SOURCE_RULES:
        if path_part.lower() in paths and tag not in tags:
            tags.append(tag)
            sources.append("source_path")

    if not tags:
        fallback = REPO_DEFAULT.get(
            _repo(question), ("其他技术知识", "其他"))
        tags = [fallback[0]]
        sources = ["repo_default"]

    tags = list(dict.fromkeys(tags))[:3]
    primary = tags[0]
    domain = TAG_DOMAIN.get(primary)
    if domain is None:
        domain = REPO_DEFAULT.get(
            _repo(question), ("其他技术知识", "其他"))[1]
    return {
        "primary_knowledge_tag": primary,
        "knowledge_domain": domain,
        "knowledge_tags": tags,
        "knowledge_tag_source": "+".join(dict.fromkeys(sources)),
        "knowledge_tag_version": "v1",
    }
