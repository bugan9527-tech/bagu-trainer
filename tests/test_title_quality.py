# -*- coding: utf-8 -*-
"""标题清洗规则：噪声拒绝、正常注解保留、Markdown 链接清洗。"""
from bagu_trainer.title_quality import clean_title


def test_unwraps_markdown_and_html_links():
    md = clean_title(
        "[13.3.7 delete和delete[]的区别](https://example.com/a)")
    assert md["rejected"] is False
    assert md["cleaned"] == "13.3.7 delete和delete[]的区别"

    html_link = clean_title('<a href="https://example.com">缓存穿透</a>')
    assert html_link["rejected"] is False
    assert html_link["cleaned"] == "缓存穿透"


def test_rejects_pure_url():
    result = clean_title("https://juejin.cn/post/7039549418663575582")
    assert result["rejected"] is True
    assert result["reason"] == "url_only"


def test_rejects_github_comments_but_keeps_java_annotations():
    comment = clean_title("@alanchanzm 我觉得不对吧")
    assert comment["rejected"] is True
    assert comment["reason"] == "github_comment"

    for title in (
        "@Async 为什么有时不生效？",
        "@Autowired 和 @Resource 有什么区别？",
        "@Component 和 @Bean 的区别是什么？",
        "⭐️@Transactional 常见失效场景有哪些？",
    ):
        result = clean_title(title)
        assert result["rejected"] is False


def test_rejects_generic_and_site_meta_titles():
    for title in (
        "怎么上手",
        "如何加入？",
        "是否收费",
        "为什么选择公众号",
        "JavaGuide 有没有 PDF 版本",
        "Action： 行动。你做了什么",
    ):
        assert clean_title(title)["rejected"] is True


def test_rejects_long_or_code_fragment_titles():
    long_title = "首先这个 mq 得支持可伸缩性吧，" + "扩容和迁移" * 40
    assert clean_title(long_title)["reason"] == "too_long"

    code = "define MAXLEN 500; char arry[MAXLEN]; cin >> arry; 这段代码有问题吗？"
    assert clean_title(code)["reason"] == "code_fragment"


def test_keeps_long_english_technical_question():
    title = (
        "Question 58：What is the difference between an arrow function and "
        "a normal function? The constructor can generate instances using "
        "new, so can the arrow function work? why?"
    )
    assert clean_title(title)["rejected"] is False


def test_keeps_short_technical_titles():
    for title in ("什么是堆", "何为线程？", "栈是什么", "什么是大O"):
        result = clean_title(title)
        assert result["rejected"] is False
        assert result["cleaned"] == title


def test_strips_decorative_emoji_and_whitespace():
    result = clean_title("  ⭐️  缓存穿透是什么？  ")
    assert result["rejected"] is False
    assert result["cleaned"] == "缓存穿透是什么？"
