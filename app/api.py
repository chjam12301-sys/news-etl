"""FastAPI 应用：对外接口 + 静态音频 + 每日任务入口。"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .db import Article, ArticleVersion, AudioAsset, JobRun, get_db, init_db
from .levels import ALL_LEVELS, EN_LEVELS, JA_LEVELS
from .schemas import (
    ArticleDetailOut,
    ArticleSummaryOut,
    AudioOut,
    FeedOut,
    HealthOut,
    JobOut,
    LevelOut,
    VocabWord,
    WordTimingOut,
)

log = logging.getLogger("news")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "每日英语听力 App 内容后台 API。"
        "每天抓取多主题新闻，经 AI 改写为 5 个英语等级（CEFR A1–C1）与 5 个日语等级"
        "（JLPT N5–N1），并生成带逐词时间轴的免费 TTS 音频。"
    ),
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    log.info("db ready: %s", settings.database_url)


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthOut, tags=["meta"])
def health(db: Session = Depends(get_db)) -> HealthOut:
    articles = db.scalar(select(func.count()).select_from(Article)) or 0
    versions = db.scalar(select(func.count()).select_from(ArticleVersion)) or 0
    audios = db.scalar(select(func.count()).select_from(AudioAsset)) or 0
    return HealthOut(
        status="ok",
        service=settings.app_name,
        version=app.version,
        llm_provider=settings.llm_provider,
        llm_enabled=bool(settings.gemini_api_key or settings.openrouter_api_key),
        tts_enabled=settings.tts_enabled,
        tts_engine="edge-tts",
        articles=articles,
        versions=versions,
        audios=audios,
        server_time=dt.datetime.now(dt.timezone.utc).isoformat(),
    )


@app.get("/api/v1/levels", response_model=list[LevelOut], tags=["meta"])
def list_levels(lang: Literal["en", "ja"] | None = Query(None, description="只返回该语言")) -> list[LevelOut]:
    specs = ALL_LEVELS if not lang else (EN_LEVELS if lang == "en" else JA_LEVELS)
    return [
        LevelOut(
            code=s.code, lang=s.lang, label=s.label, level=s.level,
            target_words=s.target_words, sentence_hint=s.sentence_hint,
        )
        for s in sorted(specs, key=lambda x: (x.lang, x.level))
    ]


@app.get("/api/v1/topics", response_model=list[str], tags=["meta"])
def list_topics() -> list[str]:
    from .fetcher import FEEDS_BY_TOPIC

    return sorted(FEEDS_BY_TOPIC.keys())


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
def _version_available_filters():
    return [
        ArticleVersion.lang == "en",
        ArticleVersion.level == 1,
    ]


@app.get("/api/v1/articles", response_model=FeedOut, tags=["content"])
def list_articles(
    db: Session = Depends(get_db),
    date: str | None = Query(None, description="按发布日过滤 YYYY-MM-DD"),
    topic: str | None = Query(None),
    source: str | None = Query(None),
    lang: Literal["en", "ja"] | None = Query(None),
    level: int | None = Query(None, ge=1, le=5),
    level_code: str | None = Query(None),
    q: str | None = Query(None, description="标题关键词"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> FeedOut:
    stmt = (
        select(ArticleVersion, Article)
        .join(Article, Article.id == ArticleVersion.article_id)
        .options(selectinload(ArticleVersion.audio))
    )
    if date:
        try:
            d = dt.date.fromisoformat(date)
        except ValueError as exc:
            raise HTTPException(400, "date 需为 YYYY-MM-DD") from exc
        stmt = stmt.where(Article.published_date == d)
    if topic:
        stmt = stmt.where(Article.topic == topic)
    if source:
        stmt = stmt.where(Article.source == source)
    if lang:
        stmt = stmt.where(ArticleVersion.lang == lang)
    if level:
        stmt = stmt.where(ArticleVersion.level == level)
    if level_code:
        stmt = stmt.where(ArticleVersion.level_code == level_code)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(ArticleVersion.title).like(like)
            | func.lower(Article.title_original).like(like)
        )

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(Article.published_date.desc(), Article.id.desc(), ArticleVersion.level)
        .limit(limit)
        .offset(offset)
    ).all()

    items = [
        ArticleSummaryOut.from_row(v, a, include_body=level is not None or level_code is not None or lang is not None)
        for v, a in rows
    ]
    return FeedOut(total=total, count=len(items), limit=limit, offset=offset, items=items)


@app.get("/api/v1/articles/today", response_model=FeedOut, tags=["content"])
def today(
    db: Session = Depends(get_db),
    lang: Literal["en", "ja"] = Query("en"),
    level: int = Query(1, ge=1, le=5),
    limit: int = Query(30, ge=1, le=100),
) -> FeedOut:
    """App 首页最常用的一次调用：今天 + 指定语言等级。"""
    stmt = (
        select(ArticleVersion, Article)
        .join(Article, Article.id == ArticleVersion.article_id)
        .options(selectinload(ArticleVersion.audio))
        .where(
            Article.published_date == dt.date.today(),
            ArticleVersion.lang == lang,
            ArticleVersion.level == level,
        )
        .order_by(ArticleVersion.id)
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    items = [ArticleSummaryOut.from_row(v, a) for v, a in rows]
    return FeedOut(total=len(items), count=len(items), limit=limit, offset=0, items=items)


# --------------------------------------------------------------------------- #
# 详情
# --------------------------------------------------------------------------- #
def _get_version(db: Session, version_id: int) -> tuple[ArticleVersion, Article]:
    row = db.execute(
        select(ArticleVersion, Article)
        .join(Article, Article.id == ArticleVersion.article_id)
        .options(selectinload(ArticleVersion.audio))
        .where(ArticleVersion.id == version_id)
    ).first()
    if not row:
        raise HTTPException(404, "version 不存在")
    return row[0], row[1]


@app.get("/api/v1/versions/{version_id}", response_model=ArticleDetailOut, tags=["content"])
def get_version(version_id: int, db: Session = Depends(get_db)) -> ArticleDetailOut:
    v, a = _get_version(db, version_id)
    return ArticleDetailOut.from_row(v, a, full=True)


@app.get("/api/v1/articles/{article_id}", response_model=ArticleDetailOut, tags=["content"])
def get_article(
    article_id: int,
    lang: Literal["en", "ja"] = Query("en"),
    level: int = Query(1, ge=1, le=5),
    db: Session = Depends(get_db),
) -> ArticleDetailOut:
    """按「文章 id + 语言 + 等级」取内容，App 端从列表跳转详情用这个。"""
    v = db.scalar(
        select(ArticleVersion)
        .options(selectinload(ArticleVersion.audio))
        .where(
            ArticleVersion.article_id == article_id,
            ArticleVersion.lang == lang,
            ArticleVersion.level == level,
        )
    )
    if not v:
        raise HTTPException(404, "该文章没有此语言等级的改写版本")
    a = db.get(Article, article_id)
    if not a:
        raise HTTPException(404, "文章不存在")
    return ArticleDetailOut.from_row(v, a, full=True)


@app.get("/api/v1/articles/{article_id}/levels", response_model=list[LevelOut], tags=["content"])
def article_levels(article_id: int, db: Session = Depends(get_db)) -> list[LevelOut]:
    rows = db.execute(
        select(ArticleVersion).where(ArticleVersion.article_id == article_id)
    ).scalars().all()
    if not rows:
        raise HTTPException(404, "文章不存在或尚未改写")
    return [
        LevelOut(
            code=v.level_code, lang=v.lang, label=v.level_label, level=v.level,
            target_words=v.word_count, sentence_hint="",
        )
        for v in sorted(rows, key=lambda x: (x.lang, x.level))
    ]


# --------------------------------------------------------------------------- #
# 音频 / 时间轴
# --------------------------------------------------------------------------- #
def _audio_out(v: ArticleVersion) -> AudioOut | None:
    a = v.audio
    if not a:
        return None
    return AudioOut(
        url=f"/api/v1/audio/{a.id}.mp3",
        duration=a.duration_ms / 1000,
        size_bytes=a.size_bytes,
        engine=a.engine,
        voice=a.voice,
        has_timeline=bool(a.timeline),
        word_count=len(a.timeline or []),
    )


@app.get("/api/v1/versions/{version_id}/audio", response_model=AudioOut, tags=["audio"])
def version_audio(version_id: int, db: Session = Depends(get_db)) -> AudioOut:
    v = db.scalar(
        select(ArticleVersion).options(selectinload(ArticleVersion.audio)).where(ArticleVersion.id == version_id)
    )
    if not v:
        raise HTTPException(404, "version 不存在")
    out = _audio_out(v)
    if not out:
        raise HTTPException(404, "该版本尚未生成音频")
    return out


@app.get("/api/v1/audio/{audio_id}.mp3", tags=["audio"])
def serve_audio(audio_id: int, db: Session = Depends(get_db), download: bool = False):
    """返回音频文件，支持 Range（播放器拖动进度条必需）。"""
    a = db.get(AudioAsset, audio_id)
    if not a:
        raise HTTPException(404, "音频不存在")
    path = Path(a.file_path)
    if not path.exists():
        raise HTTPException(410, "音频文件已丢失，请重新生成")
    media = "audio/mpeg" if path.suffix == ".mp3" else "audio/wav"
    return FileResponse(
        path, media_type=media,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=604800"},
        filename=path.name if download else None,
    )


@app.get("/api/v1/versions/{version_id}/timeline", tags=["audio"])
def version_timeline(
    version_id: int,
    format: Literal["compact", "full"] = Query("compact", description="compact=精简字段，full=含原始 100ns boundary"),
    db: Session = Depends(get_db),
):
    """逐词时间轴 —— App 跟读高亮的核心数据。"""
    v = db.scalar(
        select(ArticleVersion).options(selectinload(ArticleVersion.audio)).where(ArticleVersion.id == version_id)
    )
    if not v:
        raise HTTPException(404, "version 不存在")
    if not v.audio:
        raise HTTPException(404, "该版本尚未生成音频")
    a = v.audio
    words = [WordTimingOut(**t) for t in (a.timeline or [])]
    payload: dict[str, Any] = {
        "version_id": v.id,
        "level_code": v.level_code,
        "lang": v.lang,
        "duration": a.duration_ms / 1000,
        "text": v.body,
        "voice": a.voice,
        "engine": a.engine,
        "word_count": len(words),
        "words": [w.model_dump() for w in words],
    }
    if format == "full":
        payload["boundaries"] = a.boundaries or []
        payload["sentences"] = _sentence_segments(v.body, words)
    return JSONResponse(payload)


@app.get("/api/v1/versions/{version_id}/subtitle.srt", tags=["audio"])
def version_srt(version_id: int, db: Session = Depends(get_db)) -> Response:
    from .tts import WordTiming, to_srt

    v = db.scalar(
        select(ArticleVersion).options(selectinload(ArticleVersion.audio)).where(ArticleVersion.id == version_id)
    )
    if not v or not v.audio:
        raise HTTPException(404, "该版本尚未生成音频")
    timings = [WordTiming(**_to_kw(t)) for t in (v.audio.timeline or [])]
    return Response(to_srt(timings), media_type="application/x-subrip")


def _to_kw(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": d.get("i", 0), "text": d.get("w", ""),
        "start": d.get("s", 0.0), "end": d.get("e", 0.0),
        "start_ms": d.get("sm", 0), "end_ms": d.get("em", 0),
        "char_start": d.get("cs", 0), "char_end": d.get("ce", 0),
        "sentence_index": d.get("si", 0),
    }


def _sentence_segments(body: str, words: list[WordTimingOut]) -> list[dict[str, Any]]:
    """按标点把词聚成句，方便 App 做「整句朗读 / 段落高亮」。"""
    segs: list[list[WordTimingOut]] = []
    if not words:
        return []
    cur: list[WordTimingOut] = [words[0]]
    for w in words[1:]:
        cur.append(w)
        if w.w and w.w[-1] in ".!?。！？":
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    out = []
    for i, seg in enumerate(segs):
        out.append(
            {
                "i": i,
                "start": seg[0].s, "end": seg[-1].e,
                "start_ms": seg[0].sm, "end_ms": seg[-1].em,
                "text": "".join(w.w for w in seg),
                "char_start": seg[0].cs, "char_end": seg[-1].ce,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
@app.get("/api/v1/jobs", response_model=list[JobOut], tags=["jobs"])
def list_jobs(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)) -> list[JobOut]:
    rows = db.execute(select(JobRun).order_by(JobRun.id.desc()).limit(limit)).scalars().all()
    return [JobOut.from_row(r) for r in rows]


@app.post("/api/v1/jobs/daily", tags=["jobs"], summary="手动触发每日抓取+改写+TTS")
async def trigger_daily(
    topics: str | None = Query(None, description="逗号分隔，默认取配置全部"),
    per_topic: int = Query(1, ge=1, le=5),
    with_tts: bool = Query(True),
    force: bool = Query(False, description="True=忽略已有结果重新生成"),
):
    from .pipeline import run_daily

    return await run_daily(
        topics=[t.strip() for t in topics.split(",")] if topics else None,
        per_topic=per_topic,
        with_tts=with_tts,
        force=force,
    )


@app.exception_handler(Exception)
async def _unhandled(request, exc):  # noqa: ANN001
    log.exception("unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": f"internal error: {exc}"})