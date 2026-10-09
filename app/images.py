"""文章配图：多级图源降级，优先 Unsplash。

图源优先级（settings.image_providers，缺省 unsplash,pexels,wikimedia,openverse）
------------------------------------------------------------------------------
1. **Unsplash**  —— 视觉质量最好，商用免费。需 UNSPLASH_ACCESS_KEY。
   走 API 必须同时满足三条硬规则（见 unsplash.com/api-guidelines）：
   a. **hotlink**：只能用 API 返回的 photo.urls，**不许下载转存到自己的 R2/CDN**；
   b. **署名**：显示图片时必须给出摄影师 + Unsplash，并回链摄影师主页；
   c. **回链带 utm**：`?utm_source=<app>&utm_medium=referral`；
   d. 选图时触发一次 `links.download_location`（计数器，不是取图）。
   注：Unsplash License 本身不要求署名，但 **API Guidelines 强制要求**。
2. **Pexels**    —— 需 PEXELS_API_KEY。Pexels License 商用免费、不强制署名。
3. **Wikimedia Commons** —— 无需 key，实测可达。走 `imageinfo` 拿 CC/PD 授权元数据。
4. **Openverse** —— 无需 key，聚合 Flickr/Wikimedia/NASA。境外可达，国内常不通。

防缺图的六道闸
--------------
① 图源链降级：一个源整体不可用（无 key / 连不上 / 限流）自动跳下一个；
② 查询词降级：标题关键词 → 主题词 → 主题泛词 → 通用词，逐层放宽；
③ 瞬时错误重试：每个查询重试 N 次（settings.image_retries）；
④ URL 可达性校验：拿到链接后探活，过滤死链（Openverse 直链失效率高）；
⑤ 已有图不覆盖：重跑不会把已抓到的图清空（除非 force）；
⑥ 渐变占位兜底：全链路失败仍返回稳定渐变块，App 永远有东西可画。

绝不阻塞内容生成：任何异常都吞掉并记录日志。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

import httpx

from .config import settings

log = logging.getLogger("images")

OPENVERSE_API = "https://api.openverse.org/v1/images/"
UNSPLASH_API = "https://api.unsplash.com/search/photos"
PEXELS_API = "https://api.pexels.com/v1/search"
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"

# 主题 → 英文检索词（各图库主要索引英文标签，用英文命中率更高）
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
    provider: str = ""
    title: str = ""
    license: str = ""
    license_version: str = ""
    creator: str = ""
    creator_url: str = ""
    source: str = ""
    source_url: str = ""
    width: int = 0
    height: int = 0
    # Unsplash 选图回执地址（API 准则要求触发一次）
    download_location: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def attribution(self) -> str:
        """一行署名文本（落库到 article.image_credit）。"""
        if self.provider == "unsplash":
            who = self.creator or "Unsplash"
            return f"Photo by {who} on Unsplash"
        if self.provider == "pexels":
            who = self.creator or "Pexels"
            return f"Photo by {who} on Pexels"
        bits = []
        if self.creator:
            bits.append(self.creator)
        if self.license:
            bits.append(f"{self.license}{' ' + self.license_version if self.license_version else ''}")
        if self.source:
            bits.append(f"via {self.source}")
        return " · ".join(bits)

    def as_dict(self) -> dict[str, Any]:
        """给 API/JSON 的结构化署名，App 端照此渲染可点击出处。"""
        return {
            "provider": self.provider,
            "credit": self.attribution,
            "author": self.creator,
            "author_url": self.creator_url,
            "source": self.source,
            "source_url": self.source_url,
            "license": " ".join(x for x in (self.license, self.license_version) if x).strip(),
            "width": self.width,
            "height": self.height,
        }


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _utm(url: str) -> str:
    """给回链加 utm_source / utm_medium（Unsplash API 准则要求）。"""
    if not url:
        return ""
    app = re.sub(r"[^a-z0-9_-]+", "-", (settings.unsplash_app_name or "app").lower()).strip("-")
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}utm_source={app or 'app'}&utm_medium=referral"


async def _get_json(
    url: str, *, params: dict[str, Any], headers: dict[str, str], timeout: float
) -> dict[str, Any] | None:
    """GET JSON + 重试。失败返回 None。"""
    retries = max(0, int(getattr(settings, "image_retries", 1)))
    last = ""
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                r = await client.get(url, params=params, headers=headers)
            if r.status_code == 429:
                log.warning("[img] 限流(429) %s", url)
                return None
            if r.status_code in (401, 403):
                log.warning("[img] 鉴权失败 HTTP %s %s", r.status_code, url)
                return None
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"
            else:
                return r.json()
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        if attempt < retries:
            await asyncio.sleep(0.6 * (attempt + 1))
    log.warning("[img] 请求失败 %s：%s", url, last)
    return None


async def _reachable(url: str, *, timeout: float = 8.0) -> bool:
    """探活：URL 是否真能返回图片。过滤 Openverse/Wikimedia 的死链。"""
    if not getattr(settings, "image_verify_url", True):
        return True
    # 必须带 UA：Wikimedia / upload.wikimedia.org 对无 UA 的请求直接 403
    headers = {"Range": "bytes=0-1023", "User-Agent": settings.user_agent or "news-etl/1.0"}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            if r.status_code >= 400:
                return False
            ctype = (r.headers.get("content-type") or "").lower()
            if ctype.startswith("image/"):
                return True
            # 个别 CDN 对 Range 返回 application/octet-stream
            return r.status_code in (200, 206) and len(r.content) > 0
    except Exception:  # noqa: BLE001
        return False


def _best_by_ratio(items: list[dict[str, Any]], *, prefer_license: Iterable[str] = ()) -> dict[str, Any] | None:
    """挑最合适的：优先授权干净、横图、分辨率适中。"""
    if not items:
        return None
    prefer = set(prefer_license)

    def score(it: dict[str, Any]) -> tuple:
        w, h = it.get("width") or 0, it.get("height") or 0
        ratio = (w / h) if h else 0
        ratio_fit = -abs(ratio - 1.6)              # 16:9 附近最佳
        res_fit = -abs((w or 1000) - 1600) / 1000  # 够用即可，过大浪费流量
        lic_bonus = 1 if str(it.get("license", "")).lower() in prefer else 0
        return (lic_bonus, ratio_fit, res_fit)

    return max(items, key=score)


# --------------------------------------------------------------------------
# Provider 1 · Unsplash（首选）
# --------------------------------------------------------------------------
async def search_unsplash(query: str, *, timeout: float = 15.0) -> ImageResult | None:
    key = (settings.unsplash_access_key or "").strip()
    if not key:
        return None
    headers = {
        "Authorization": f"Client-ID {key}",
        "Accept-Version": "v1",
        "User-Agent": "news-etl/1.0",
    }
    data = await _get_json(
        UNSPLASH_API,
        params={"query": query, "per_page": 10, "orientation": "landscape"},
        headers=headers,
        timeout=timeout,
    )
    if not data:
        return None

    results = data.get("results") or []
    log.info("[img] Unsplash %r → %d 条（共%s）", query, len(results), data.get("total"))
    if not results:
        return None

    best = _best_by_ratio(results)
    urls = (best or {}).get("urls") or {}
    url = urls.get("raw") or urls.get("regular") or urls.get("full")
    if not url:
        return None
    # hotlink：只拼尺寸参数，不下载不转存
    url = f"{url}&w=1600&q=75&fm=jpg&fit=max" if "?" in url else f"{url}?w=1600&q=75&fm=jpg&fit=max"

    user = best.get("user") or {}
    return ImageResult(
        url=url,
        provider="unsplash",
        title=(best.get("alt_description") or best.get("description") or "")[:120],
        license="Unsplash License",
        creator=(user.get("name") or "")[:80],
        creator_url=_utm(user.get("links", {}).get("html") or ""),
        source="Unsplash",
        # 「Unsplash」这个词链到图片详情页（官方示例链首页，链详情页同样满足
        # 「links back to Unsplash」，且给摄影师的曝光更直接）
        source_url=_utm((best.get("links") or {}).get("html") or "https://unsplash.com/"),
        width=int(best.get("width") or 0),
        height=int(best.get("height") or 0),
        download_location=(best.get("links") or {}).get("download_location") or "",
    )


# --------------------------------------------------------------------------
# Provider 2 · Pexels
# --------------------------------------------------------------------------
async def search_pexels(query: str, *, timeout: float = 15.0) -> ImageResult | None:
    key = (settings.pexels_api_key or "").strip()
    if not key:
        return None
    data = await _get_json(
        PEXELS_API,
        params={"query": query, "per_page": 10, "orientation": "landscape"},
        headers={"Authorization": key, "User-Agent": "news-etl/1.0"},
        timeout=timeout,
    )
    if not data:
        return None

    results = data.get("photos") or []
    log.info("[img] Pexels %r → %d 条", query, len(results))
    if not results:
        return None

    best = _best_by_ratio(results)
    if not best:
        return None
    src = best.get("src") or {}
    url = src.get("large") or src.get("original") or best.get("url")
    if not url:
        return None

    return ImageResult(
        url=url,
        provider="pexels",
        title=(best.get("alt") or "")[:120],
        license="Pexels License",
        creator=(best.get("photographer") or "")[:80],
        creator_url=best.get("photographer_url") or "",
        source="Pexels",
        source_url=best.get("url") or "https://www.pexels.com/",
        width=int(best.get("width") or 0),
        height=int(best.get("height") or 0),
    )


# --------------------------------------------------------------------------
# Provider 3 · Wikimedia Commons（无需 key）
# --------------------------------------------------------------------------
def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


async def search_wikimedia(query: str, *, timeout: float = 15.0) -> ImageResult | None:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": 6,
        "gsrlimit": 10,
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata",
        "iiurlwidth": 1600,
    }
    data = await _get_json(
        WIKIMEDIA_API, params=params, headers={"User-Agent": "news-etl/1.0"}, timeout=timeout
    )
    if not data:
        return None

    pages = (data.get("query") or {}).get("pages") or {}
    cands: list[dict[str, Any]] = []
    for p in pages.values():
        info = (p.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        meta = info.get("extmetadata") or {}
        lic = _strip_html((meta.get("LicenseShortName") or {}).get("value", ""))
        cands.append({
            "url": url,
            "title": p.get("title", "").removeprefix("File:")[:120],
            "license": lic,
            "creator": _strip_html((meta.get("Artist") or {}).get("value", ""))[:80],
            "width": info.get("thumbwidth") or info.get("width") or 0,
            "height": info.get("thumbheight") or info.get("height") or 0,
            "descpage": (p.get("imageinfo") or [{}])[0].get("descriptionurl", ""),
        })
    log.info("[img] Wikimedia %r → %d 条", query, len(cands))
    if not cands:
        return None

    best = _best_by_ratio(cands, prefer_license=("cc0", "public domain", "cc by-sa 4.0", "cc by 4.0"))
    return ImageResult(
        url=best["url"],
        provider="wikimedia",
        title=best["title"],
        license=best["license"],
        creator=best["creator"],
        source="Wikimedia Commons",
        source_url=best.get("descpage") or "https://commons.wikimedia.org/",
        # Wikimedia 的 url 已是图片页；Creator 页在 extmetadata 里未必有，不单独存
        width=int(best.get("width") or 0),
        height=int(best.get("height") or 0),
    )


# --------------------------------------------------------------------------
# Provider 4 · Openverse（CC0 / Public Domain Mark，无需 key）
# --------------------------------------------------------------------------
async def search_openverse(
    query: str, *, timeout: float = 20.0, page_size: int = 8
) -> ImageResult | None:
    params = {
        "q": query,
        "license": "cc0,pdm",   # 只取 CC0 与 Public Domain Mark
        "page_size": page_size,
        "mature": "false",
    }
    # HTTP 头只能放 ASCII —— app_name 是中文，直接放会抛 UnicodeEncodeError
    headers = {"User-Agent": "news-etl/1.0 (+https://github.com/news-etl)"}
    data = await _get_json(OPENVERSE_API, params=params, headers=headers, timeout=timeout)
    if not data:
        return None

    results = data.get("results") or []
    log.info("[img] Openverse %r → %d 条（共%s）",
             query, len(results), data.get("result_count"))
    if not results:
        return None

    best = _best_by_ratio(results, prefer_license=("cc0", "pdm"))
    if not best or not best.get("url"):
        log.warning("[img] %r 有 %d 条但无可用url", query, len(results))
        return None

    return ImageResult(
        url=best["url"],
        provider="openverse",
        title=(best.get("title") or "")[:120],
        license=best.get("license") or "",
        license_version=best.get("license_version") or "",
        creator=(best.get("creator") or "")[:80],
        source=(best.get("source") or "")[:40],
        source_url=best.get("foreign_landing_url") or "",
        width=int(best.get("width") or 0),
        height=int(best.get("height") or 0),
    )


# 只登记名字，调用时再解析 —— 便于测试替换与后续扩展
PROVIDER_NAMES: tuple[str, ...] = ("unsplash", "pexels", "wikimedia", "openverse")


def resolve_provider(name: str) -> Callable[..., Awaitable[ImageResult | None]] | None:
    fn = globals().get(f"search_{name}")
    return fn if callable(fn) else None


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------
def _candidate_queries(topic: str, title: str) -> list[str]:
    """构造检索词：标题关键词优先，再补主题词，逐层放宽。"""
    topic = (topic or "").lower()
    base = TOPIC_QUERIES.get(topic, [])
    kws = keywords_from_title(title)

    queries: list[str] = []
    if kws:
        queries.append(" ".join(kws[:2]))          # 标题关键词（最贴切）
    queries.extend(base[:2])                      # 主题常用词
    if kws and base:
        queries.append(f"{kws[0]} {base[0]}")
    if base:
        queries.append(base[0])
    queries.extend(GENERIC_QUERIES[:2])           # 通用兜底词
    return [q for q in dict.fromkeys(queries) if q]


async def _trigger_download(img: ImageResult) -> None:
    """Unsplash API 准则：选图后触发一次 download_location（仅计数）。"""
    if not img.download_location:
        return
    key = (settings.unsplash_access_key or "").strip()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            await client.get(
                img.download_location,
                headers={"Authorization": f"Client-ID {key}", "Accept-Version": "v1"},
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("[img] download 回执失败（不影响出图）: %s", type(exc).__name__)


async def find_image(topic: str, title: str, *, enabled: bool = True) -> ImageResult | None:
    """为文章找配图：按图源优先级 + 查询词逐级降级。失败返回 None，由调用方占位。"""
    if not enabled or not getattr(settings, "image_fetch_enabled", True):
        return None

    order = [
        p.strip().lower()
        for p in (getattr(settings, "image_providers", "") or "unsplash,pexels,wikimedia,openverse").split(",")
        if p.strip()
    ]
    providers = [(n, resolve_provider(n)) for n in order]
    providers = [(n, f) for n, f in providers if f] or [
        (n, resolve_provider(n)) for n in PROVIDER_NAMES
    ]
    queries = _candidate_queries(topic, title)
    timeout = float(getattr(settings, "image_timeout", 15.0))

    for name, provider in providers:
        for q in queries[:4]:
            try:
                res = await provider(q, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                log.warning("[img] %s 异常 %s：%s", name, type(exc).__name__, q)
                res = None
            if not res or not res.url:
                await asyncio.sleep(0.3)
                continue
            if not await _reachable(res.url):
                log.warning("[img] %s 图片不可达，换下一个：%s", name, res.url[:70])
                await asyncio.sleep(0.2)
                continue
            log.info("[img] %s 命中 %r → %s", name, q, res.url[:70])
            await _trigger_download(res)
            return res

    log.info("[img] 全部图源未命中（%s / %s），走渐变占位", topic, (title or "")[:40])
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
