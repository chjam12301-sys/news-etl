"""国内源改造（2026-10-27）：红线过滤、中文去重、中文打分、feed 瘦身。

背景：用户要求把新闻源从海外媒体换成国内媒体，并明确
「不要那种严肃新闻，特别是红色政治新闻，任何官方发的新闻都不要搞」。
所以这里守三件事：
1. 官方 / 时政稿在抓取阶段就被丢掉，且不能被「全部出局」的兜底捞回来；
2. 中文标题的去重与打分不能退化成空转（原实现只认英文正则）；
3. 换源后 feedparser 不能把一轮跑成几分钟。
"""
from __future__ import annotations

import feedparser

from app import fetcher
from app.config import settings
from app.dedup import content_tokens, entity_signature, is_duplicate, same_event
from app.editorial import blocked_reason, is_official_url
from app.selector import score_title


# --------------------------------------------------------------------------- #
# 一、红线：官方渠道 / 时政 / 严肃新闻
# --------------------------------------------------------------------------- #
def test_official_domain_blocked() -> None:
    assert is_official_url("https://www.people.com.cn/n1/2026/1027/c1001.html")
    assert is_official_url("http://www.news.cn/politics/2026-10/27/c.htm")
    assert is_official_url("https://www.cctv.com/2026/10/27/VIDE.shtml")
    assert not is_official_url("https://www.ifanr.com/1686003")
    assert not is_official_url("https://www.ithome.com/1/018/256.htm")


def test_political_titles_blocked() -> None:
    for title in (
        "习近平会见外国代表团",
        "中共中央政治局召开会议",
        "国务院印发新型城镇化实施方案",
        "外交部就相关问题答记者问",
        "某地发生火灾 已致 3 人遇难",
        # 2026-10-27 抽查补漏：这三条真的从国内源里漏出来过
        "今年 9 月那场联合国大会投票通过了“新的世界地图”，到底是什么情况？",
        "“蛟龙”号载人潜水器实现迭代升级，顺利完成 7000 米级海试",
        "《英雄联盟》传奇选手 Cyanide 去世，终年 31 岁",
    ):
        assert blocked_reason(title=title), title


def test_official_press_release_body_blocked() -> None:
    body = "记者获悉，相关部门已就此作出部署。" + "正文" * 200
    assert blocked_reason(title="某产业大会成功举办", body=body)


def test_normal_consumer_news_not_blocked() -> None:
    for title in (
        "小米米家高速吹风机 Mini 开售：11 万转 / 分钟电机，首销 199.7 元",
        "荣耀 WIN 2 体验：被迫升杯的「游戏机」，这次还能不能赢？",
        "公司总经理谈新品：把本地部署成本打下来",
        "《影之刃零》，真正的六边形战士",
        "星巴克中国门店体验焕新：很少有一个公共议题，大家会集体站“资本家”这边",
        "触乐怪话：修好了一台PS5",
        "25.98 万元起，魏牌高山 8 HEV 上市，想跟燃油 MPV 们掰掰手腕",
    ):
        assert not blocked_reason(title=title), title


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _Client:
    """最小 httpx.Client 替身：fetch_feed 只用 .get().text / raise_for_status。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def get(self, *args, **kwargs) -> _Resp:  # noqa: ANN002, ANN003
        return _Resp(self._text)


def test_fetch_feed_drops_official_items() -> None:
    """红线在候选阶段生效，否则 select() 的「全部出局」兜底会把它捞回来。"""
    rss = (
        '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>t</title>'
        "<item><title>中共中央政治局召开会议</title>"
        "<link>https://example.com/a</link><description>会议指出</description></item>"
        "<item><title>小米新品开售</title>"
        "<link>https://www.people.com.cn/n1/1.html</link><description>官方转载</description></item>"
        "<item><title>小米米家吹风机 Mini 开售</title>"
        "<link>https://example.com/b</link><description>首销 199.7 元</description></item>"
        "</channel></rss>"
    )
    out = fetcher.fetch_feed(fetcher.FEEDS[0], _Client(rss))  # type: ignore[arg-type]
    assert [a.title for a in out] == ["小米米家吹风机 Mini 开售"]


# --------------------------------------------------------------------------- #
# 二、中文去重（原实现把整句当一个 token，中文标题等于没去重）
# --------------------------------------------------------------------------- #
CHINESE_DUP_PAIRS = [
    ("《羊蹄山之魂》开发商 Sucker Punch 裁员 26 人", "羊蹄山之魂开发商宣布裁员 26 人"),
    ("小米米家高速吹风机 Mini 开售：11 万转电机", "米家高速吹风机 Mini 今天开卖"),
    ("三十万玩家请愿 EA 拯救《质量效应》系列", "超22万玩家请愿EA拯救《质量效应》"),
]

CHINESE_NOT_DUP_PAIRS = [
    ("荣耀 WIN 2 体验：被迫升杯的游戏机", "新 iPad mini 发布：5499 元起"),
    ("《GTA6》还没发售，但已经开始重创美国经济了", "苹果回应 iPhone Duo 内屏开裂"),
]


def test_chinese_dedup() -> None:
    for a, b in CHINESE_DUP_PAIRS:
        assert is_duplicate(a, b), (a, b)
    for a, b in CHINESE_NOT_DUP_PAIRS:
        assert not is_duplicate(a, b), (a, b)


def test_chinese_tokens_are_bigrams() -> None:
    """连续中文必须切出多个 token，否则热度比对（≥2 个实词重合）永远是 0。"""
    tokens = content_tokens("小米米家高速吹风机开售")
    assert len(tokens) > 3
    assert "吹风" in tokens


# --------------------------------------------------------------------------- #
# 三、中文打分：汇总型栏目出局，正常选题过线
# --------------------------------------------------------------------------- #
def test_digest_columns_rejected() -> None:
    for title in (
        "派早报：Apple 发布 iPad mini (A20 Pro) 等",
        "早报｜苹果回应 iPhone Duo 内屏开裂/Anthropic 公开上市申请",
        "今日消费资讯：Golden Goose 推出迪士尼联名、苹果将在 29 日开发布会",
        "一周盘点：本月值得关注的 8 款 App",
        "一文读懂：什么是端侧大模型",
        "触乐本周行业大事：从业者怎么看？",
    ):
        _score, _reasons, rejected = score_title(title, "ifanr")
        assert rejected, title


def test_real_chinese_titles_pass() -> None:
    for title, source in (
        ("小米米家高速吹风机 Mini 开售：11 万转 / 分钟电机，首销 199.7 元", "ithome"),
        ("《影之刃零》，真正的六边形战士", "yystv"),
        ("超22万玩家请愿 EA 拯救《质量效应》系列", "gcores"),
        ("除了创作才华，《全力闪耀》告诉你泰勒·斯威夫特成功的背后", "toodaylab"),
    ):
        score, _reasons, rejected = score_title(title, source)
        assert not rejected, title
        assert score >= 0.35, (title, score)


# --------------------------------------------------------------------------- #
# 四、feed 瘦身：feedparser 对超长 content:encoded 是超线性变慢的
# --------------------------------------------------------------------------- #
def _fake_rss(n: int) -> str:
    items = "".join(
        f"<item><title>标题 {i}</title><link>https://example.com/{i}</link>"
        f"<description>摘要 {i}</description>"
        f"<content:encoded><![CDATA[<p>{'正文' * 500}</p>]]></content:encoded></item>"
        for i in range(n)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?><rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>t</title>{items}</channel></rss>"
    )


def test_prepare_feed_xml_trims_and_drops_content() -> None:
    trimmed = fetcher._prepare_feed_xml(_fake_rss(30), limit=12)
    assert "content:encoded" not in trimmed
    parsed = feedparser.parse(trimmed)
    assert len(parsed.entries) == 16          # limit + 4
    assert parsed.entries[0]["title"] == "标题 0"
    assert parsed.entries[0]["summary"] == "摘要 0"


def test_prepare_feed_xml_keeps_short_feed() -> None:
    """条目不够 limit 的短 feed 不裁条目，但 content:encoded 一样要删。"""
    parsed = feedparser.parse(fetcher._prepare_feed_xml(_fake_rss(3), limit=12))
    assert len(parsed.entries) == 3
    assert parsed.entries[-1]["title"] == "标题 2"


# --------------------------------------------------------------------------- #
# 六、source_url 必须是 https（App 端 CdnEntry.key() 的硬约束）
# --------------------------------------------------------------------------- #
class _UpgradeResp:
    def __init__(self, text: str, url: str) -> None:
        self.text = text
        self.url = url
        self.headers = {"content-type": "text/html; charset=utf-8"}

    def raise_for_status(self) -> None:
        return None


class _UpgradeClient:
    """http 直接失败、https 成功的替身，用来验证升级 + 重试。"""

    def __init__(self, html: str) -> None:
        self.html = html
        self.calls: list[str] = []

    def get(self, url, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        self.calls.append(url)
        if not url.startswith("https://"):
            raise RuntimeError("http 不可用")
        return _UpgradeResp(self.html, url)


def _raw(url: str) -> fetcher.RawArticle:
    return fetcher.RawArticle(
        source="toodaylab",
        source_url=url,
        topic="lifestyle",
        title="理想生活实验室测试稿",
        summary="摘要",
        body="",
        image_url=None,
        published_at=None,
        fingerprint="f",
    )


def test_http_source_upgraded_to_https() -> None:
    html = "<html><body><div class='post-content'>" + "中文正文" * 200 + "</div></body></html>"
    client = _UpgradeClient(html)
    raw = _raw("http://www.toodaylab.com/84263")
    fetcher.fetch_full_text(raw, client)  # type: ignore[arg-type]
    # 必须**先**试 https：先试 http 的话站点直接 200 返回、不跳转，就永远拿不到
    # https 地址（极客公园、超能网就是这样被整站丢掉的）
    assert client.calls[0].startswith("https://")
    assert raw.source_url.startswith("https://www.toodaylab.com/84263")


class _HttpOnlyClient:
    """https 不通、只有 http 可用的站点（App 端会拒收，应被丢弃）。"""

    def __init__(self, html: str) -> None:
        self.html = html

    def get(self, url, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if url.startswith("https://"):
            raise RuntimeError("https 不可用")
        return _UpgradeResp(self.html, url)


def test_http_only_source_is_dropped() -> None:
    html = "<html><body><div class='post-content'>" + "中文正文" * 200 + "</div></body></html>"
    raw = _raw("http://www.toodaylab.com/84263")
    fetcher.fetch_full_text(raw, _HttpOnlyClient(html))  # type: ignore[arg-type]
    # 正文抓到了，但地址还是 http —— App 的 CdnEntry.key() 会抛异常，必须丢
    assert not raw.source_url.startswith("https://")
    assert not fetcher._usable(raw)


def test_non_https_article_is_dropped() -> None:
    raw = _raw("http://www.toodaylab.com/84263")
    raw.body = "中文正文" * 200
    assert not fetcher._usable(raw)


# --------------------------------------------------------------------------- #
# 八、CDN 产物里不允许出现源文（中文原文只留在库里做审计）
# --------------------------------------------------------------------------- #
def test_version_detail_carries_no_source_text() -> None:
    """2026-10-27 发现：详情 JSON 一直公开带 body_original，App 从未读过。
    源换成中文媒体后，这等于把国内新闻全文挂到公开 CDN 上。"""
    import datetime as dt
    import json

    from app import exporter
    from app.db import Article, ArticleVersion

    article = Article(
        id=1,
        source="ifanr",
        source_url="https://www.ifanr.com/1686003",
        topic="tech",
        title_original="小米米家高速吹风机 Mini 开售",
        summary_original="小米米家高速吹风机 Mini 开售，首销 199.7 元。",
        body_original="中文原文正文，这一段不该出现在 CDN 上。" * 20,
        published_date=dt.date(2026, 10, 27),
        published_at=dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc),
        fingerprint="a" * 40,
        rewrite_status="done",
        tts_status="done",
    )
    version = ArticleVersion(
        id=1,
        article_id=1,
        level_code="en_a1",
        lang="en",
        level=1,
        level_label="A1 入门",
        title="Xiaomi puts a fast hair dryer on sale",
        body="Xiaomi put a fast hair dryer on sale.",
        paragraphs=["Xiaomi put a fast hair dryer on sale."],
        word_count=8,
        reading_minutes=0.1,
        vocab=[],
        lead="",
        title_zh="小米开卖高速吹风机",
        lead_zh="",
        paragraphs_zh=["小米开卖高速吹风机。"],
    )

    payload = exporter._version_detail(version, article, "")
    for key in ("title_original", "summary_original", "body_original"):
        assert key not in payload, key
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "小米米家高速吹风机 Mini 开售，首销" not in dumped
    # 改写产物本身当然要留着
    assert payload["title"].startswith("Xiaomi")
    assert payload["title_zh"] == "小米开卖高速吹风机"


# --------------------------------------------------------------------------- #
# 七、源与主题口径
# --------------------------------------------------------------------------- #
def test_default_topics_are_domestic_soft_only() -> None:
    topics = [t.strip() for t in settings.topics.split(",") if t.strip()]
    assert topics == ["tech", "lifestyle", "business", "culture"]
    # 已下线的时政/严肃主题不该再出现
    assert not {"world", "science", "health", "sports"} & set(topics)


def test_all_feeds_are_chinese_and_non_official() -> None:
    assert fetcher.FEEDS
    for feed in fetcher.FEEDS:
        assert feed.lang == "zh", feed.key
        assert feed.topic in settings.topics.split(","), feed.key
        assert not is_official_url(feed.url), feed.key


# --------------------------------------------------------------------------- #
# 九、第二批源（2026-10-27 加源）
# --------------------------------------------------------------------------- #
def test_feed_slots_are_shared_by_all_sources(monkeypatch) -> None:
    """以前是「前两个源各 12 条就凑满 CANDIDATE_CAP」，第 3 个之后的源白加。"""
    feeds = [
        fetcher.Feed(f"src{i}", f"源{i}", "tech", f"https://example.com/{i}.xml", lang="zh")
        for i in range(5)
    ]
    seen: list[tuple[str, int]] = []

    def fake_fetch(feed, client, limit=12):  # noqa: ANN001, ANN002, ANN003
        seen.append((feed.key, limit))
        return [
            fetcher.RawArticle(
                source=feed.key,
                source_url=f"https://example.com/{feed.key}/1",
                topic="tech",
                title=f"{feed.key} 发布新款耳机：售价 599 元",
                summary="摘要" * 100,
                body="",
                image_url=None,
                published_at=None,
                fingerprint=feed.key,
            )
        ]

    monkeypatch.setattr(fetcher, "FEEDS_BY_TOPIC", {"tech": feeds})
    monkeypatch.setattr(fetcher, "fetch_feed", fake_fetch)
    monkeypatch.setattr(fetcher, "fetch_signals", lambda client, topics: {})
    monkeypatch.setattr(fetcher, "fetch_full_text", lambda raw, client: "正文" * 300)
    monkeypatch.setattr(fetcher, "_usable", lambda raw: True)

    fetcher.collect(["tech"], per_topic=1)
    assert [k for k, _ in seen] == [f"src{i}" for i in range(5)]
    assert {limit for _, limit in seen} == {fetcher.CANDIDATE_CAP // len(feeds)}


def test_cross_topic_same_event_detected() -> None:
    """同一天 ifanr 与 tmtpost 各写一遍 GPT-6.1：中文措辞差很远，产品名一样。"""
    a = entity_signature("刚刚，GPT-6.1 Terra 骨折价发布，Agent 时代的打工模型来了")
    b = entity_signature("GPT-6.1 Terra发布，评论区：拉完了")
    assert same_event(a, b)

    # 一条标题里只共享泛化品牌词（iPhone）不算同一件事
    c = entity_signature("iPhone Duo 拆解：最好修的折叠 iPhone，就是别撕膜")
    d = entity_signature("这一波新款 iPhone 已经全面发售，看看配件值得入手哪些")
    assert not same_event(c, d)


def test_multi_item_title_rejected() -> None:
    """一条标题里用分号塞了三件事 = 汇总，不是单篇选题。"""
    _score, _reasons, rejected = score_title(
        "苹果发 iPhone 折叠屏官方文档，排查铰链故障；触屏 MacBook 或周三发布；《流浪地球 3》将曝光",
        "geekpark",
    )
    assert rejected == "汇总型栏目"


def test_second_batch_sources_registered() -> None:
    keys = {f.key for f in fetcher.FEEDS}
    assert {"geekpark", "mydrivers", "mydrivers_ev", "smzdm"} <= keys
    # 超能网 https 握手在本环境不通，原文地址只能是 http，已被排除
    assert "expreview" not in keys


def test_pr_release_rejected_but_event_news_kept() -> None:
    """厂商通稿挡掉，但「某游戏在试玩会亮相」这类真事件要留着。"""
    _s, _r, rejected = score_title(
        "BOE（京东方）亮相2026核聚变嘉年华北京站 以创新显示技术力促电竞产业升级", "yystv"
    )
    assert rejected == "厂商通稿"
    for title in (
        "《湮灭之潮》亮相核聚变试玩会与「核电波」：格雯面部效果迎来优化",
        "荣耀WIN2手机评测：手游玩家首选的年度T0电竞夯机",
    ):
        assert not score_title(title, "yystv")[2], title


def test_body_boilerplate_stripped() -> None:
    """站点模板噪声（致谢 / 署名日期 / 图注 / 裸链接）不能混进改写 prompt。"""
    html = (
        '<div id="paragraph">'
        "<p>感谢IT之家网友 某网友 的线索投递！</p>"
        "<p>IT之家 10 月 27 日消息，微软旗下《我的世界》明日登陆 Switch 2。</p>"
        "<p>图｜博客🔗</p>"
        "<p>https://example.com/only-a-link</p>"
        "<p>港服定价 235 港币。</p>"
        "</div>"
    )
    body = fetcher._clean(html)
    assert "线索投递" not in body
    assert "IT之家 10 月 27 日消息" not in body
    assert "图｜" not in body
    assert "only-a-link" not in body
    # 正文句子必须原样留着
    assert "微软旗下《我的世界》明日登陆 Switch 2。" in body
    assert "港服定价 235 港币。" in body


def test_body_boilerplate_keeps_normal_sentences() -> None:
    """清洗不能误伤正文：普通句子里出现「感谢」「图」都得留着。"""
    html = "<p>他感谢团队的支持，并称这张图表很关键。</p>"
    assert fetcher._clean(html) == "他感谢团队的支持，并称这张图表很关键。"
