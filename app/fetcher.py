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
from .editorial import blocked_reason

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


# 正文源：国内市场化媒体，题材是消费电子 / 数字生活 / 游戏文化。
#
# 2026-10-27 用户要求「从国外新闻转到国内新闻」，并明确排除严肃新闻与官方新闻
# （原话：「不要搞那种严肃新闻啊，特别是红色政治新闻，任何官方发的新闻都不要搞」）。
# 因此这里只保留**非官方、非时政**的中文源；人民网 / 中新网 / 新华 / 央视一类的
# 官方源虽然 RSS 稳定可用，但**故意不用**。抓取阶段还有 app/editorial.py 做红线过滤。
#
# 实测（2026-10-27）：8 个源全部 200 且有条目；China Daily RSS 已全线 404，
# Sixth Tone 只回 1 条，SCMP 与本机网络不通，均未采用。
FEEDS: list[Feed] = [
    # 消费电子 / 科技
    Feed("ifanr", "爱范儿", "tech", "https://www.ifanr.com/feed", lang="zh"),
    Feed("ithome", "IT之家", "tech", "https://www.ithome.com/rss/", lang="zh"),
    Feed("geekpark", "极客公园", "tech", "https://www.geekpark.net/rss", lang="zh"),
    Feed("mydrivers", "快科技", "tech", "https://rss.mydrivers.com/rss.aspx?Tid=1", lang="zh"),
    # 超能网（expreview）已排除：它的 https 握手在本环境直接失败
    # （UNEXPECTED_EOF_WHILE_READING，curl 能通但 httpx 不通），
    # 于是所有原文地址只能是 http，全部被 App 端的 https 约束挡掉。
    # 数字生活 / 消费
    Feed("sspai", "少数派", "lifestyle", "https://sspai.com/feed", lang="zh"),
    Feed("toodaylab", "理想生活实验室", "lifestyle", "https://www.toodaylab.com/feed", lang="zh"),
    # 评测频道（Tid=2）：整篇是产品体验，适合做分级阅读
    Feed("mydrivers_ev", "快科技·评测", "lifestyle",
         "https://rss.mydrivers.com/rss.aspx?Tid=2", lang="zh"),
    Feed("smzdm", "什么值得买", "lifestyle", "https://post.smzdm.com/feed/", lang="zh"),
    # 商业消费（钛媒体偏产品与公司，不是政策财经）
    Feed("tmtpost", "钛媒体", "business", "https://www.tmtpost.com/rss", lang="zh"),
    # 游戏 / 文化
    Feed("gcores", "机核", "culture", "https://www.gcores.com/rss", lang="zh"),
    Feed("yystv", "游研社", "culture", "https://www.yystv.cn/rss/feed", lang="zh"),
    Feed("chuapp", "触乐", "culture", "https://www.chuapp.com/feed", lang="zh"),
]

FEEDS_BY_TOPIC: dict[str, list[Feed]] = {}
for _f in FEEDS:
    if _f.purpose == "body":
        FEEDS_BY_TOPIC.setdefault(_f.topic, []).append(_f)

# Google News 只当热度信号：它的 <link> 是 news.google.com/rss/articles/CBMi…
# 跳转壳，跟随重定向拿到的是 Google 自己的 JS 页面（实测 593KB，无正文），
# 解 base64 也只得到不透明 token。所以只取标题，用来判断"这条有多少家媒体在报"。
#
# 源换成中文媒体后，热度信号同步换 zh-CN 版；故意不用 NATION（国内）栏目 ——
# 那个栏目基本是时政通稿，和 editorial.py 的红线冲突。取不到就 heat=0，
# 打分段会自然降级，不影响出稿。
SIGNAL_FEEDS: dict[str, str] = {
    "tech": "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY",
    "business": "https://news.google.com/rss/headlines/section/topic/BUSINESS",
    "culture": "https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT",
}
_SIGNAL_SUFFIX = "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"

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


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_cjk(text: str) -> bool:
    """粗判一段文本是不是中文。中文一个字符承载的信息约为英文的 1.7 倍，
    所以字数门槛要按语言分开设，否则中文稿会被「太短」误杀。"""
    sample = (text or "")[:2000]
    if not sample:
        return False
    return len(_CJK_RE.findall(sample)) / len(sample) >= 0.15


def _extract_body(html: str) -> str:
    """从文章 HTML 抽正文：优先语义容器，再退到各站的主内容 class。

    中文站的容器多数没有 article / itemProp 语义，靠 class 名区分：
    IT之家 #paragraph、理想生活实验室 .post-content、游研社 .doc-content、
    机核 .newsPage。
    """
    soup = BeautifulSoup(html or "", "lxml")
    node = None
    for sel in (
        "article [itemprop='articleBody']",
        "[itemprop='articleBody']",
        "#js_content",           # 微信公众号转载
        "#articleContent",
        "#artibody",
        "#paragraph",            # IT之家
        ".article-content",
        ".article-body",
        ".post-content",         # 理想生活实验室
        ".single-content",
        ".doc-content",          # 游研社
        ".post_content",
        ".entry-content",
        ".rich_media_content",
        ".newsPage",             # 机核
        "article",
        "main article",
        "main",
    ):
        node = soup.select_one(sel)
        if node is not None:
            text = node.get_text(strip=True)
            if len(text) > (320 if _is_cjk(text) else 400):
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


def _usable(raw: RawArticle) -> bool:
    """全文抓回后复核一次：正文是否通稿、长度是否够写一篇分级稿。

    摘要干净、正文是通稿的情况真实存在（转载稿常见），所以红线和长度两道检查
    都放在拿到全文之后再做一次。
    """
    body = raw.body or raw.summary or ""
    why = blocked_reason(title=raw.title, body=body, url=raw.source_url)
    if why:
        log.info("[editorial] 丢弃（正文复核 %s）：%s", why, raw.title[:48])
        return False
    # App 端 CdnEntry.key() 要求 source_url 必须是 https，否则整条目录项解析抛异常。
    # 升级失败就宁可当天少一篇，也不能把一条会炸客户端的数据发出去。
    if not raw.source_url.startswith("https://"):
        log.info("[fetch] 非 https 原文地址，跳过: %s", raw.source_url)
        return False
    if len(body) < (260 if _is_cjk(body) else 400):
        log.info("[fetch] 内容过短，跳过: %s", raw.source_url)
        return False
    return True


_CONTENT_BLOCK = re.compile(r"<content:encoded>.*?</content:encoded>", re.S)


def _prepare_feed_xml(text: str, limit: int) -> str:
    """解析前先把 XML 瘦身，否则 feedparser 会慢到离谱。

    实测（2026-10-27，国内源）：feedparser 对超长 content:encoded（内联数据 URI
    图片）是超线性变慢的 —— ifanr 的 314KB feed 要 36.7s，IT之家 245KB 要 19.9s。
    一轮 4 个主题能白烧好几分钟，而这些 HTML 我们本来也不用（正文单独抓页面）。
    删掉 content:encoded + 只留前 limit+4 条后：ifanr 0.4s、IT之家 0.9s。
    """
    text = _CONTENT_BLOCK.sub("", text or "")
    is_atom = "</entry>" in text
    pattern = r"</entry>" if is_atom else r"</item>"
    end_tag = "</feed>" if is_atom else "</channel></rss>"
    kept = 0
    for m in re.finditer(pattern, text):
        kept += 1
        if kept >= limit + 4:
            return text[: m.end()] + end_tag
    return text


def fetch_feed(feed: Feed, client: httpx.Client, limit: int = 12) -> list[RawArticle]:
    """抓一个 RSS 源，返回原始文章列表。"""
    try:
        resp = client.get(feed.url, headers={"User-Agent": settings.user_agent}, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("[fetch] %s 失败: %s", feed.key, exc)
        return []

    parsed = feedparser.parse(_prepare_feed_xml(resp.text, limit))
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
        # 红线过滤放在这里：官方稿 / 时政稿连候选都不进，不占候选名额，
        # 也就不会在「全部出局」的兜底分支里被捞回来。
        why = blocked_reason(title=title, body=f"{summary}\n{body}", url=link)
        if why:
            log.info("[editorial] 丢弃 %s（%s）：%s", feed.key, why, title[:48])
            continue
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


def _https_variants(url: str) -> list[str]:
    """把 http:// 的原地址换成 https:// 优先试，http 只作兜底。

    理想生活实验室 / 极客公园 / 超能网的 RSS 里给的是 http 链接，而 App 端
    `CdnEntry.key()` 对 source_url 有硬约束：必须 https 开头，否则整条目录项
    解析直接抛异常。**顺序很关键**：先试 http 的话，站点 200 返回、不跳转，
    就永远拿不到 https 地址（实测极客公园就是这样被整站丢掉的）。
    """
    if not url.startswith("http://"):
        return [url]
    return ["https://" + url[len("http://") :], url]


def fetch_full_text(raw: RawArticle, client: httpx.Client) -> str:
    """抓原文正文；失败时回退到 RSS 摘要。"""
    last_exc: Exception | None = None
    for url in _https_variants(raw.source_url):
        try:
            resp = client.get(
                url, headers={"User-Agent": settings.user_agent}, follow_redirects=True
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
        # 回写最终地址：跟随重定向后的 URL 才是权威的，且必须是 https
        final = str(resp.url)
        if final.startswith("https://"):
            raw.source_url = final
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            return raw.body or raw.summary
        text = _extract_body(resp.text)
        need = 400 if _is_cjk(text) else 600
        return text if len(text) > need else (raw.body or raw.summary or text)
    log.info("[fetch] 正文抓取失败 %s: %s", raw.source_url, last_exc)
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
        parsed = feedparser.parse(_prepare_feed_xml(resp.text, 40))
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
    total_limit: int | None = None,
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
    from .dedup import content_tokens, entity_signature, same_event
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

        global_candidates: list[RawArticle] = []
        global_heats: dict[str, int] = {}
        # 已入选文章的「产品/型号」指纹，用做跨主题同事件判重
        picked_sigs: list[set[str]] = []
        for topic in topics:
            feeds = FEEDS_BY_TOPIC.get(topic) or []
            sigs = signals.get(topic, [])

            candidates: list[RawArticle] = []
            # 每个源分到多少条：以前是「前两个源各 12 条就凑满 CANDIDATE_CAP」，
            # 于是第 3 个之后的源根本没被拉过 —— 加新源等于白加。
            # 现在按源数平分名额，保证每个源都有代表作进候选池。
            per_feed = max(3, CANDIDATE_CAP // max(len(feeds), 1))
            for feed in feeds:
                # 够评就行：源越多越慢，而 Actions 一个源卡住要等满 25s。
                if len(candidates) >= CANDIDATE_CAP:
                    break
                try:
                    entries = fetch_feed(feed, client, limit=per_feed)
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
            if total_limit is not None:
                global_candidates.extend(candidates)
                global_heats.update(heats)
                continue
            # 多要几条：同事件 / 非 https / 正文太短都会让某条落选，
            # 只取 per_topic 条的话一被否掉这个主题当天就空了。
            picked, _scored = select(candidates, per_topic + 3, heats)

            got = 0
            for raw in picked:
                if got >= per_topic:
                    break
                # 再查一次：同批里靠前的主题可能刚用掉相近标题
                hit = index.find(raw.title)
                if hit:
                    log.info("[dedup] 二次判重跳过：%s ‖ 已用：%s", raw.title[:60], hit[:60])
                    continue
                # 跨主题同事件：不同媒体各写一遍同一款产品，中文措辞差很远，
                # 但产品名一样 —— 按拉丁实词判，避免同一天出两篇 GPT-6.1。
                sig = entity_signature(raw.title)
                dup_of = next((s for s in picked_sigs if same_event(sig, s)), None)
                if dup_of is not None:
                    log.info("[dedup] 跨主题同事件，跳过：%s", raw.title[:56])
                    continue
                raw.body = fetch_full_text(raw, client)
                if not _usable(raw):
                    continue
                index.add(raw.title)
                picked_sigs.append(sig)
                result.append(raw)
                got += 1
            log.info("[fetch] 主题 %s 得到 %d 篇（候选 %d）", topic, got, len(candidates))
        if total_limit is not None:
            from .selector import rank

            seen_urls: set[str] = set()
            for scored in rank(global_candidates, global_heats):
                if len(result) >= total_limit:
                    break
                raw = scored.raw
                url = _normalize_url(raw.source_url)
                if url in seen_urls or index.find(raw.title):
                    continue
                seen_urls.add(url)
                sig = entity_signature(raw.title)
                if any(same_event(sig, s) for s in picked_sigs):
                    log.info("[dedup] 同事件，跳过：%s", raw.title[:56])
                    continue
                raw.body = fetch_full_text(raw, client)
                if not _usable(raw):
                    continue
                index.add(raw.title)
                picked_sigs.append(sig)
                result.append(raw)
    return result
