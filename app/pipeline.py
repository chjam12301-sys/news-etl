"""每日流水线：抓取 → AI 改写（5 英 + 5 日）→ TTS + 时间轴 → 落库。"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from pathlib import Path
from typing import Any

from sqlalchemy import delete as _delete, select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .db import Article, ArticleVersion, AudioAsset, JobRun, SessionLocal, init_db
from .fetcher import RawArticle, collect
from .levels import ALL_LEVELS
from .rewriter import get_llm, rewrite_all
from .audio_store import get_audio_storage, public_audio_url
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

    # force 时只重建「缺中文译文」的版本；已有完整译文的保留，
    # 否则会白扔 DeepSeek 额度且让旧内容的中译消失。
    if existing and force:
        keep = []
        for v in db.scalars(
            select(ArticleVersion).where(ArticleVersion.article_id == article.id)
        ):
            if v.title_zh and v.paragraphs_zh:
                keep.append(v.id)
            else:
                db.delete(v)
        db.flush()
        if keep:
            log.info("[pipe] article_id=%s 保留 %d 个已有完整译文的版本，只补其余",
                     article.id, len(keep))

    # 配图：只用 Openverse CC0（方案 B）。
    # 不使用新闻原图 —— 非 CC0 有版权风险，且热链对方 CDN 易失效。
    if force or article.image_url is None:
        from .images import find_image

        try:
            img = await find_image(article.topic, article.title_original)
            if img:
                article.image_url = img.url
                article.image_credit = img.attribution
                log.info("[img] article_id=%s → CC0 %s", article.id, img.url[:70])
            else:
                # 显式清空原图（避免沿用旧的媒体图），由 _image_meta 走渐变占位
                article.image_url = ""
                article.image_credit = ""
                log.info("[img] article_id=%s 未找到 CC0 图，用渐变占位块", article.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[img] 配图失败: %s", exc)
            if not getattr(settings, "image_allow_source_fallback", False):
                article.image_url = ""
                article.image_credit = ""
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
            title_zh=r.title_zh,
            lead_zh=r.lead_zh,
            paragraphs_zh=r.paragraphs_zh,
        )
        db.add(version)
        db.flush()
        stats["versions"] += 1

    article.rewrite_status = "done"
    article.error = None
    db.flush()

    if with_tts and settings.tts_enabled:
        # 音频走对象存储，**不进 Git**（jsDelivr 有 50MB 仓库上限）
        storage = get_audio_storage()
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
            # 保留的版本仍需补音频（它们可能只是译文完整但没配音）
            if version.audio is not None:
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
                        # 以配置基址为准，保证与导出时的拼法完全一致
                        public_url=public_audio_url(file_path=key) or obj.url or "",
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

    # 内容指纹：必须等音频生成完再算，否则 hash 会与实际音频不符
    try:
        from .content_hash import from_version

        for version in db.scalars(
            select(ArticleVersion)
            .options(selectinload(ArticleVersion.audio))
            .where(ArticleVersion.article_id == article.id)
        ):
            old_hash = version.content_hash
            version.content_hash = from_version(version, version.audio)
            if version.content_hash != old_hash:
                log.debug("[hash] article_id=%s %s → %s",
                          article.id, version.level_code, version.content_hash)
    except Exception as exc:  # noqa: BLE001
        log.warning("[pipe] 计算 content_hash 失败: %s", exc)

    db.flush()
    return stats


async def run_daily(
    topics: list[str] | None = None,
    per_topic: int | None = None,
    with_tts: bool = True,
    force: bool = False,
    reset_all: bool = False,
) -> dict[str, Any]:
    """执行一次完整流水线。

    reset_all=True 时先清空 articles 表（连带versions/audio），
    用于「RSS 去重导致抓不到新文章」时重新开始。
    """
    if reset_all:
        init_db()
        d = SessionLocal()
        try:
            n_v = d.execute(_delete(ArticleVersion)).rowcount
            n_a = d.execute(_delete(Article)).rowcount
            d.commit()
            log.info("[pipe] 已清空 %d 篇文章 / %d 个版本", n_a, n_v)
        finally:
            d.close()
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

    # 发布：① 提交内容 → ② 用该 commit 生成索引 → ③ 校验 → ④ 更新指针
    # 顺序不能颠倒：索引里的 detail_url / audio.url 必须在内容提交后才生成，
    # 否则会指向不含这些文件的旧 commit（首页能显示卡片、点进详情却 404）。
    if getattr(get_storage(), "provider", "") == "github":
        try:
            from .publish import publish_all

            pub = publish_all(
                db=SessionLocal(),
                storage=get_storage(),
                topics_days=3,
                commit_msg=(
                    f"content: {totals.get('versions', 0)} 版本 / "
                    f"{totals.get('audios', 0)} 音频"
                    f"（{job.finished_at:%Y-%m-%d %H:%M}）"
                ),
            )
            totals["publish_ok"] = pub.get("ok")
            totals["publish_verify"] = pub.get("verify")
            totals["index_rev"] = pub.get("index_rev", "")
            if not pub.get("ok"):
                log.error("[pipe] 发布未完成：%s", pub.get("verify"))
            else:
                st = get_storage()
                keep = getattr(settings, "github_keep_days", 14)
                totals["git_pruned"] = st.prune(keep)["deleted"]
        except Exception as exc:  # noqa: BLE001
            log.error("[pipe] 发布失败: %s", exc)
            totals["publish_error"] = str(exc)[:200]

    log.info("[pipe] 结束 %s", totals)
    return {
        "status": job.status,
        "stats": totals,
        "error": job.error,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }