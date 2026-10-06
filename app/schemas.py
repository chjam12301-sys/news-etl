"""Pydantic 出参模型。"""
from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class VocabWord(BaseModel):
    word: str
    pos: str = ""
    zh: str = ""
    note: str = ""


class WordTimingOut(BaseModel):
    i: int
    w: str
    s: float
    e: float
    sm: int
    em: int
    cs: int
    ce: int
    si: int = 0


class AudioOut(BaseModel):
    url: str
    duration: float
    size_bytes: int = 0
    engine: str = "edge-tts"
    voice: str = ""
    has_timeline: bool = False
    word_count: int = 0


class LevelOut(BaseModel):
    code: str
    lang: str
    label: str
    level: int
    target_words: int = 0
    sentence_hint: str = ""


class ArticleSummaryOut(BaseModel):
    """列表项 —— 默认不含正文，避免 payload 过大。"""

    version_id: int
    article_id: int
    level_code: str
    lang: str
    level: int
    level_label: str

    title: str
    topic: str
    source: str
    source_url: str
    image_url: str | None = None

    published_date: dt.date
    published_at: dt.datetime | None = None

    word_count: int = 0
    reading_minutes: float = 0.0
    lead: str = ""
    preview: str = ""
    body: str | None = None
    vocab: list[VocabWord] = Field(default_factory=list)
    audio: AudioOut | None = None

    @classmethod
    def from_row(cls, v, a, include_body: bool = False) -> "ArticleSummaryOut":
        audio = None
        if v.audio is not None:
            audio = AudioOut(
                url=f"/api/v1/audio/{v.audio.id}.mp3",
                duration=v.audio.duration_ms / 1000,
                size_bytes=v.audio.size_bytes,
                engine=v.audio.engine,
                voice=v.audio.voice,
                has_timeline=bool(v.audio.timeline),
                word_count=len(v.audio.timeline or []),
            )
        preview = (v.paragraphs[0] if v.paragraphs else "")[:140]
        return cls(
            version_id=v.id,
            article_id=v.article_id,
            level_code=v.level_code,
            lang=v.lang,
            level=v.level,
            level_label=v.level_label,
            title=v.title,
            topic=a.topic,
            source=a.source,
            source_url=a.source_url,
            image_url=a.image_url,
            published_date=a.published_date,
            published_at=a.published_at,
            word_count=v.word_count,
            reading_minutes=v.reading_minutes,
            lead=v.lead,
            preview=preview,
            body=v.body if include_body else None,
            vocab=[VocabWord(**x) for x in (v.vocab or [])],
            audio=audio,
        )


class ArticleDetailOut(ArticleSummaryOut):
    paragraphs: list[str] = Field(default_factory=list)
    title_original: str = ""
    summary_original: str = ""
    body_original: str = ""
    fetched_at: dt.datetime | None = None

    @classmethod
    def from_row(cls, v, a, full: bool = True) -> "ArticleDetailOut":
        base = ArticleSummaryOut.from_row(v, a, include_body=full)
        return cls(
            **base.model_dump(),
            paragraphs=v.paragraphs or [],
            title_original=a.title_original,
            summary_original=a.summary_original,
            body_original=a.body_original if full else "",
            fetched_at=a.fetched_at,
        )


class FeedOut(BaseModel):
    total: int
    count: int
    limit: int
    offset: int
    items: list[ArticleSummaryOut]


class JobOut(BaseModel):
    id: int
    job: str
    status: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @classmethod
    def from_row(cls, r) -> "JobOut":
        return cls(
            id=r.id, job=r.job, status=r.status, started_at=r.started_at,
            finished_at=r.finished_at, stats=r.stats or {}, error=r.error,
        )


class HealthOut(BaseModel):
    status: str
    service: str
    version: str
    llm_provider: str
    llm_enabled: bool
    tts_enabled: bool
    tts_engine: str
    storage: str = "local"
    export_json: bool = False
    articles: int
    versions: int
    audios: int
    server_time: str