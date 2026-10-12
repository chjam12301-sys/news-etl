import ast
import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import pipeline, fetcher, voice_warmup
from app.db import Base, Article, ArticleVersion, AudioAsset, JobRun
from app.levels import ALL_LEVELS


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def raw(title="Government announces landmark Mars mission", topic="science"):
    return fetcher.RawArticle("bbc_world", "https://example.test/" + title, topic, title,
                              "summary", "body " * 100, None,
                              dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc),
                              fetcher.fingerprint(title, title))


def add_article(db, day=None, title="Government announces landmark Mars mission"):
    article = pipeline._upsert_article(db, raw(title), day or pipeline.release_day())
    db.commit()
    return article


def add_versions(db, article, codes=None):
    for lv in ALL_LEVELS:
        if codes is not None and lv.code not in codes:
            continue
        db.add(ArticleVersion(article_id=article.id, level_code=lv.code, lang=lv.lang,
            level=lv.level, level_label=lv.label, title="Title", body="Hello world.",
            paragraphs=["Hello world."], title_zh="标题", paragraphs_zh=["你好。"],
            content_hash="a" * 16))
    db.commit()


def test_beijing_boundary_and_source_date(sessions):
    before = dt.datetime(2026, 10, 11, 15, 59, tzinfo=dt.timezone.utc)
    after = before + dt.timedelta(minutes=1)
    assert pipeline.release_day(before) == dt.date(2026, 10, 11)
    assert pipeline.release_day(after) == dt.date(2026, 10, 12)
    with sessions() as db:
        article = add_article(db, pipeline.release_day(after))
        assert article.published_date == dt.date(2026, 10, 12)
        assert article.published_at.date() == dt.date(2026, 10, 9)


def test_reserve_reuses_partial_and_force_scope(sessions):
    day = dt.date(2026, 10, 12)
    with sessions() as db:
        first, created = pipeline._reserve_target(db, day, [raw()])
        again, second_created = pipeline._reserve_target(db, day, [raw("Sports wins title")])
        assert created and not second_created
        assert first.id == again.id
        assert len(db.scalars(select(Article)).all()) == 1


def test_global_collect_ranks_all_topics_and_falls_back(monkeypatch):
    topics = ["tech", "business", "science", "health", "sports", "culture", "world"]
    candidates = [raw(f"Unique report {i}", t) for i, t in enumerate(topics)]
    monkeypatch.setattr(fetcher, "fetch_signals", lambda *a: {})
    monkeypatch.setattr(fetcher, "FEEDS_BY_TOPIC", {t: [i] for i, t in enumerate(topics)})
    monkeypatch.setattr(fetcher, "fetch_feed", lambda feed, client: [candidates[feed]])
    from app import selector
    monkeypatch.setattr(selector, "rank", lambda *a: [SimpleNamespace(raw=c) for c in reversed(candidates)])
    seen = []
    def body(r, client):
        seen.append(r.topic)
        return "short" if r.topic == "world" else "body " * 100
    monkeypatch.setattr(fetcher, "fetch_full_text", body)
    result = fetcher.collect(topics, 99, total_limit=1)
    assert [r.topic for r in result] == ["culture"]
    assert seen == ["world", "culture"]


@pytest.mark.asyncio
async def test_complete_versions_and_audio_not_regenerated(sessions, monkeypatch):
    with sessions() as db:
        article = add_article(db)
        article.image_provider = "unsplash"
        article.image_url = "https://example.test/image"
        add_versions(db, article)
        for version in db.scalars(select(ArticleVersion)):
            db.add(AudioAsset(version_id=version.id, lang=version.lang, voice="legacy",
                              file_path="old.mp3", duration_ms=1000, size_bytes=10))
        db.commit()
        rewrite = AsyncMock(side_effect=AssertionError("must not rewrite"))
        synth = AsyncMock(side_effect=AssertionError("must not synthesize"))
        monkeypatch.setattr(pipeline, "rewrite_all", rewrite)
        monkeypatch.setattr(pipeline, "synthesize", synth)
        monkeypatch.setattr(pipeline, "get_audio_storage", lambda: object())
        monkeypatch.setattr(pipeline.settings, "tts_enabled", True)
        from app import storage_guard
        monkeypatch.setattr(storage_guard, "check_capacity", lambda: None)
        stats = await pipeline._process_article(db, article, True, False)
        assert stats == {"versions": 0, "audios": 0, "audio_errors": 0}
        rewrite.assert_not_called()
        synth.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_ok", [True, False])
async def test_publication_result_controls_status_and_warmup(sessions, monkeypatch, publish_ok):
    with sessions() as db:
        article_id = add_article(db).id
    monkeypatch.setattr(pipeline, "SessionLocal", sessions)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(pipeline, "collect", Mock(side_effect=AssertionError("must reuse target")))
    process = AsyncMock(return_value={"versions": 0, "audios": 0, "audio_errors": 0})
    monkeypatch.setattr(pipeline, "_process_article", process)
    monkeypatch.setattr(pipeline, "get_storage", lambda: SimpleNamespace(provider="github", prune=lambda n: {"deleted": 0}))
    from app import exporter, publish
    monkeypatch.setattr(exporter, "export_all", lambda db: {"exported": True})
    monkeypatch.setattr(publish, "publish_all", lambda **kw: {"ok": publish_ok})
    warmup = AsyncMock(side_effect=RuntimeError("voice unavailable"))
    monkeypatch.setattr(voice_warmup, "warmup_article", warmup)
    result = await pipeline.run_daily(force=True)
    assert result["status"] == ("success" if publish_ok else "failed")
    assert result["stats"]["article_id"] == article_id
    assert warmup.await_count == int(publish_ok)
    with sessions() as db:
        assert db.scalar(select(JobRun)).status == result["status"]
        assert len(db.scalars(select(Article)).all()) == 1


@pytest.mark.asyncio
async def test_warmup_only_does_not_process_or_publish(sessions, monkeypatch):
    with sessions() as db:
        add_article(db)
    monkeypatch.setattr(pipeline, "SessionLocal", sessions)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(pipeline, "_process_article", AsyncMock(side_effect=AssertionError()))
    monkeypatch.setattr(pipeline, "get_storage", Mock(side_effect=AssertionError()))
    monkeypatch.setattr(voice_warmup, "warmup_article", AsyncMock(return_value={"accepted": 20}))
    result = await pipeline.run_daily(warmup_only=True)
    assert result["status"] == "success"
    assert result["stats"]["voice_warmup"]["accepted"] == 20


@pytest.mark.asyncio
async def test_reset_rejected():
    with pytest.raises(ValueError):
        await pipeline.run_daily(reset_all=True)


@pytest.mark.asyncio
async def test_warmup_twenty_requests_reuses_exact_contract(sessions, monkeypatch):
    monkeypatch.setattr(voice_warmup.settings, "voice_warmup_token", "test-only")
    calls = []
    def handle(request):
        assert request.headers["Authorization"] == "Bearer test-only"
        parts = request.url.path.split("/")
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"article_id": int(parts[3]), "level_code": parts[5],
                "content_version": int(parts[7]), "legacy_content_hash": "a" * 16, "text_identity": "b" * 64})
        return httpx.Response(202, json={"status": "pending", "job_id": "same-key"})
    with sessions() as db:
        article = add_article(db)
        add_versions(db, article)
        for _ in range(2):
            result = await voice_warmup.warmup_article(db, article.id, transport=httpx.MockTransport(handle))
            assert result["accepted"] == 20 and result["failed"] == 0
    assert len(calls) == 50
    assert all("/levels/en_" in str(req.url) for req in calls)
    assert not any("aria:ensure" in str(req.url) for req in calls)


def test_date_repair_exact_match_and_dry(sessions):
    # Load only the pure repair function; never execute the operational script's env loader.
    tree = ast.parse(Path("scripts/republish.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "repair_release_date")
    scope = {"dt": dt, "select": select, "Article": Article}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "repair", "exec"), scope)
    repair = scope["repair_release_date"]
    with sessions() as db:
        article = add_article(db, dt.date(2026, 10, 9))
        article.fetched_at = dt.datetime(2026, 10, 11, 22)
        db.commit()
        args = (db, dt.date(2026, 10, 12), dt.date(2026, 10, 12), [article.id])
        with pytest.raises(ValueError):
            repair(*args[:-1], [999], dry=False)
        db.rollback()
        changes = repair(*args, dry=True)
        assert changes[0]["from"] == "2026-10-09"
        assert article.published_date == dt.date(2026, 10, 9)
        repair(*args, dry=False)
        assert article.published_date == dt.date(2026, 10, 12)

@pytest.mark.asyncio
async def test_partial_version_recovery_only_rewrites_missing_levels(sessions, monkeypatch):
    from app.rewriter import offline_rewrite
    with sessions() as db:
        article = add_article(db)
        article.image_provider = "unsplash"
        article.image_url = "https://example.test/image"
        add_versions(db, article, {"en_a1"})
        original_id = db.scalar(select(ArticleVersion.id))
        received = []
        async def rewrite(topic, title, body, levels, llm):
            received.extend(lv.code for lv in levels)
            return [offline_rewrite(topic, title, body, lv) for lv in levels]
        monkeypatch.setattr(pipeline, "rewrite_all", rewrite)
        monkeypatch.setattr(pipeline, "get_llm", lambda: None)
        stats = await pipeline._process_article(db, article, False, False)
        db.commit()
        assert "en_a1" not in received
        assert len(received) == 9
        assert stats["versions"] == 9
        assert db.scalar(select(ArticleVersion.id).where(ArticleVersion.level_code == "en_a1")) == original_id


def test_concurrent_reservation_creates_one_article(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}", connect_args={"timeout": 10})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    def reserve(i):
        with sessions() as db:
            article, created = pipeline._reserve_target(db, dt.date(2026, 10, 12), [raw(f"Candidate {i}")])
            return article.id, created
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert results[0][0] == results[1][0]
    assert sum(created for _, created in results) == 1

@pytest.mark.asyncio
async def test_warmup_retries_new_cdn_and_reports_one_failed_asset(sessions, monkeypatch):
    monkeypatch.setattr(voice_warmup.settings, "voice_warmup_token", "test-only")
    monkeypatch.setattr(voice_warmup.asyncio, "sleep", AsyncMock())
    get_attempts = 0
    def handle(request):
        nonlocal get_attempts
        parts = request.url.path.split("/")
        if request.method == "GET":
            get_attempts += 1
            if get_attempts == 1:
                return httpx.Response(409, json={"error": "old CDN"})
            return httpx.Response(200, json={"article_id": int(parts[3]), "level_code": parts[5],
                "content_version": int(parts[7]), "legacy_content_hash": "a" * 16, "text_identity": "b" * 64})
        if parts[5] == "en_a1" and "guy:ensure" in request.url.path:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"status": "ready", "job_id": "existing"})
    with sessions() as db:
        article = add_article(db)
        add_versions(db, article)
        result = await voice_warmup.warmup_article(db, article.id, transport=httpx.MockTransport(handle))
    assert result["accepted"] == 19 and result["failed"] == 1
    assert get_attempts == 6
    assert result["items"][3]["voice"] == "guy"


@pytest.mark.asyncio
async def test_warmup_mismatched_manifest_never_submits(sessions, monkeypatch):
    monkeypatch.setattr(voice_warmup.settings, "voice_warmup_token", "test-only")
    def handle(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"article_id": 999})
    with sessions() as db:
        article = add_article(db)
        add_versions(db, article)
        result = await voice_warmup.warmup_article(db, article.id, transport=httpx.MockTransport(handle))
    assert result["accepted"] == 0 and result["failed"] == 20
