"""每日流水线：抓取 → AI 改写（5 英 + 5 日）→ TTS + 时间轴 → 落库。"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .db import Article, ArticleVersion, AudioAsset, JobRun, SessionLocal, init_db
from .fetcher import RawArticle, collect
from .levels import ALL_LEVELS
from .rewriter import get_llm, rewrite_all
from .audio_store import get_audio_storage, public_audio_url, is_playable_audio
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


def _recent_titles(db: Session, days: int) -> list[str]:
    """库里近期已用过的标题 —— 交给抓取阶段做跨天去重。

    只看 fingerprint 是不够的：同一条新闻隔天被源改了标题就是新指纹，
    于是北极冻土写了 4 篇、Nvidia 3 篇。这里按标题实词再挡一层。
    """
    if days <= 0:
        return []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    rows = db.scalars(
        select(Article.title_original).where(Article.fetched_at >= cutoff)
    ).all()
    return [t for t in rows if t]


def _upsert_article(db: Session, raw: RawArticle, release_date: dt.date | None = None) -> Article | None:
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
        published_date=release_date or release_day(),
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

    versions = list(db.scalars(select(ArticleVersion).where(ArticleVersion.article_id == article.id)))
    if force:
        for version in versions:
            if not version.title_zh or not version.paragraphs_zh:
                db.delete(version)
        db.flush()
        versions = list(db.scalars(select(ArticleVersion).where(ArticleVersion.article_id == article.id)))
    present = {version.level_code for version in versions}
    missing = [level for level in ALL_LEVELS if level.code not in present]

    # 配图：多级图源降级（unsplash → pexels → wikimedia → openverse）。
    # 抓取阶段已经把新闻原图写进 image_url 了，所以判断条件不能只看「有没有值」——
    # 必须看 provider 是不是合规图源，否则媒体原图永远换不掉。
    from .images import PROVIDER_NAMES, find_image

    prov = (article.image_provider or "").strip().lower()
    needs_image = force or not (article.image_url or "").strip() or prov not in PROVIDER_NAMES
    if needs_image:

        def _clear_image() -> None:
            article.image_url = ""
            article.image_credit = ""
            article.image_provider = ""
            article.image_credit_url = ""
            article.image_source_url = ""
            article.image_author = ""

        try:
            img = await find_image(article.topic, article.title_original)
            if img:
                article.image_url = img.url
                article.image_credit = img.attribution
                article.image_provider = img.provider
                article.image_credit_url = img.creator_url
                article.image_source_url = img.source_url
                article.image_author = img.creator
                log.info("[img] article_id=%s → %s %s", article.id, img.provider, img.url[:70])
            else:
                # 由 _image_meta 走渐变占位
                _clear_image()
                log.info("[img] article_id=%s 全部图源未命中，用渐变占位块", article.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[img] 配图失败: %s", exc)
            if not getattr(settings, "image_allow_source_fallback", False):
                _clear_image()
        db.flush()

    llm = get_llm() if missing else None
    log.info("[pipe] 改写中 article_id=%s topic=%s llm=%s",
             article.id, article.topic, llm.name if llm else "offline")

    t0 = time.time()
    results = await rewrite_all(
        article.topic, article.title_original, article.body_original, missing, llm
    ) if missing else []
    log.info("[pipe] 改写完成 %d 个等级，用时 %.1fs", len(results), time.time() - t0)

    for r in results:
        body = r.body
        # 词条原样入库；音标 / 英文释义 / 词性由 App 在运行时直读 R2 词典回填，
        # 生成端不再烘焙（见 app/dict.py 头部说明）。
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
                if not is_playable_audio(version.audio):
                    stats["audio_errors"] += 1
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


def release_day(now: dt.datetime | None = None) -> dt.date:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo("Asia/Shanghai")).date()


def daily_target(db: Session, day: dt.date) -> Article | None:
    return db.scalar(select(Article).where(Article.published_date == day)
                     .order_by(Article.fetched_at, Article.id).limit(1))


def _reserve_target(db: Session, day: dt.date, raws: list[RawArticle]) -> tuple[Article | None, bool]:
    # A database lock also covers API/manual invocations outside Actions concurrency.
    db.rollback()
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:namespace, :day)"),
                   {"namespace": 730105, "day": day.toordinal()})
    elif db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    article = daily_target(db, day)
    created = False
    if article is None:
        for raw in raws:
            article = _upsert_article(db, raw, day)
            if article is not None:
                created = True
                break
    db.commit()
    return article, created


async def run_daily(
    topics: list[str] | None = None,
    per_topic: int | None = None,
    with_tts: bool = True,
    force: bool = False,
    reset_all: bool = False,
    warmup_only: bool = False,
) -> dict[str, Any]:
    if reset_all:
        raise ValueError("daily does not allow reset; historical content must be preserved")
    started = dt.datetime.now(dt.timezone.utc)
    day = release_day(started)
    init_db()
    totals: dict[str, Any] = {"fetched": 0, "created": 0, "versions": 0, "audios": 0,
                              "audio_errors": 0, "skipped": 0, "release_date": day.isoformat()}
    errors: list[str] = []
    with SessionLocal() as db:
        job = JobRun(job="daily", status="running", started_at=started)
        db.add(job)
        db.commit()
        try:
            article = daily_target(db, day)
            if warmup_only:
                if article is None:
                    raise ValueError("no existing article for today's warmup")
            else:
                raws = []
                if article is None:
                    recent = _recent_titles(db, getattr(settings, "dedup_lookback_days", 60))
                    db.rollback()
                    raws = await asyncio.to_thread(collect, topics, per_topic, recent, total_limit=1)
                    totals["fetched"] = len(raws)
                article, created = _reserve_target(db, day, raws)
                totals["created"] = int(created)
                if article is None:
                    raise ValueError("no usable article for today's release")
            totals["article_id"] = article.id
            if not warmup_only:
                # Serialize recovery of the same reserved article, then re-read completed levels.
                article = db.scalar(select(Article).where(Article.id == article.id)
                                    .with_for_update().execution_options(populate_existing=True))
                stats = await _process_article(db, article, with_tts, force)
                totals.update(stats)
                db.commit()
                if totals["audio_errors"]:
                    raise RuntimeError("default audio incomplete; rerun resumes missing assets")
                from .exporter import export_all
                export_result = export_all(db)
                totals["export"] = "ok" if export_result.get("exported") else "skipped"
                storage = get_storage()
                if getattr(storage, "provider", "") == "github":
                    from .publish import publish_all
                    pub = publish_all(db=db, storage=storage, topics_days=3,
                                      commit_msg=f"content: daily {day} article {article.id}")
                    totals.update(publish_ok=bool(pub.get("ok")),
                                  publish_verify=pub.get("verify"), index_rev=pub.get("index_rev", ""))
                    if not pub.get("ok"):
                        raise RuntimeError("four-stage publication failed")
                    try:
                        totals["git_pruned"] = storage.prune(settings.github_keep_days)["deleted"]
                    except Exception:
                        totals["prune"] = "failed"
            if warmup_only or totals.get("publish_ok"):
                from .voice_warmup import warmup_article
                try:
                    totals["voice_warmup"] = await warmup_article(db, article.id)
                except Exception as exc:
                    totals["voice_warmup"] = {"status": "failed", "error": type(exc).__name__}
            job.status = "success"
        except Exception as exc:
            db.rollback()
            log.exception("[pipe] daily failed")
            errors.append(str(exc)[:300])
            job.status = "failed"
        job.stats = totals
        job.error = "; ".join(errors) if errors else None
        job.finished_at = dt.datetime.now(dt.timezone.utc)
        db.add(job)
        db.commit()
        return {"status": job.status, "stats": totals, "error": job.error,
                "finished_at": job.finished_at.isoformat()}
