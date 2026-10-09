"""RSS / 正文抓取。全部使用公开免费 RSS，无需 API key。"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from .config import settings
from .dedup import TopicIndex, build_index

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Feed:
    key: str
    name: str
    topic: str
    url: str
    lang: str = "en"
    # body   : 能抓到正文，作为候选稿件
    # signal : 只取标题做热度信号（Google News 的链接是 JS 壳，取不到正文）
    purpose: str = "body"


# 正文源：每个主题 4~5 路，互为备份；均为公开 RSS。
# 排序有讲究：靠前的源会被优先评测，所以把大众新闻编辑部放前面、
# 学术机构源放后面（配合 selector 的源权重，学术稿基本轮不上）。
FEEDS: list[Feed] = [
    # tech
    Feed("bbc_tech", "BBC Technology", "tech", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    Feed("guardian_tech", "The Guardian Tech", "tech", "https://www.theguardian.com/technology/rss"),
    Feed("theverge", "The Verge", "tech", "https://www.theverge.com/rss/index.xml"),
    Feed("arstechnica", "Ars Technica", "tech", "https://feeds.arstechnica.com/arstechnica/index"),
    Feed("hnrss", "Hacker News", "tech", "https://hnrss.org/frontpage"),
    # business
    Feed("bbc_business", "BBC Business", "business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    Feed("cnbc", "CNBC", "business", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss25&id=10001147"),
    Feed("guardian_business", "The Guardian Business", "business", "https://www.theguardian.com/business/rss"),
    Feed("marketwatch", "MarketWatch", "business", "http://feeds.marketwatch.com/marketwatch/topstories/"),
    Feed("ft", "Financial Times", "business", "https://www.ft.com/rss/home"),
    # science
    Feed("bbc_science", "BBC Science", "science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
    Feed("guardian_science", "The Guardian Science", "science", "https://www.theguardian.com/science/rss"),
    Feed("phys_org", "Phys.org", "science", "https://phys.org/rss-feed/"),
    Feed("nature", "Nature", "science", "https://www.nature.com/nature.rss"),
    Feed("quanta", "Quanta Magazine", "science", "https://api.quantamagazine.org/feed/"),
    # health
    Feed("bbc_health", "BBC Health", "health", "https://feeds.bbci.co.uk/news/health/rss.xml"),
    Feed("guardian_science_health", "The Guardian Health", "health", "https://www.theguardian.com/society/health/rss"),
    Feed("medical_x", "Medical Xpress", "health", "https://medicalxpress.com/rss-feed/"),
    Feed("who", "WHO", "health", "https://www.who.int/rss-feeds/news-english.xml"),
    Feed("nih", "NIH", "health", "https://www.nih.gov/rss/News_Health.xml"),
    # sports
    Feed("bbc_sport", "BBC Sport", "sports", "http://feeds.bbci.co.uk/sport/rss.xml"),
    Feed("espn", "ESPN", "sports", "http://www.espn.com/espn/rss/news"),
    Feed("guardian_sport", "The Guardian Sport", "sports", "https://www.theguardian.com/sport/rss"),
    Feed("skysports", "Sky Sports", "sports", "https://www.skysports.com/rss/12040"),
    # culture
    Feed("bbc_culture", "BBC Culture", "culture", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
    Feed("guardian_culture", "The Guardian Arts", "culture", "https://www.theguardian.com/uk/arts/rss"),
    Feed("npr_books", "NPR Books", "culture", "https://feeds.npr.org/1007/rss.xml"),
    # world
    Feed("bbc_world", "BBC World", "world", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    Feed("guardian_world", "The Guardian World", "world", "https://www.theguardian.com/world/rss"),
    Feed("aljazeera", "Al Jazeera", "world", "https://www.aljazeera.com/xml/rss/all.xml"),
]

FEEDS_BY_TOPIC: dict[str, list[Feed]] = {}
for _f in FEEDS:
    if _f.purpose == "body":
        FEEDS_BY_TOPIC.setdefault(_f.topic, []).append(_f)

# Google News 只当热度信号：它的 <link> 是 news.google.com/rss/articles/CBMi…
# 跳转壳，跟随重定向拿到的是 Google 自己的 JS 页面（实测 593KB，无正文），
# 解 base64 也只得到不透明 token。所以只取标题，用来判断"这条有多少外媒在报"。
SIGNAL_FEEDS: dict[str, str] = {
    "tech": "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY",
    "business": "https://news.google.com/rss/headlines/section/topic/BUSINESS",
    "science": "https://news.google.com/rss/headlines/section/topic/SCIENCE",
    "health": "https://news.google.com/rss/headlines/section/topic/HEALTH",
    "sports": "https://news.google.com/rss/headlines/section/topic/SPORTS",
    "culture": "https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT",
    "world": "https://news.google.com/rss/headlines/section/topic/WORLD",
}
_SIGNAL_SUFFIX = "&hl=en-US&gl=US&ceid=US:en"

# 每个主题最多评测多少条候选（够选出 1~3 篇即可，源越多越慢）
CANDIDATE_CAP = 20


def _normalize_url(url: str) -> str:
    """去掉 utm 等跟踪参数与 fragment，作为去重指纹基础。"""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    keep = [q for q in p.query.split("&") if q and not q.lower().startswith(("utm_", "at_", "cmp", "ns_", "ito"))]
    return urlunparse((p.scheme, p.netloc, p.path, p.params, "&".join(keep), ""))


def fingerprint(url: str, title: str) -> str:
    key = f"{_normalize_url(url)}|{(title or '').strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _clean(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "figure", "figcaption", "aside", "nav", "footer", "form", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip()).strip()


def _extract_body(html: str) -> str:
    """从文章 HTML 抽正文：优先 article/main/[itemprop=articleBody]。"""
    soup = BeautifulSoup(html or "", "lxml")
    node = None
    for sel in (
        "article [itemprop='articleBody']",
        "[itemprop='articleBody']",
        "article",
        "main article",
        "main",
        ".article-body",
        ".story-body",
    ):
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 400:
            break
        node = None
    if node is None:
        body = soup.select_one("body")
        return _clean(body.decode_contents() if body else html)[:9000]
    return _clean(node.decode_contents())[:9000]


def _abs_url(base: str, url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urlparse(url)
        if not p.scheme:
            from urllib.parse import urljoin

            return urljoin(base, url)
    except ValueError:
        return None
    return url


def _entry_image(entry, base: str) -> str | None:
    for link in entry.get("links", []) or []:
        if link.get("type", "").startswith("image") and link.get("href"):
            return _abs_url(base, link["href"])
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href") or enc.get("url")
        if href and enc.get("type", "").startswith("image"):
            return _abs_url(base, href)
    for key in ("media_content", "media_thumbnail"):
        val = entry.get(key)
        if isinstance(val, list) and val:
            href = val[0].get("url")
            if href:
                return _abs_url(base, href)
    return None


def _entry_published(entry) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


@dataclass(slots=True)
class RawArticle:
    source: str
    source_url: str
    topic: str
    title: str
    summary: str
    body: str
    image_url: str | None
    published_at: dt.datetime | None
    fingerprint: str
    # Google News 上有多少家外媒在报同一件事（热度信号，0 = 未取到）
    heat: int = 0


def fetch_feed(feed: Feed, client: httpx.Client, limit: int = 12) -> list[RawArticle]:
    """抓一个 RSS 源，返回原始文章列表。"""
    try:
        resp = client.get(feed.url, headers={"User-Agent": settings.user_agent}, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("[fetch] %s 失败: %s", feed.key, exc)
        return []

    parsed = feedparser.parse(resp.text)
    if not parsed.entries:
        log.warning("[fetch] %s 无条目", feed.key)
        return []

    out: list[RawArticle] = []
    for entry in parsed.entries[:limit]:
        link = entry.get("link") or ""
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        summary = _clean(entry.get("summary", "") or entry.get("description", ""))
        body = _clean(entry.get("content", [{}])[0].get("value", "")) if entry.get("content") else ""
        out.append(
            RawArticle(
                source=feed.key,
                source_url=link,
                topic=feed.topic,
                title=title,
                summary=summary[:1200],
                body=body,
                image_url=_entry_image(entry, link),
                published_at=_entry_published(entry),
                fingerprint=fingerprint(link, title),
            )
        )
    return out


def fetch_full_text(raw: RawArticle, client: httpx.Client) -> str:
    """抓原文正文；失败时回退到 RSS 摘要。"""
    try:
        resp = client.get(
            raw.source_url, headers={"User-Agent": settings.user_agent}, follow_redirects=True
        )
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            return raw.body or raw.summary
        text = _extract_body(resp.text)
        return text if len(text) > 600 else (raw.body or raw.summary or text)
    except Exception as exc:  # noqa: BLE001
        log.info("[fetch] 正文抓取失败 %s: %s", raw.source_url, exc)
        return raw.body or raw.summary


@dataclass(slots=True)
class SignalItem:
    """Google News 的一条标题（只做热度，不进正文源）。"""

    title: str
    publisher: str = ""
    tokens: set = field(default_factory=set)


def _signal_url(base: str) -> str:
    return base + "?" + _SIGNAL_SUFFIX.lstrip("&")


def fetch_signals(client: httpx.Client, topics: list[str]) -> dict[str, list[SignalItem]]:
    """取各主题的 Google News 头条，作为「有多少外媒在报」的热度信号。"""
    from .dedup import content_tokens

    out: dict[str, list[SignalItem]] = {}
    for topic in topics:
        base = SIGNAL_FEEDS.get(topic)
        if not base:
            continue
        try:
            resp = client.get(
                _signal_url(base), headers={"User-Agent": settings.user_agent},
                follow_redirects=True,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.info("[signal] %s 不可用(%s)，本轮跳过热度评分", topic, exc)
            continue
        parsed = feedparser.parse(resp.text)
        items: list[SignalItem] = []
        for entry in parsed.entries[:40]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            src = entry.get("source") or {}
            pub = str(src.get("title") or "") if isinstance(src, dict) else ""
            # 标题尾部是 " - Publisher"，比对前必须剥掉，否则实词集合被污染
            clean = content_tokens(title, publisher_suffix=True, publisher=pub)
            if len(clean) < 2:
                continue
            items.append(SignalItem(title=title, publisher=pub, tokens=clean))
        out[topic] = items
        log.info("[signal] %s 取得 %d 条热度标题", topic, len(items))
    return out


def _heat_of(tokens: set[str], signals: list[SignalItem]) -> int:
    """候选标题与多少条 Google News 标题讲的是同一件事（≥2 个实词重合）。"""
    if not tokens or not signals:
        return 0
    return sum(1 for s in signals if len(tokens & s.tokens) >= 2)


def collect(
    topics: list[str] | None = None,
    per_topic: int | None = None,
    recent_titles: list[str] | None = None,
) -> list[RawArticle]:
    """按主题各取 N 篇。

    顺序是有讲究的：**先去重/打分，再抓全文**。
    全文抓取是最贵的一步；旧实现是挨个抓全文再判重，
    等于给每篇重复稿都付了一次网络开销，还把当天额度浪费在头条上。

     ① 取 Google News 热度信号
     ② 拉各源条目（只要标题）
     ③ 跨源 + 跨天去重（recent_titles = 库里近期已用过的标题）
     ④ 打分排序，只给入选的那几条抓正文

    recent_titles 为空时只在当批内去重（CLI 单次调用的场景）。
    """
    from .dedup import content_tokens
    from .selector import select

    topics = topics or settings.topics.split(",")
    topics = [t.strip() for t in topics if t.strip()]
    per_topic = per_topic or settings.articles_per_topic
    headers = {"User-Agent": settings.user_agent}

    index: TopicIndex = build_index(recent_titles or [])
    if index.titles:
        log.info("[fetch] 载入 %d 条历史标题用于去重", len(index.titles))

    result: list[RawArticle] = []
    with httpx.Client(timeout=settings.request_timeout, headers=headers) as client:
        signals = fetch_signals(client, topics)

        for topic in topics:
            feeds = FEEDS_BY_TOPIC.get(topic) or []
            sigs = signals.get(topic, [])

            candidates: list[RawArticle] = []
            for feed in feeds:
                # 够评就行：源越多越慢，而 Actions 一个源卡住要等满 25s。
                # 20 条候选足以选出 1~3 篇，没必要把 5 个源全拉一遍。
                if len(candidates) >= CANDIDATE_CAP:
                    break
                try:
                    entries = fetch_feed(feed, client)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[fetch] %s 拉取异常: %s", feed.key, exc)
                    continue
                for raw in entries:
                    hit = index.find(raw.title)
                    if hit:
                        log.info("[dedup] 重复，跳过：%s ‖ 已用：%s",
                                 raw.title[:60], hit[:60])
                        continue
                    candidates.append(raw)

            if not candidates:
                log.warning("[fetch] 主题 %s 无可用候选（全部重复或源不可用）", topic)
                continue

            heats = {c.title: _heat_of(content_tokens(c.title), sigs) for c in candidates}
            picked, _scored = select(candidates, per_topic, heats)

            got = 0
            for raw in picked:
                if got >= per_topic:
                    break
                # 再查一次：同批里靠前的主题可能刚用掉相近标题
                hit = index.find(raw.title)
                if hit:
                    log.info("[dedup] 二次判重跳过：%s ‖ 已用：%s", raw.title[:60], hit[:60])
                    continue
                raw.body = fetch_full_text(raw, client)
                if len(raw.body or raw.summary) < 400:
                    log.info("[fetch] 内容过短，跳过: %s", raw.source_url)
                    continue
                index.add(raw.title)
                result.append(raw)
                got += 1
            log.info("[fetch] 主题 %s 得到 %d 篇（候选 %d）", topic, got, len(candidates))
    return result