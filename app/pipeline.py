"""每日流水线：抓取 → AI 改写（5 英 + 5 日）→ TTS + 时间轴 → 落库。"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .db import Article, ArticleVersion, AudioAsset, JobRun, SessionLocal, init_db
from .fetcher import RawArticle, collect
from .levels import ALL_LEVELS
from .rewriter import get_llm, rewrite_all
from .storage import audio_key, get_storage
from .tts import synthesize

log = logging.getLogger("pipeline")

# 英文阅读速度 wpm / 日文 mora 换算
WPM_EN = 200.0
WPM_JA = 350.0


def _reading_minutes(text: str, lang: str) -> float:
    if lang == "ja":
        units = len([c for c in text if not c.isspace()])
    else:
        units = len(text.split())
    rate = WPM_JA if lang == "ja" else WPM_EN
    return round(units / rate, 1)


def _count_words(text: str, lang: str) -> int:
    if lang == "ja":
        import re

        return len(re.findall(r"[぀-ヿ一-鿿]|[A-Za-z0-9]+", text))
    return len(text.split())


def _upsert_article(db: Session, raw: RawArticle) -> Article | None:
    existing = db.scalar(select(Article).where(Article.fingerprint == raw.fingerprint))
    if existing:
        return None
    published = raw.published_at or dt.datetime.now(dt.timezone.utc)
    article = Article(
        source=raw.source,
        source_url=raw.source_url,
        topic=raw.topic,
        title_original=raw.title,
        summary_original=raw.summary,
        body_original=raw.body or raw.summary,
        image_url=raw.image_url,
        published_at=published,
        published_date=published.date(),
        fingerprint=raw.fingerprint,
        rewrite_status="pending",
        tts_status="pending",
    )
    db.add(article)
    db.flush()
    return article


async def _process_article(
    db: Session, article: Article, with_tts: bool, force: bool
) -> dict[str, Any]:
    stats: dict[str, Any] = {"versions": 0, "audios": 0, "audio_errors": 0}

    existing = db.scalar(
        select(ArticleVersion).where(ArticleVersion.article_id == article.id)
    )
    if existing and not force:
        log.info("[pipe] 已存在改写版本，跳过 article_id=%s", article.id)
        return stats
    if existing and force:
        for v in db.scalars(select(ArticleVersion).where(ArticleVersion.article_id == article.id)):
            db.delete(v)
        db.flush()

    llm = get_llm()
    log.info("[pipe] 改写中 article_id=%s topic=%s llm=%s",
             article.id, article.topic, llm.name if llm else "offline")

    t0 = time.time()
    results = await rewrite_all(
        article.topic, article.title_original, article.body_original, list(ALL_LEVELS), llm
    )
    log.info("[pipe] 改写完成 %d 个等级，用时 %.1fs", len(results), time.time() - t0)

    for r in results:
        body = r.body
        version = ArticleVersion(
            article_id=article.id,
            level_code=r.level_code,
            lang=r.lang,
            level=r.level,
            level_label=r.level_label,
            title=r.title,
            body=body,
            paragraphs=r.paragraphs,
            word_count=_count_words(body, r.lang),
            reading_minutes=_reading_minutes(body, r.lang),
            vocab=r.vocab,
            lead=r.lead,
        )
        db.add(version)
        db.flush()
        stats["versions"] += 1

    article.rewrite_status = "done"
    article.error = None
    db.flush()

    if with_tts and settings.tts_enabled:
        storage = get_storage()
        # 容量护栏：超上限直接跳过配音，避免写入 R2 时产生费用
        try:
            from .storage_guard import check_capacity

            check_capacity()
        except Exception as exc:  # noqa: BLE001
            log.warning("[pipe] %s", exc)
            stats["audio_errors"] = 0
        for version in db.scalars(
            select(ArticleVersion)
            .options(selectinload(ArticleVersion.audio))
            .where(ArticleVersion.article_id == article.id)
        ):
            if version.audio is not None and not force:
                continue
            try:
                res = await synthesize(version.body, version.lang)
                is_mp3 = res.audio_bytes[:3] == b"ID3" or res.engine == "edge-tts"
                ext = "mp3" if is_mp3 else "wav"
                key = audio_key(version.article_id, version.level_code, ext)
                obj = storage.put(
                    key,
                    res.audio_bytes,
                    content_type="audio/mpeg" if is_mp3 else "audio/wav",
                )
                if version.audio is not None:
                    db.delete(version.audio)
                    db.flush()
                db.add(
                    AudioAsset(
                        version_id=version.id,
                        lang=version.lang,
                        voice=res.voice,
                        # 库里存对象 key，不再是本地绝对路径
                        file_path=key,
                        public_url=obj.url or "",
                        duration_ms=int(res.duration * 1000),
                        size_bytes=len(res.audio_bytes),
                        timeline=res.timeline_dict(),
                        boundaries=res.boundaries_dict(),
                        engine=res.engine,
                    )
                )
                db.flush()
                stats["audios"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("[pipe] TTS 失败 %s: %s", version.level_code, exc)
                stats["audio_errors"] += 1

        article.tts_status = "done" if stats["audio_errors"] == 0 else "partial"
    else:
        article.tts_status = "skipped"

    db.flush()
    return stats


async def run_daily(
    topics: list[str] | None = None,
    per_topic: int | None = None,
    with_tts: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """执行一次完整流水线，返回统计结果。"""
    started = dt.datetime.now(dt.timezone.utc)
    init_db()
    job = JobRun(job="daily", status="running", started_at=started)
    db = SessionLocal()
    db.add(job)
    db.commit()

    totals = {"fetched": 0, "created": 0, "versions": 0, "audios": 0, "audio_errors": 0, "skipped": 0}
    errors: list[str] = []

    try:
        log.info("[pipe] 开始每日任务 topics=%s per_topic=%s", topics or settings.topics, per_topic or settings.articles_per_topic)
        raws = await asyncio.to_thread(collect, topics, per_topic)
        totals["fetched"] = len(raws)
        log.info("[pipe] 抓取到 %d 篇", len(raws))

        for raw in raws:
            db.expire_all()
            try:
                article = _upsert_article(db, raw)
                if article is None:
                    totals["skipped"] += 1
                    db.commit()
                    continue
                totals["created"] += 1
                db.commit()
                stats = await _process_article(db, article, with_tts, force)
                for k in ("versions", "audios", "audio_errors"):
                    totals[k] += stats.get(k, 0)
                db.commit()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{raw.source_url}: {exc}")
                log.exception("[pipe] 单篇失败 %s", raw.source_url)
                db.rollback()

        # 导出静态 JSON（CDN 直读用）。失败不影响主流程。
        try:
            from .exporter import export_all

            export_result = export_all(db)
            totals["export"] = "ok" if export_result.get("exported") else "skipped"
        except Exception as exc:  # noqa: BLE001
            log.warning("[pipe] JSON 导出失败: %s", exc)
            totals["export"] = "failed"

        job.status = "failed" if errors and totals["versions"] == 0 else "success"
        job.stats = totals
        job.error = "; ".join(errors[:10]) if errors else None
    except Exception as exc:  # noqa: BLE001
        log.exception("[pipe] 任务异常")
        job.status = "failed"
        job.stats = totals
        job.error = str(exc)
    finally:
        job.finished_at = dt.datetime.now(dt.timezone.utc)
        db.add(job)
        db.commit()
        db.close()

    log.info("[pipe] 结束 %s", totals)
    return {
        "status": job.status,
        "stats": totals,
        "error": job.error,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }