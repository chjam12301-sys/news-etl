"""静态 JSON 导出：让 App 不经服务器也能读内容（CDN 直读）。

产物结构（全部写在存储的data/ 下）：
    data/index.json              全量索引：每天的概览 + 近期文章列表
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

from .config import settings
from .db import Article, ArticleVersion
from .storage import (
    day_index_key,
    get_storage,
    index_key,
    version_key,
)

log = logging.getLogger("exporter")

MAX_DAYS_IN_INDEX = 60


def _audio_meta(v: ArticleVersion, base: str, rev: str = "") -> dict[str, Any] | None:
    a = v.audio
    if not a:
        return None
    # 不用 a.public_url —— 它是入库时写的旧地址（可能还带 @main）。
    # 统一用本次导出的 base（已替换成 commit SHA），保证 URL 与本次提交一致。
    url = (
        f"{base}/audio/{v.article_id}/{v.level_code}.mp3"
        if base else f"/api/v1/audio/{a.id}.mp3"
    )
    return {
        "id": a.id,
        "url": url,
        "duration": round(a.duration_ms / 1000, 3),
        "size_bytes": a.size_bytes,
        "engine": a.engine,
        "voice": a.voice,
        "word_count": len(a.timeline or []),
        "has_timeline": bool(a.timeline),
    }


def _image_meta(a: Article) -> dict[str, Any]:
    """配图信息。

    对外只暴露 **CC0 / Public Domain** 图片（Openverse 抓的，credit 里带署名）。
    抓不到时返回渐变占位描述，App 可直接渲染色块。
    非 CC0 的新闻原图不对外输出 —— 商用有版权风险，且热链易失效。
    """
    from .images import placeholder

    url = (a.image_url or "").strip()
    # 双重保险：即使是媒体域名（cdn.arstechnica.net 等）也不对外给
    if url and _is_cc0_safe(url):
        return {
            "type": "photo",
            "url": url,
            "credit": a.image_credit or "",
        }
    ph = placeholder(a.topic, a.title_original)
    return {"type": "gradient", **ph}


# 已知媒体图床域名（含即视为非 CC0）
_MEDIA_HOSTS = (
    "arstechnica", "bbc.", "nytimes", "theguardian", "cnn.", "reuters",
    "washingtonpost", "bloomberg", "ft.com", "economist", "wsj",
    "npr.org", "aljazeera", "cnbc", "forbes", "wired", "theverge",
)


def _is_cc0_safe(url: str) -> bool:
    """粗筛：媒体图床域名一律视为非 CC0。Openverse 结果不在此列表内。"""
    low = url.lower()
    return not any(h in low for h in _MEDIA_HOSTS)


def _version_summary(v: ArticleVersion, a: Article, base: str, rev: str = "") -> dict[str, Any]:
    from .content_hash import from_version

    return {
        # App 用它判断是否需要重新下载：相同 → 读本地；不同 → 下载
        "content_hash": v.content_hash or from_version(v, v.audio),
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
        "has_audio": v.audio is not None,
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
            "title_original": a.title_original,
            "summary_original": a.summary_original,
            "body_original": a.body_original,
            "fetched_at": a.fetched_at.isoformat() if a.fetched_at else None,
            "timeline": (v.audio.timeline if v.audio else []) or [],
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


def export_day(db: Session, date: dt.date) -> str:
    """导出某天的完整列表。按 语言/等级 分组，App 可整日缓存。"""
    st = get_storage()
    base, rev = _cdn_base_with_rev(st, (settings.public_base_url or "").rstrip("/"))

    rows = db.execute(
        select(ArticleVersion, Article)
        .join(Article, Article.id == ArticleVersion.article_id)
        .options(selectinload(ArticleVersion.audio))
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
                "title_original": a.title_original,
                "published_date": a.published_date.isoformat(),
                "published_at": a.published_at.isoformat() if a.published_at else None,
            }
            articles[a.id] = art

        grouped.setdefault(v.lang, {}).setdefault(v.level_code, []).append(
            _version_summary(v, a, base, rev)
        )
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
    """
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
            .options(selectinload(ArticleVersion.audio))
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
    st.put(key, json.dumps(payload, ensure_ascii=False).encode("utf-8"), content_type="application/json; charset=utf-8")
    log.info("[export] 全量索引已更新，%d 天", len(date_list))
    return key


def _all_levels():
    from .levels import ALL_LEVELS

    return [(lv.code, lv.label, lv.lang, lv.level) for lv in sorted(ALL_LEVELS, key=lambda x: (x.lang, x.level))]


def export_all(db: Session) -> dict[str, Any]:
    """跑完流水线后统一导出。"""
    if not settings.export_json:
        log.info("[export] export_json=false，跳过")
        return {"exported": False}

    try:
        dates = db.execute(
            select(Article.published_date).distinct().order_by(Article.published_date.desc()).limit(3)
        ).scalars().all()
        day_keys = [export_day(db, d) for d in dates if d]
        idx = export_index(db)

        # 同步短路径入口 content/index.json —— App 拉的是这个路径，
        # 若只写 data/index.json 会导致短路径长期停留在旧版本。
        canonical = get_storage().read(idx)
        if canonical:
            get_storage().put("index.json", canonical,
                              content_type="application/json; charset=utf-8")

        return {"exported": True, "day_keys": day_keys, "index_key": idx}
    except Exception as exc:  # noqa: BLE001
        log.error("[export] 失败: %s", exc)
        return {"exported": False, "error": str(exc)}