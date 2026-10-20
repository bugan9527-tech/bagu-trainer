# -*- coding: utf-8 -*-
"""Matcher held-out evaluation cases.

This file is intentionally separate from ``eval_cases.py``.  The cases below
were fixed before changing matching thresholds or gating rules and must not be
used while tuning those values.

Positive cases use a narrow ``accept`` set: a result is useful only when it is
semantically about the stated engineering problem.  ``forbidden_domains`` is
diagnostic metadata for reporting cross-domain mistakes; it is not added to an
accept set to make a case pass.

All contexts are fictional and derived from interview topics in the bundled
question bank.  No user session, API key, keyring value, or real config is read.
"""

POSITIVE_CASES_HELDOUT = [
    {
        "label": "Spring 事务自调用失效",
        "context": "Spring 里同一个类内部调用带 @Transactional 的方法，异常后事务没有回滚",
        "accept": {"bank-0666", "bank-0968", "bank-0991", "bank-0993", "bank-0985"},
        "domain": "java",
        "forbidden_domains": {"database"},
    },
    {
        "label": "InnoDB MVCC Read View",
        "context": "InnoDB 快照读为什么能读到旧版本，MVCC 里的 Read View 怎么工作",
        "accept": {"bank-0447", "bank-0448", "bank-0665", "bank-5239"},
        "domain": "database",
        "forbidden_domains": {"java", "frontend"},
    },
    {
        "label": "数据库反序更新死锁",
        "context": "两个事务按相反顺序更新 A、B 两行，互相等锁，怎样规避数据库死锁",
        "accept": {"bank-1602", "bank-1765", "bank-2124", "bank-2675"},
        "domain": "database",
        "forbidden_domains": {"cpp", "frontend"},
    },
    {
        "label": "Kafka 消费组重平衡",
        "context": "Kafka 消费组频繁 rebalance，成员来回抖动，怎样减少重平衡",
        "accept": {"bank-0831", "bank-0832"},
        "domain": "messaging",
        "forbidden_domains": {"frontend", "cpp"},
    },
    {
        "label": "Full GC 老年代回收",
        "context": "线上 Java 服务频繁 Full GC，老年代对象一直回收不掉，应该看哪些指标",
        "accept": {"bank-0325", "bank-0326", "bank-1952"},
        "domain": "java",
        "forbidden_domains": {"cpp", "frontend"},
    },
    {
        "label": "线程池拒绝策略",
        "context": "线程池任务队列打满后想尽量不丢任务，应该选择哪种拒绝策略",
        "accept": {"bank-0170", "bank-2208", "bank-2863", "bank-2864"},
        "domain": "java",
        "forbidden_domains": {"database", "frontend"},
    },
    {
        "label": "慢 SQL 执行计划",
        "context": "慢查询用 explain 看执行计划，发现扫描行数远大于返回行数",
        "accept": {"bank-1589", "bank-2148", "bank-2183", "bank-2425", "bank-2426"},
        "domain": "database",
        "forbidden_domains": {"frontend", "cpp"},
    },
    {
        "label": "浏览器微任务顺序",
        "context": "浏览器里 Promise 回调和 setTimeout 谁先执行，微任务如何调度",
        "accept": {"bank-4911", "bank-5044", "bank-5120", "bank-5121"},
        "domain": "frontend",
        "forbidden_domains": {"cpp", "database"},
    },
    {
        "label": "React index key",
        "context": "React 列表使用数组下标作为 key，删除中间项后组件状态串位",
        "accept": {"bank-5088"},
        "domain": "frontend",
        "forbidden_domains": {"cpp", "database"},
    },
    {
        "label": "C++ shared_ptr 循环引用",
        "context": "C++ shared_ptr 相互持有形成循环引用，引用计数无法归零怎么解决",
        "accept": {"bank-4135", "bank-4381", "bank-4382", "bank-4561"},
        "domain": "cpp",
        "forbidden_domains": {"java", "frontend"},
    },
    {
        "label": "缓存数据库双写一致性",
        "context": "更新数据库后删除缓存仍然有短暂旧值，双写一致性应该怎样处理",
        "accept": {"bank-2023", "bank-2886"},
        "domain": "database",
        "forbidden_domains": {"frontend", "cpp"},
    },
    {
        "label": "索引列函数导致全表扫描",
        "context": "MySQL 在索引列上套了日期函数，执行计划从范围扫描变成全表扫描",
        "accept": {"bank-0701", "bank-1930", "bank-1936", "bank-2591", "bank-2831"},
        "domain": "database",
        "forbidden_domains": {"frontend", "cpp"},
    },
    {
        "label": "TCP 三次握手必要性",
        "context": "TCP 建连为什么必须三次握手，只握两次会留下什么问题",
        "accept": {"bank-1067", "bank-1068", "bank-1344", "bank-1375", "bank-4112"},
        "domain": "network",
        "forbidden_domains": {"frontend", "cpp"},
    },
    {
        "label": "HTTP 504 网关超时",
        "context": "网关日志大量 504，后端偶发处理超时，如何区分服务故障和网关超时",
        "accept": {"bank-0433", "bank-2320"},
        "domain": "network",
        "forbidden_domains": {"frontend", "cpp"},
    },
    {
        "label": "Spring 循环依赖",
        "context": "Spring 的 Bean 构造器互相依赖，三级缓存为什么也解决不了",
        "accept": {"bank-0551", "bank-0995", "bank-0996"},
        "domain": "java",
        "forbidden_domains": {"database", "frontend"},
    },
    {
        "label": "CPU 高线程栈定位",
        "context": "Java 进程 CPU 占用 100%，用 top 和 jstack 找忙线程的调用栈",
        "accept": {"bank-0249", "bank-3052"},
        "domain": "java",
        "forbidden_domains": {"frontend", "cpp"},
    },
    {
        "label": "Direct Memory OOM",
        "context": "Java 堆看起来正常但进程 OOM，怀疑 DirectByteBuffer 直接内存泄漏",
        "accept": {"bank-0501", "bank-2776"},
        "domain": "java",
        "forbidden_domains": {"cpp", "frontend"},
    },
    {
        "label": "Docker 容器日志",
        "context": "Docker 容器退出后日志没保留，怎样配置日志驱动并挂载到宿主机",
        "accept": {"bank-0294", "bank-2123"},
        "domain": "operations",
        "forbidden_domains": {"frontend", "cpp"},
    },
]


NEGATIVE_CASES_HELDOUT = [
    # Documentation, comments, prose, and formatting.
    {"label": "补全安装文档", "category": "docs", "no_match": True,
     "context": "更新 README，把安装命令和新手引导补全"},
    {"label": "补 docstring", "category": "docs", "no_match": True,
     "context": "给模块补上 docstring，解释每个参数分别是什么"},
    {"label": "整理变更日志", "category": "docs", "no_match": True,
     "context": "整理 CHANGELOG，把本周发布记录写清楚"},
    {"label": "修改过期注释", "category": "docs", "no_match": True,
     "context": "修改接口注释里的过期示例"},
    {"label": "统一格式", "category": "formatting", "no_match": True,
     "context": "统一代码格式，用 ruff 跑一遍自动格式化"},
    {"label": "修正文档链接", "category": "docs", "no_match": True,
     "context": "修正文档里的链接和中文标点"},
    {"label": "更新 API 手册", "category": "docs", "no_match": True,
     "context": "更新 API 使用手册里的环境变量列表"},
    {"label": "添加 TODO", "category": "comments", "no_match": True,
     "context": "给函数加几行 TODO 注释，暂时不改变逻辑"},
    {"label": "批量改注释", "category": "comments", "no_match": True,
     "context": "重命名变量并批量替换旧注释"},
    {"label": "替换 README 截图", "category": "docs", "no_match": True,
     "context": "把 README 里的截图换成最新界面版本"},
    {"label": "润色发布说明", "category": "docs", "no_match": True,
     "context": "润色发布说明，补充升级注意事项"},
    {"label": "添加版权头", "category": "comments", "no_match": True,
     "context": "给代码文件添加版权头注释"},

    # CSS and visual layout edits.
    {"label": "按钮主色圆角", "category": "style", "no_match": True,
     "context": "把登录页按钮的主色改成深蓝，并加一点圆角"},
    {"label": "卡片间距", "category": "style", "no_match": True,
     "context": "把卡片之间的 margin 从 12px 调到 16px"},
    {"label": "移动端字号间距", "category": "style", "no_match": True,
     "context": "调整移动端导航栏的间距和字号"},
    {"label": "表头背景", "category": "style", "no_match": True,
     "context": "用 Tailwind 类名把表头背景换成浅灰色"},
    {"label": "隐藏横向滚动条", "category": "style", "no_match": True,
     "context": "修复页面横向滚动条，把 overflow-x 设置成 hidden"},
    {"label": "flex 方向", "category": "style", "no_match": True,
     "context": "把这个 div 的 flex 方向改成 column"},
    {"label": "弹窗阴影", "category": "style", "no_match": True,
     "context": "调整弹窗阴影和边框透明度"},
    {"label": "按钮渐变", "category": "style", "no_match": True,
     "context": "hover 时让按钮从蓝色渐变到紫色"},
    {"label": "主题色变量", "category": "style", "no_match": True,
     "context": "更换主题色变量，并同步 logo 颜色"},
    {"label": "栅格列数", "category": "style", "no_match": True,
     "context": "把页面栅格布局从两列改成三列"},
    {"label": "图表配色", "category": "style", "no_match": True,
     "context": "把折线图的颜色改成品牌绿，线条加粗一点"},
    {"label": "图标大小", "category": "style", "no_match": True,
     "context": "把工具栏图标统一改成 24px"},

    # Generic engineering edits without a testable knowledge point.
    {"label": "泛化修 bug", "category": "generic", "no_match": True,
     "context": "修一下这个 bug"},
    {"label": "泛化性能优化", "category": "generic", "no_match": True,
     "context": "优化一下性能"},
    {"label": "泛化重构", "category": "generic", "no_match": True,
     "context": "重构一下这段代码"},
    {"label": "代码优雅", "category": "generic", "no_match": True,
     "context": "把函数改得更优雅一些"},
    {"label": "测试断言", "category": "generic", "no_match": True,
     "context": "修复测试里失败的断言"},
    {"label": "升级依赖", "category": "generic", "no_match": True,
     "context": "升级依赖版本并处理兼容性告警"},
    {"label": "增加参数校验", "category": "generic", "no_match": True,
     "context": "这个功能再加一点参数校验"},
    {"label": "变量可读性", "category": "generic", "no_match": True,
     "context": "把变量名改得更容易理解"},
    {"label": "提取常量", "category": "generic", "no_match": True,
     "context": "提取常量替代魔法数字"},

    # Shared Chinese words in non-technical or unrelated contexts.
    {"label": "UI 锁定文案", "category": "word_sense", "no_match": True,
     "context": "产品需求里的锁定按钮状态文案改成已锁定"},
    {"label": "图表指针颜色", "category": "word_sense", "no_match": True,
     "context": "把仪表盘指针的颜色改深一些"},
    {"label": "桌面清理事务", "category": "word_sense", "no_match": True,
     "context": "处理一下桌面上积压的文件和待办事务"},
    {"label": "缓存文案", "category": "word_sense", "no_match": True,
     "context": "给设置页的缓存清理按钮换一段提示文案"},
    {"label": "会议线程", "category": "word_sense", "no_match": True,
     "context": "把会议讨论线程里的回复按时间排序"},
    {"label": "锁屏壁纸", "category": "word_sense", "no_match": True,
     "context": "换一张电脑锁屏壁纸"},
    {"label": "日志里的指针", "category": "word_sense", "no_match": True,
     "context": "把报表里鼠标指针的样式改成小手"},
    {"label": "文档中的索引", "category": "word_sense", "no_match": True,
     "context": "给产品手册补一个术语索引页"},
    {"label": "事务性邮件", "category": "word_sense", "no_match": True,
     "context": "整理客户发来的事务性邮件"},
]


# These cases exercise domain separation without requiring the matcher to force
# a match when the bundled bank has no sufficiently specific item.
DOMAIN_SAFETY_CASES = [
    {
        "label": "Java NullPointerException",
        "context": "Java 登录接口抛出 NullPointerException，堆栈指向 user.getRole()",
        "accept": {"bank-0187", "bank-0315", "bank-1099", "bank-2355", "bank-2878"},
        "allow_empty": True,
        "domain": "java",
        "forbidden_domains": {"cpp"},
    },
    {
        "label": "Java 锁升级",
        "context": "Java synchronized 的偏向锁、轻量级锁和重量级锁是怎样升级的",
        "accept": {"bank-0504", "bank-1050", "bank-1051", "bank-1052"},
        "allow_empty": False,
        "domain": "java",
        "forbidden_domains": {"database"},
    },
    {
        "label": "数据库共享锁排他锁",
        "context": "MySQL 可重复读隔离级别下，共享锁和排他锁如何阻塞彼此",
        "accept": {"bank-0449", "bank-0699", "bank-1749", "bank-1750"},
        "allow_empty": False,
        "domain": "database",
        "forbidden_domains": {"java"},
    },
    {
        "label": "C++ 空指针悬空指针",
        "context": "C++ 里空指针解引用和对象释放后的悬空指针有什么区别",
        "accept": {"bank-4391", "bank-4505", "bank-4542", "bank-4543", "bank-4601", "bank-4699"},
        "allow_empty": False,
        "domain": "cpp",
        "forbidden_domains": {"java", "frontend"},
    },
    {
        "label": "CSS BFC 概念题",
        "context": "解释 BFC 的布局规则，以及为什么它会影响外边距折叠",
        "accept": {"bank-5092"},
        "allow_empty": False,
        "domain": "frontend",
        "forbidden_domains": {"java", "cpp", "database"},
    },
    {
        "label": "Java 线程池概念题",
        "context": "Java 线程池核心线程数和最大线程数分别何时生效",
        "accept": {"bank-1290", "bank-1397", "bank-1619", "bank-2863"},
        "allow_empty": False,
        "domain": "java",
        "forbidden_domains": {"database", "frontend"},
    },
    {
        "label": "C++ 内存释放后访问",
        "context": "C++ delete 之后仍然访问旧地址，产生 use-after-free 和野指针",
        "accept": {"bank-4391", "bank-4505", "bank-4542", "bank-4543", "bank-4699"},
        "allow_empty": False,
        "domain": "cpp",
        "forbidden_domains": {"java", "frontend"},
    },
    {
        "label": "数据库连接池",
        "context": "MySQL 连接池耗尽导致请求排队，怎样排查连接泄漏",
        "accept": {"bank-1415", "bank-2467", "bank-2505"},
        "allow_empty": False,
        "domain": "database",
        "forbidden_domains": {"frontend", "cpp"},
    },
]
