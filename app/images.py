"""文章配图：Openverse（真 CC0 / Public Domain Mark）。

为什么用 Openverse 而不是 Unsplash/Pexels
------------------------------------------
- Unsplash / Pexels 用的是各自专有 License，**不是标准 CC0**，
  且明确禁止「把图片打包成图片搜索服务」。商用 App 需谨慎。
- Openverse 聚合 Flickr / Wikimedia / NASA 等资源，条目自带
  CC0 或 Public Domain Mark 标记，是**可商用的最干净选择**。
- **无需 API key**（匿名调用有速率限制，但每天几十次的量级完全够）。

失败降级
--------
网络不通 / 无结果时返回None，由调用方退回渐变占位块，
绝不阻塞内容生成。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("images")

OPENVERSE_API = "https://api.openverse.org/v1/images/"

# 主题 → 英文检索词（Openverse 主要索引英文标签，用英文命中率更高）
TOPIC_QUERIES: dict[str, list[str]] = {
    "tech": ["technology computer", "circuit board", "software code", "robot"],
    "business": ["office business", "city skyline", "team meeting", "finance"],
    "science": ["laboratory research", "microscope science", "nature research", "experiment"],
    "health": ["medical health", "hospital care", "wellness exercise", "nutrition"],
    "sports": ["sports action", "athletics stadium", "running race", "team sport"],
    "culture": ["art museum", "music concert", "theatre performance", "architecture"],
    "world": ["city street", "people travel", "world map", "landscape nature"],
}

GENERIC_QUERIES = ["news", "world", "city", "nature", "people"]

# 从标题里抽关键词（补充主题词，提升相关性）
_STOP = {
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "is", "are",
    "was", "were", "be", "been", "it", "its", "as", "at", "by", "from", "with",
    "that", "this", "these", "those", "has", "have", "had", "will", "would", "can",
    "new", "may", "more", "about", "after", "before", "says", "say", "said",
}


def keywords_from_title(title: str, limit: int = 4) -> list[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", title or "")]
    seen: list[str] = []
    for w in words:
        if w in _STOP or w in seen:
            continue
        seen.append(w)
        if len(seen) >= limit:
            break
    return seen


@dataclass(slots=True)
class ImageResult:
    url: str
    title: str = ""
    license: str = ""
    license_version: str = ""
    creator: str = ""
    source: str = ""
    width: int = 0
    height: int = 0

    @property
    def attribution(self) -> str:
        bits = []
        if self.creator:
            bits.append(self.creator)
        if self.license:
            bits.append(f"{self.license}{' ' + self.license_version if self.license_version else ''}")
        if self.source:
            bits.append(f"via {self.source}")
        return " · ".join(bits)


def _pick_best(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从结果里挑最合适的：优先横图、分辨率适中、优先 CC0。"""
    if not items:
        return None

    def score(it: dict[str, Any]) -> tuple:
        w, h = it.get("width") or 0, it.get("height") or 0
        ratio = (w / h) if h else 0
        # 16:9 附近最佳
        ratio_fit = -abs(ratio - 1.6)
        # 分辨率够用即可，过大浪费流量
        res_fit = -abs((w or 1000) - 1600) / 1000
        lic = it.get("license", "")
        lic_bonus = 1 if lic in ("cc0", "pdm") else 0
        return (lic_bonus, ratio_fit, res_fit)

    return max(items, key=score)


async def search_openverse(
    query: str, *, timeout: float = 20.0, page_size: int = 8
) -> ImageResult | None:
    """调用 Openverse 搜图，失败返回 None。"""
    params = {
        "q": query,
        "license": "cc0,pdm",   # 只取 CC0 与 Public Domain Mark
        "page_size": page_size,
        "mature": "false",
    }
    headers = {"User-Agent": f"news-etl/{settings.app_name[:20]}"}

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(OPENVERSE_API, params=params, headers=headers)
            if r.status_code == 429:
                log.warning("[img] Openverse 限流(429)，跳过：%s", query)
                return None
            if r.status_code != 200:
                log.warning("[img] Openverse HTTP %s：%s", r.status_code, query)
                return None
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("[img] Openverse 请求失败 %s：%s", type(exc).__name__, query)
        return None

    results = data.get("results") or []
    log.info("[img] Openverse %r → %d 条（共%s）",
             query, len(results), data.get("result_count"))
    if not results:
        return None

    best = _pick_best(results)
    if not best or not best.get("url"):
        log.warning("[img] %r 有 %d 条但无可用url", query, len(results))
        return None

    return ImageResult(
        url=best["url"],
        title=(best.get("title") or "")[:120],
        license=best.get("license") or "",
        license_version=best.get("license_version") or "",
        creator=(best.get("creator") or "")[:80],
        source=(best.get("source") or "")[:40],
        width=int(best.get("width") or 0),
        height=int(best.get("height") or 0),
    )


def _candidate_queries(topic: str, title: str) -> list[str]:
    """构造检索词：主题词优先，再补标题关键词。"""
    topic = (topic or "").lower()
    base = TOPIC_QUERIES.get(topic, [])
    kws = keywords_from_title(title)

    queries: list[str] = []
    if kws:
        queries.append(" ".join(kws[:2]))          # 标题关键词（最贴切）
    queries.extend(base[:2])                      # 主题常用词
    if kws:
        queries.append(f"{kws[0]} {base[0]}" if base else kws[0])
    queries.append(base[0] if base else GENERIC_QUERIES[0])
    return [q for q in dict.fromkeys(queries) if q]


async def find_image(topic: str, title: str, *, enabled: bool = True) -> ImageResult | None:
    """为文章找一张 CC0 配图。失败返回 None，由调用方降级。"""
    if not enabled or not getattr(settings, "image_fetch_enabled", True):
        return None

    queries = _candidate_queries(topic, title)
    for q in queries[:3]:  # 最多试 3 个查询词，节省配额
        res = await search_openverse(q)
        if res:
            log.info("[img] 命中 %r → %s", q, res.url[:70])
            return res
        await asyncio.sleep(0.3)  # 轻微限速，别把免费接口打爆

    # 标题关键词太具体可能无结果，退到主题词
    for q in GENERIC_QUERIES[:2]:
        res = await search_openverse(q)
        if res:
            log.info("[img] 兜底命中 %r → %s", q, res.url[:70])
            return res

    log.info("[img] 未找到 CC0 配图（%s / %s）", topic, (title or "")[:40])
    return None


def placeholder(topic: str, title: str) -> dict[str, Any]:
    """无图时的占位：稳定渐变色 + 主题标签，供 App 端渲染。"""
    seed = hashlib.sha1(f"{topic}|{title}".encode()).hexdigest()
    hue = int(seed[:4], 16) % 360
    return {
        "type": "gradient",
        "from": f"hsl({hue}, 42%, 62%)",
        "to": f"hsl({(hue + 38) % 360}, 46%, 48%)",
        "label": (topic or "news").upper()[:12],
        "seed": seed[:12],
    }