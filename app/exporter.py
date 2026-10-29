"""静态 JSON 导出：让 App 不经服务器也能读内容（CDN 直读）。

产物结构（全部写在存储的data/ 下）：
    data/index.json              全量索引：首页概览 + 近期文章列表
    data/archive.json            历史日期清单（只给「加载更多」用，无正文）
    data/<date>/index.json        某一天的完整列表（按语言等级索引）
    data/versions/<id>.json       单个版本的详情（含逐词时间轴）

App 两种读法：
  1) 快路径：拉data/index.json，首页直接渲染，之后按需拉详情
  2) 懒加载：只拉 data/<date>/index.json，再拉 versions/<id>.json

时间轴体积提示：单个版本的时间轴约 10–20KB；index.json 只带元信息不含逐词数据，
保证首页包体小。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .audio_store import public_audio_url, public_timeline_url, is_playable_audio
from .config import settings
from .db import Article, ArticleVersion
from .storage import (
    day_index_key,
    get_storage,
    index_key,
    version_key,
)

log = logging.getLogger("exporter")

# 索引里列出的历史日期数（同时决定每次发布重新导出 / 全量校验几天的目录）。
#
# 2026-10-29：原来有效值是 3（`publish_all(topics_days=3)` 写死）。后果是**已经
# 发布出去的历史日期根本进不了索引** —— 仓库里 `content/data/` 已有 15 天，
# App 只看得见 3 天，「加载更多」也翻不到更早的。客户端从这天起改为「终身一次
# 下载、旧文章保留首版」，回看会变多，索引却把能力卡在 3 天，与需求相反。
#
# 上限不是随便取的，而是 `settings.github_keep_days`（当前 14）：超期日目录会被
# `storage.prune()` 从仓库删掉，索引若继续列出它，App 翻到那天就是 404、发布校验
# 也会判失败。所以这里取值必须 ≤ 保留期，`export_index()` 里另有 clamp 兜底。
#
# 想要更长历史（一年可回看）不能只调大这两个数：content/ 的 JSON 每天约 110KB，
# 一年就是 40MB，会顶到 jsDelivr 单仓库 50MB 的硬上限（音频当初就是因此搬去 R2）。
# 那需要把 JSON 也迁到对象存储，并同步改客户端的 base_url —— 属于基建+产品决定，
# 不在本次"一次下载"范围内。
MAX_DAYS_IN_INDEX = 14

# App 经 latest.json 实际拉取的短路径。必须与 data/index.json 保持一致，
# 否则 App 读到的是历史版本（曾出现短路径停在 @0605a4d 的情况）。
INDEX_SHORT_KEY = "index.json"

# 「加载更多」专用的历史日期清单（2026-10-29）。
#
# 为什么单独一个文件：`index.json` 现在同时干两件事 —— `latest` 是首屏要显示
# 的那一天，`dates` 是「还能往回翻到哪」。两者诉求不同（一个要**内容**、一个
# 要**目录**），却共用一个数组，于是服务端为了支持分页，必须把整个保留期都塞进
# 首屏文件；调整首屏窗口就会顺手改变「能翻多远」。当天改成 14 天时就是这么
# 撞上的。拆出来之后：index 只管首屏，archive 只管翻页，各自独立演进。
#
# 形态是**静态文件而不是带参数的 API**：内容托管在 GitHub + jsDelivr，query
# 不参与路由（客户端自己就在用 `?t=<stamp>` 做 cache busting，正是它不路由的
# 证据）。要做 `?before=&limit=` 就得前面挂一个 Worker，为一个纯静态清单付
# 运行时成本和单点故障，不划算。
ARCHIVE_KEY = "data/archive.json"
ARCHIVE_SHORT_KEY = "archive.json"


def _audio_meta(v: ArticleVersion, base: str, rev: str = "") -> dict[str, Any] | None:
    a = v.audio
    if not is_playable_audio(a):
        return None
    # 音频托管在对象存储：URL 与 commit 无关，永久稳定（内容更新由
    # content_hash 驱动，不靠换 URL）。只有未配置对象存储时才退回 CDN 路径。
    url = public_audio_url(
        file_path=a.file_path or "",
        article_id=v.article_id,
        level_code=v.level_code,
    )
    if not url:
        ext = (a.file_path or "").rsplit(".", 1)[-1] if "." in (a.file_path or "") else "mp3"
        url = (
            f"{base}/audio/{v.article_id}/{v.level_code}.{ext}"
            if base else f"/api/v1/audio/{a.id}.mp3"
        )
    meta = {
        "id": a.id,
        "url": url,
        "duration": round(a.duration_ms / 1000, 3),
        "size_bytes": a.size_bytes,
        "engine": a.engine,
        "voice": a.voice,
        "word_count": len(a.timeline or []),
        "has_timeline": bool(a.timeline),
    }
    # 可选音色（默认音色之外）。时间轴**不内联**，只给 timeline_url：
    # 内联 4 份会让最大等级的详情从 ~59KB 涨到 ~217KB，而用户通常并不切音色。
    alts: list[dict[str, Any]] = []
    for alt in (v.alt_audios or []):
        alt_url = alt.public_url or public_audio_url(file_path=alt.file_path or "")
        if not alt_url:
            continue
        item = {
            "voice": alt.voice,
            "url": alt_url,
            "duration": round(alt.duration_ms / 1000, 3),
            "size_bytes": alt.size_bytes,
            "engine": alt.engine,
            "word_count": len(alt.timeline or []),
            "has_timeline": bool(alt.timeline),
        }
        # 只有真有词级时间轴才给地址 —— 否则 App 会去拉一个必然 404 的 JSON。
        # URL 以库里存的为准（生成时与音频同一基址写死），现拼只是历史行的兜底。
        if alt.timeline:
            item["timeline_url"] = (
                alt.timeline_url
                or public_timeline_url(v.article_id, v.level_code, alt.voice)
            )
        alts.append(item)
    if alts:
        meta["alternates"] = alts
    return meta


def _image_meta(a: Article) -> dict[str, Any]:
    """配图信息。

    只对外暴露图源抓来的合规图（unsplash / pexels / wikimedia / openverse）。
    抓不到时返回渐变占位描述，App 可直接渲染色块。
    非合规的新闻原图不对外输出 —— 商用有版权风险，且热链易失效。

    `type=photo` 时附 `fallback` 渐变块：Unsplash/Pexels 走 hotlink，
    App 侧网络取不到图（onError）时直接画它，保证永不空白。
    """
    from .images import placeholder

    ph = placeholder(a.topic, a.title_original)
    url = (a.image_url or "").strip()
    prov = (a.image_provider or "").strip().lower()

    # 白名单判定：**只有 provider 明确是合规图源才输出实图**。
    # provider 为空 = 抓取阶段写进来的新闻原图（各家 CDN 域名不一，黑名单兜不住），
    # 一律降级为渐变块，等 pipeline 重抓成合规图。
    if url and prov in _SAFE_IMAGE_PROVIDERS:
        return {
            "type": "photo",
            "url": url,
            "provider": prov,
            # 署名渲染成：Photo by <作者→credit_url> on <图源→source_url>
            "credit": a.image_credit or "",           # 全文，兜底用
            "author": a.image_author or "",            # 作者名
            "credit_url": a.image_credit_url or "",   # 摄影师主页
            "source": _PROVIDER_LABEL.get(prov, prov),              # 显示名，如 Unsplash
            "source_url": a.image_source_url or "",   # 图片详情页
            "fallback": {"type": "gradient", **{k: v for k, v in ph.items() if k != "type"}},
        }
    return ph


# 允许对外输出的图源（与 images.PROVIDER_NAMES 对齐）
_SAFE_IMAGE_PROVIDERS = {"unsplash", "pexels", "wikimedia", "openverse"}

# 图源标识 → 显示名（署名里「on ___」那个词）
_PROVIDER_LABEL = {
    "unsplash": "Unsplash",
    "pexels": "Pexels",
    "wikimedia": "Wikimedia Commons",
    "openverse": "Openverse",
}


def _version_summary(v: ArticleVersion, a: Article, base: str, rev: str = "") -> dict[str, Any]:
    from .content_hash import from_version

    return {
        # App 用它判断是否需要重新下载：相同 → 读本地；不同 → 下载
        "content_hash": (v.content_hash if v.audio is None or is_playable_audio(v.audio) else "") or from_version(v, v.audio),
        "version_id": v.id,
        "article_id": v.article_id,
        "level_code": v.level_code,
        "lang": v.lang,
        "level": v.level,
        "level_label": v.level_label,
        "title": v.title,
        "title_zh": v.title_zh or "",
        # App 用它判断能否显示译文，避免"无译文却看不出原因"
        "has_translation": bool(v.title_zh and v.paragraphs_zh),
        "topic": a.topic,
        "source": a.source,
        "source_url": a.source_url,
        "image": _image_meta(a),
        "published_date": a.published_date.isoformat(),
        "word_count": v.word_count,
        "reading_minutes": v.reading_minutes,
        "lead": v.lead,
        "lead_zh": v.lead_zh or "",
        "preview": (v.paragraphs[0] if v.paragraphs else "")[:140],
        "preview_zh": (v.paragraphs_zh[0][:140] if v.paragraphs_zh else ""),
        "has_audio": is_playable_audio(v.audio),
        "audio": _audio_meta(v, base, rev),
        # 详情地址，App 按需拉取
        "detail_url": (
            f"{base}/data/versions/{v.id}.json" if base
            else f"/api/v1/versions/{v.id}"
        ),
    }


def _version_detail(v: ArticleVersion, a: Article, base: str, rev: str = "") -> dict[str, Any]:
    from .tts import normalize_for_tts

    d = _version_summary(v, a, base, rev)
    d.update(
        {
            "paragraphs": v.paragraphs or [],
            "paragraphs_zh": v.paragraphs_zh or [],
            "has_translation": bool(
                v.title_zh and v.paragraphs_zh
                and len(v.paragraphs_zh) == len(v.paragraphs or [])
            ),
            "body": v.body,
            "body_zh": "\n\n".join(v.paragraphs_zh) if v.paragraphs_zh else "",
            # 时间轴 cs/ce 的坐标系：段落换行被压成空格后的单行文本。
            # App 端高亮必须用这个字段，不能直接用 body。
            "text": normalize_for_tts(v.body),
            "vocab": v.vocab or [],
            # 注意：这里**不能**再带 title_original / summary_original / body_original。
            # 2026-10-27 源换成中文媒体后发现，这三个字段一直随详情 JSON 公开到 CDN，
            # 而 App 侧从头到尾没读过它们 —— 等于把抓来的原文全文（现在是中文报道）
            # 挂在了公开地址上。原文留在库里做审计用即可。
            "fetched_at": a.fetched_at.isoformat() if a.fetched_at else None,
            "timeline": (v.audio.timeline if is_playable_audio(v.audio) else []) or [],
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
    )
    return d



def _cdn_base_with_rev(storage, base: str, rev: str = "") -> tuple[str, str]:
    """把 CDN 基址里的 @main 换成 commit SHA。

    jsDelivr 对分支名（@main）会缓存较久，实测客户端会拿到旧版数据
    （timeline 为空）。改用 commit SHA 后，每次内容更新都是全新 URL。

    `rev` 显式传入时用它；否则取当前 HEAD —— 注意此时 HEAD 尚不包含
    刚导出的文件，必须由调用方在内容提交后再调用（见 app/publish.py）。
    """
    if not rev and getattr(storage, "provider", "") == "github":
        try:
            rev = storage.current_ref()
        except Exception:  # noqa: BLE001
            rev = ""
    if rev and "@main" in base:
        base = base.replace("@main", f"@{rev}")
    return base, rev

def export_version(db: Session, v: ArticleVersion, a: Article) -> str:
    """导出单个版本详情（含时间轴）。返回存储 key。"""
    st = get_storage()
    base, rev = _cdn_base_with_rev(st, (settings.public_base_url or "").rstrip("/"))
    key = version_key(v.id)
    payload = _version_detail(v, a, base, rev)
    st.put(key, json.dumps(payload, ensure_ascii=False).encode("utf-8"), content_type="application/json; charset=utf-8")
    return key


def export_day(db: Session, date: dt.date, *, rev: str = "", export_details: bool = True) -> str:
    """导出某天的完整列表。按 语言/等级 分组，App 可整日缓存。"""
    st = get_storage()
    base, rev = _cdn_base_with_rev(st, (settings.public_base_url or "").rstrip("/"), rev)

    rows = db.execute(
        select(ArticleVersion, Article)
        .join(Article, Article.id == ArticleVersion.article_id)
        .options(selectinload(ArticleVersion.audio), selectinload(ArticleVersion.alt_audios))
        .where(Article.published_date == date)
        .order_by(Article.id, ArticleVersion.lang, ArticleVersion.level)
    ).all()

    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    articles: dict[int, dict[str, Any]] = {}

    for v, a in rows:
        art = articles.get(a.id)
        if art is None:
            art = {
                "article_id": a.id,
                "source": a.source,
                "source_url": a.source_url,
                "topic": a.topic,
                "image_url": a.image_url,
                "published_date": a.published_date.isoformat(),
                "published_at": a.published_at.isoformat() if a.published_at else None,
            }
            articles[a.id] = art

        grouped.setdefault(v.lang, {}).setdefault(v.level_code, []).append(
            _version_summary(v, a, base, rev)
        )
        if export_details:
            export_version(db, v, a)

    payload = {
        "date": date.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "article_count": len(articles),
        "version_count": len(rows),
        "articles": list(articles.values()),
        "versions": grouped,
    }
    key = day_index_key(date.isoformat())
    st.put(key, json.dumps(payload, ensure_ascii=False).encode("utf-8"), content_type="application/json; charset=utf-8")
    log.info("[export] %s 导出 %d 篇 / %d 版本", date, len(articles), len(rows))
    return key


def export_index(db: Session, days: int = MAX_DAYS_IN_INDEX, rev: str = "") -> str:
    """导出全量索引。首页只需要这个文件。

    `rev` 用于指定生成链接所用的 commit —— 必须在详情与音频**已提交**
    之后调用，否则链接会指向不包含这些文件的旧 commit（曾导致详情 404）。

    `days` 会被 `settings.github_keep_days`（仓库保留期）截断：索引里出现
    一份已被 prune 掉的日目录，等于给 App 发一个必然 404 的地址。
    """
    keep = int(getattr(settings, "github_keep_days", 0) or 0)
    if keep > 0 and int(days) > keep:
        log.warning("[export] 索引天数 %d 超过仓库保留期 %d，按保留期截断", int(days), keep)
        days = keep
    days = max(1, int(days))

    st = get_storage()
    base, rev = _cdn_base_with_rev(
        st, (settings.public_base_url or "").rstrip("/"), rev
    )

    dates = db.execute(
        select(Article.published_date)
        .distinct()
        .order_by(Article.published_date.desc())
        .limit(days)
    ).scalars().all()
    date_list = [d for d in dates if d]

    # 首页默认展示：最近一天
    latest: dict[str, Any] = {}
    for d in date_list[:1]:
        rows = db.execute(
            select(ArticleVersion, Article)
            .join(Article, Article.id == ArticleVersion.article_id)
            .options(selectinload(ArticleVersion.audio), selectinload(ArticleVersion.alt_audios))
            .where(Article.published_date == d)
            .order_by(Article.id, ArticleVersion.lang, ArticleVersion.level)
        ).all()
        by_lang: dict[str, list[dict[str, Any]]] = {}
        for v, a in rows:
            by_lang.setdefault(v.lang, []).append(_version_summary(v, a, base, rev))
        latest = {"date": d.isoformat(), "versions": by_lang}

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()

    # 内容指纹：随内容变化，App 可据此判断拉到的索引是不是最新的
    content_hash = hashlib.sha256(
        json.dumps(
            {lg: [i["content_hash"] for i in items] for lg, items in by_lang.items()},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]

    payload = {
        "service": settings.app_name,
        "generated_at": generated_at,
        "latest": latest,
        "dates": [d.isoformat() for d in date_list],
        "base_url": base or "",
        "index_url": f"{base}/{index_key()}" if base else "",
        "version": {
            "published_at": generated_at,
            "content_hash": content_hash,
            "tip": "本文件即最新版；若published_at 明显早于当前时间，说明命中了 CDN 旧缓存",
        },
        "levels": [
            {"code": c, "label": lb, "lang": lg, "level": lv}
            for c, lb, lg, lv in _all_levels()
        ],
    }
    key = index_key()
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    st.put(key, raw, content_type="application/json; charset=utf-8")
    # 短路径 content/index.json 是 App 经 latest.json 实际拉取的那个地址。
    # 只写 data/index.json 会让短路径停留在旧版本（曾导致 App 读到陈旧索引）。
    short = INDEX_SHORT_KEY
    if short != key:
        st.put(short, raw, content_type="application/json; charset=utf-8")
    log.info("[export] 全量索引已更新，%d 天（%s + %s）", len(date_list), key, short)
    return key


def export_archive(db: Session, days: int | None = None, rev: str = "") -> str:
    """导出「加载更多」专用的历史日期清单（`data/archive.json` + 短路径同名文件）。

    只回答一件事：**保留期内还有哪些日期可以往回翻**，以及还有多少。
    不含任何正文字段 —— 客户端拿到清单后按日期去取 `data/<date>/index.json`，
    正文仍然是点进去才下载，一次一篇。

    为什么单独一个文件而不是塞进 `index.json`：见 `ARCHIVE_KEY` 上方的说明。
    简单说，index 的 `dates` 同时被首屏窗口和分页深度共用，改一个必然动另一个。

    `days` 缺省取仓库保留期（`settings.github_keep_days`）：清单里列一天已被
    `storage.prune()` 删掉的日期，就是给 App 发一个必然 404 的地址。

    客户端契约：
      - 点「加载更多」时才拉，一天最多拉一次，可落盘复用；
      - 用 `oldest` / `dates` 与本地已有日期求差集，不必信任任何页码；
      - 清单里没有的日期就是**确实没有了**（保留期外），不要再回头试。
    """
    keep = int(getattr(settings, "github_keep_days", 0) or 0)
    want = int(days) if days is not None else (keep or MAX_DAYS_IN_INDEX)
    if keep > 0 and want > keep:
        log.warning("[export] archive 天数 %d 超过仓库保留期 %d，按保留期截断", want, keep)
        want = keep
    want = max(1, want)

    st = get_storage()
    base, rev = _cdn_base_with_rev(
        st, (settings.public_base_url or "").rstrip("/"), rev
    )

    rows = db.execute(
        select(Article.published_date)
        .distinct()
        .order_by(Article.published_date.desc())
        .limit(want)
    ).scalars().all()
    date_list = [d.isoformat() for d in rows if d]

    # 清单只列**日目录真的在**的日期：库里有一条记录、但导出/提交失败过的那天，
    # 列进清单就是给 App 一个必然 404 的游标。storage 没有 exists() 时不做过滤
    # （宁可多列，不要因为探测能力缺失把所有日期都丢掉）。
    if date_list:
        try:
            kept = [d for d in date_list if st.exists(day_index_key(d))]
            if len(kept) != len(date_list):
                log.warning("[export] 历史清单剔除 %d 个无日目录的日期：%s",
                            len(date_list) - len(kept),
                            sorted(set(date_list) - set(kept)))
            date_list = kept
        except Exception as exc:  # noqa: BLE001
            log.debug("[export] 跳过日目录存在性过滤（%s）", exc)

    payload = {
        "service": settings.app_name,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "count": len(date_list),
        "newest": date_list[0] if date_list else "",
        "oldest": date_list[-1] if date_list else "",
        "dates": date_list,
        "retention_days": keep,
        "base_url": base or "",
        "contract": (
            "只用于「加载更多」：按日期游标切页，不要用页码；"
            "清单外的日期已过保留期，不会再有。"
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    st.put(ARCHIVE_KEY, raw, content_type="application/json; charset=utf-8")
    if ARCHIVE_SHORT_KEY != ARCHIVE_KEY:
        st.put(ARCHIVE_SHORT_KEY, raw, content_type="application/json; charset=utf-8")
    log.info("[export] 历史清单已更新，%d 天（%s ~ %s）",
             len(date_list), payload["oldest"], payload["newest"])
    return ARCHIVE_KEY


def _all_levels():
    from .levels import ALL_LEVELS

    return [(lv.code, lv.label, lv.lang, lv.level) for lv in sorted(ALL_LEVELS, key=lambda x: (x.lang, x.level))]


def export_all(db: Session, days: int = 3) -> dict[str, Any]:
    """跑完流水线后统一导出。

    `days` 决定覆盖多少个发行日 —— 回填场景（--warmup-days N）的窗口可能
    大于常规的 3 天，若这里写死 3，第 4 天及更早的改动就永远不会落进 Git，
    等于对象传了、详情没变。
    """
    if not settings.export_json:
        log.info("[export] export_json=false，跳过")
        return {"exported": False}

    try:
        span = max(1, int(days))
        dates = db.execute(
            select(Article.published_date).distinct().order_by(Article.published_date.desc()).limit(span)
        ).scalars().all()
        day_keys = [export_day(db, d) for d in dates if d]
        idx = export_index(db)
        # 短路径 content/index.json 已由 export_index 一并写入（App 拉的是这个
        # 路径，只写 data/index.json 会让短路径长期停留在旧版本）。
        # 历史清单跟着一起刷新：它就是「有哪些日目录」的镜像，只更新索引会让
        # 清单落后于实际内容（导出单独被调用时尤其明显）。
        arc = export_archive(db)
        return {"exported": True, "day_keys": day_keys, "index_key": idx,
                "archive_key": arc}
    except Exception as exc:  # noqa: BLE001
        log.error("[export] 失败: %s", exc)
        return {"exported": False, "error": str(exc)}