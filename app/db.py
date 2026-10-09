"""数据模型：Article / ArticleVersion / AudioAsset / JobRun。"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import settings

# 引擎与会话（Render 上把 DATABASE_URL 指向 Postgres 即可无缝切换）
_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """建表 + 补列。API 与 CLI 共用，保证任何入口都能直接跑。"""
    import sqlalchemy as sa

    Base.metadata.create_all(engine)

    # 轻量迁移：给已存在的库补上新增的列
    insp = sa.inspect(engine)
    wanted = {
        "articles": {
            "summary_original": "TEXT",
            "image_url": "TEXT",
            "image_credit": "TEXT",
            "image_provider": "VARCHAR(24)",
            "image_credit_url": "TEXT",
            "rewrite_status": "VARCHAR(24)",
            "tts_status": "VARCHAR(24)",
            "error": "TEXT",
            "published_date": "DATE",
        },
        "article_versions": {
            "reading_minutes": "FLOAT",
            "lead": "TEXT",
            "content_hash": "VARCHAR(32)",
            "title_zh": "VARCHAR(512)",
            "lead_zh": "TEXT",
            "paragraphs_zh": "JSON",
            "vocab": "JSON",
        },
        "audio_assets": {
            "boundaries": "JSON",
            "engine": "VARCHAR(32)",
            "public_url": "TEXT",
        },
    }
    with engine.begin() as conn:
        for table, cols in wanted.items():
            if table not in insp.get_table_names():
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, coltype in cols.items():
                if col not in existing:
                    conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Article(Base):
    """一篇源新闻。"""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(String(32), index=True)

    # 抓取到的原始内容
    title_original: Mapped[str] = mapped_column(String(512))
    summary_original: Mapped[str] = mapped_column(Text, default="")
    body_original: Mapped[str] = mapped_column(Text, default="")

    # 主题相关元数据
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 配图署名（如 "Photo by Annie Spratt on Unsplash"）
    image_credit: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 图源（unsplash / pexels / wikimedia / openverse）
    image_provider: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # 摄影师主页（Unsplash 场景下已带 utm，供 App 回链）
    image_credit_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    published_date: Mapped[dt.date] = mapped_column(Date, index=True, default=dt.date.today)

    # 去重指纹：source_url 归一化后的 sha1
    fingerprint: Mapped[str] = mapped_column(String(40), unique=True, index=True)

    # 处理状态
    rewrite_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    tts_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    versions: Mapped[list["ArticleVersion"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )


class ArticleVersion(Base):
    """一篇新闻 × 一个等级 的改写产物（5 英 + 5 日 = 10 条）。"""

    __tablename__ = "article_versions"
    __table_args__ = (
        UniqueConstraint("article_id", "level_code", name="uq_article_level"),
        Index("ix_version_feed", "lang", "level", "article_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )

    level_code: Mapped[str] = mapped_column(String(16), index=True)
    lang: Mapped[str] = mapped_column(String(4), index=True)
    level: Mapped[int] = mapped_column(Integer, index=True)
    level_label: Mapped[str] = mapped_column(String(32))

    title: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)             # 纯文本正文，段落以 \n\n 分隔
    paragraphs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    reading_minutes: Mapped[float] = mapped_column(Float, default=0.0)

    # 词汇表：[{"word","pos","zh","note"}]
    vocab: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # 1-3 句导读
    lead: Mapped[str] = mapped_column(Text, default="")

    # ---- 内容指纹（App 用它判断是否需要重新下载）----
    # 范围：正文 + 中文译文 + 词汇表 + 音频；不含配图与派生值。
    # 全局 commit SHA 变化不代表本篇变化，这个才是单篇粒度的判断依据。
    content_hash: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)

    # ---- 中文翻译（App 端展示用）----
    title_zh: Mapped[str | None] = mapped_column(String(512), nullable=True)
    lead_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 与 paragraphs 一一对应
    paragraphs_zh: Mapped[list[Any]] = mapped_column(JSON, default=list)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)

    article: Mapped[Article] = relationship(back_populates="versions")
    audio: Mapped["AudioAsset | None"] = relationship(
        back_populates="version", cascade="all, delete-orphan", uselist=False
    )


class AudioAsset(Base):
    """TTS 音频 + 逐词时间轴。"""

    __tablename__ = "audio_assets"
    __table_args__ = (
        UniqueConstraint("version_id", "voice", name="uq_version_voice"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("article_versions.id", ondelete="CASCADE"), index=True
    )
    lang: Mapped[str] = mapped_column(String(4), index=True)
    voice: Mapped[str] = mapped_column(String(64))

    file_path: Mapped[str] = mapped_column(Text)
    #对象存储上的对外 URL（R2 公开域名或签名 URL）；本地存储时为空
    public_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    # 逐词时间轴: [{"i":0,"text":"...","start":0.0,"end":0.35,"char_start":0,"char_end":5}]
    timeline: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # 引擎原始 word boundary（含 100ns 制 offset），便于换算与校准
    boundaries: Mapped[list[Any]] = mapped_column(JSON, default=list)

    engine: Mapped[str] = mapped_column(String(32), default="edge-tts")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)

    version: Mapped[ArticleVersion] = relationship(back_populates="audio")


class JobRun(Base):
    """任务执行记录，便于 App 侧或运维查看流水线状态。"""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)