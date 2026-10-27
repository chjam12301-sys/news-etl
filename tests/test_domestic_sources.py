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
from app.dedup import content_tokens, is_duplicate
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
    assert client.calls[0].startswith("http://")
    assert raw.source_url.startswith("https://www.toodaylab.com/84263")


def test_non_https_article_is_dropped() -> None:
    raw = _raw("http://www.toodaylab.com/84263")
    raw.body = "中文正文" * 200
    assert not fetcher._usable(raw)


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
