"""RSS / 正文抓取。全部使用公开免费 RSS，无需 API key。"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from .config import settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Feed:
    key: str
    name: str
    topic: str
    url: str
    lang: str = "en"


# 每个主题 3 路源，互为备份；均为公开 RSS
FEEDS: list[Feed] = [
    # tech
    Feed("arstechnica", "Ars Technica", "tech", "https://feeds.arstechnica.com/arstechnica/index"),
    Feed("theverge", "The Verge", "tech", "https://www.theverge.com/rss/index.xml"),
    Feed("hnrss", "Hacker News", "tech", "https://hnrss.org/frontpage"),
    # business
    Feed("cnbc", "CNBC", "business", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss25&id=10001147"),
    Feed("ft", "Financial Times", "business", "https://www.ft.com/rss/home"),
    Feed("marketwatch", "MarketWatch", "business", "http://feeds.marketwatch.com/marketwatch/topstories/"),
    # science
    Feed("nature", "Nature", "science", "https://www.nature.com/nature.rss"),
    Feed("phys_org", "Phys.org", "science", "https://phys.org/rss-feed/"),
    Feed("quanta", "Quanta Magazine", "science", "https://api.quantamagazine.org/feed/"),
    # health
    Feed("medical_x", "Medical Xpress", "health", "https://medicalxpress.com/rss-feed/"),
    Feed("who", "WHO", "health", "https://www.who.int/rss-feeds/news-english.xml"),
    Feed("nih", "NIH", "health", "https://www.nih.gov/rss/News_Health.xml"),
    # sports
    Feed("espn", "ESPN", "sports", "http://www.espn.com/espn/rss/news"),
    Feed("skysports", "Sky Sports", "sports", "https://www.skysports.com/rss/12040"),
    Feed("bbc_sport", "BBC Sport", "sports", "http://feeds.bbci.co.uk/sport/rss.xml"),
    # culture
    Feed("bbc_culture", "BBC Culture", "culture", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
    Feed("guardian_culture", "The Guardian Arts", "culture", "https://www.theguardian.com/uk/arts/rss"),
    Feed("npr_books", "NPR Books", "culture", "https://feeds.npr.org/1007/rss.xml"),
    # world
    Feed("bbc_world", "BBC World", "world", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    Feed("aljazeera", "Al Jazeera", "world", "https://www.aljazeera.com/xml/rss/all.xml"),
    Feed("guardian_world", "The Guardian World", "world", "https://www.theguardian.com/world/rss"),
]

FEEDS_BY_TOPIC: dict[str, list[Feed]] = {}
for _f in FEEDS:
    FEEDS_BY_TOPIC.setdefault(_f.topic, []).append(_f)


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


def fetch_feed(feed: Feed, client: httpx.Client) -> list[RawArticle]:
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
    for entry in parsed.entries[:8]:
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


def collect(
    topics: list[str] | None = None, per_topic: int | None = None
) -> list[RawArticle]:
    """按主题各取 N 篇。"""
    topics = topics or settings.topics.split(",")
    topics = [t.strip() for t in topics if t.strip()]
    per_topic = per_topic or settings.articles_per_topic
    headers = {"User-Agent": settings.user_agent}

    result: list[RawArticle] = []
    with httpx.Client(timeout=settings.request_timeout, headers=headers) as client:
        for topic in topics:
            feeds = FEEDS_BY_TOPIC.get(topic) or []
            got = 0
            for feed in feeds:
                if got >= per_topic:
                    break
                for raw in fetch_feed(feed, client):
                    if got >= per_topic:
                        break
                    raw.body = fetch_full_text(raw, client)
                    if len(raw.body or raw.summary) < 400:
                        log.info("[fetch] 内容过短，跳过: %s", raw.source_url)
                        continue
                    result.append(raw)
                    got += 1
            log.info("[fetch] 主题 %s 得到 %d 篇", topic, got)
    return result