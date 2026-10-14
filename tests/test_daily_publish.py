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

from app import pipeline, fetcher, alt_voices
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


def add_versions(db, article, codes=None, with_audio=False):
    for lv in ALL_LEVELS:
        if codes is not None and lv.code not in codes:
            continue
        v = ArticleVersion(article_id=article.id, level_code=lv.code, lang=lv.lang,
            level=lv.level, level_label=lv.label, title="Title", body="Hello world.",
            paragraphs=["Hello world."], title_zh="标题", paragraphs_zh=["你好。"],
            content_hash="a" * 16)
        db.add(v)
        db.flush()
        if with_audio:
            db.add(AudioAsset(version_id=v.id, lang=lv.lang, voice="af_heart",
                              file_path=f"audio/{article.id}/{lv.code}.mp3",
                              duration_ms=1000, size_bytes=1000,
                              timeline=[{"i": 0}], engine="kokoro"))
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
    monkeypatch.setattr(alt_voices, "warm_alt_voices", warmup)
    result = await pipeline.run_daily(force=True)
    assert result["status"] == ("success" if publish_ok else "failed")
    assert result["stats"]["article_id"] == article_id
    # 可选音色排在**发布之后**：发布失败就不跑（不浪费），发布成功才跑且抛错被吞掉
    assert warmup.await_count == int(publish_ok)
    if publish_ok:
        assert result["stats"]["alt_voices"]["status"] == "failed"
    else:
        assert "alt_voices" not in result["stats"]
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
    monkeypatch.setattr(alt_voices, "warm_alt_voices", AsyncMock(return_value={"generated": 20}))
    result = await pipeline.run_daily(warmup_only=True)
    assert result["status"] == "success"
    assert result["stats"]["alt_voices"]["generated"] == 20


@pytest.mark.asyncio
async def test_reset_rejected():
    with pytest.raises(ValueError):
        await pipeline.run_daily(reset_all=True)


@pytest.mark.asyncio
async def test_alt_voices_generates_skips_retries_and_refreshes_hash(sessions, monkeypatch):
    """缺失的生成、已存在的跳过、单份失败重试 3 次、content_hash 重算，
    以及 MP3 编码器不可用时按字节嗅探写成 .wav（不冒充 mp3）。"""
    from app import alt_voices as av
    from app.db import AltVoiceAsset

    monkeypatch.setattr(av, "alt_voices", lambda: ("af_bella", "am_michael"))
    monkeypatch.setattr(av.settings, "tts_enabled", True)
    monkeypatch.setattr(av, "_engine", lambda: "kokoro")
    calls = []

    async def fake_synth(text, lang, voice=None):
        calls.append(voice)
        if voice == "am_michael":
            raise RuntimeError("boom")
        # 模拟 MP3 编码器不可用 → 回退 WAV（RIFF 头）
        return SimpleNamespace(audio_bytes=b"RIFF\x00\x00\x00\x00WAVEfmt ", duration=1.5,
                               engine="kokoro", timeline_dict=lambda: [{"i": 0}])

    monkeypatch.setattr(av, "synthesize", fake_synth)
    monkeypatch.setattr(av, "_sleep", AsyncMock())
    put = Mock()
    storage = SimpleNamespace(put=put, delete=Mock())
    with sessions() as db:
        article = add_article(db)
        add_versions(db, article, codes={"en_a1"}, with_audio=True)
        before = {v.level_code: v.content_hash for v in db.scalars(select(ArticleVersion))}

        stats = await av.warm_alt_voices(db, article, storage=storage)
        assert stats["generated"] == 1 and stats["failed"] == 1
        assert sum(1 for v in calls if v == "am_michael") == 3
        call = put.call_args_list[0]
        assert call.args[0].endswith("_af_bella.wav")
        assert call.kwargs["content_type"] == "audio/wav"

        calls.clear()
        stats2 = await av.warm_alt_voices(db, article, storage=storage)
        assert stats2["skipped"] == 1 and stats2["failed"] == 1
        assert "af_bella" not in calls

        # 可选音色变了 → content_hash 必须跟着变，否则 App 认为内容没更新、永远拉不到
        v = db.scalar(select(ArticleVersion).where(ArticleVersion.level_code == "en_a1"))
        assert v.content_hash != before["en_a1"]
        assert db.scalar(select(AltVoiceAsset).where(AltVoiceAsset.voice == "af_bella"))


@pytest.mark.asyncio
async def test_alt_voices_skip_guards_and_prune_stale(sessions, monkeypatch):
    """跳过条件（无默认音频 / with_tts=False / 非 kokoro 引擎）
    与 ALT_VOICES 变更后的下架清理（DB 行 + 对象）。"""
    from app import alt_voices as av
    from app.db import AltVoiceAsset

    monkeypatch.setattr(av.settings, "tts_enabled", True)
    monkeypatch.setattr(av, "alt_voices", lambda: ("af_bella",))
    delete = Mock()
    storage = SimpleNamespace(put=Mock(), delete=delete)

    async def fake_synth(text, lang, voice=None):
        return SimpleNamespace(audio_bytes=b"ID3xxxx", duration=1.0,
                               engine="kokoro", timeline_dict=lambda: [])

    monkeypatch.setattr(av, "synthesize", fake_synth)
    monkeypatch.setattr(av, "_sleep", AsyncMock())

    monkeypatch.setattr(av, "_engine", lambda: "kokoro")
    with sessions() as db:
        article = add_article(db)
        add_versions(db, article, codes={"en_a1"}, with_audio=False)
        s = await av.warm_alt_voices(db, article, storage=storage)
        assert s["generated"] == 0 and any(
            i.get("reason") == "no_default_audio" for i in s["items"]
        )
        s = await av.warm_alt_voices(db, article, storage=storage, with_tts=False)
        assert s["reason"] == "tts_disabled"
        monkeypatch.setattr(av, "_engine", lambda: "edge-tts")
        s = await av.warm_alt_voices(db, article, storage=storage)
        assert s["reason"] == "engine_mismatch"

    monkeypatch.setattr(av, "_engine", lambda: "kokoro")
    with sessions() as db:
        article = add_article(db, title="Prune stale voices story")
        add_versions(db, article, codes={"en_a1"}, with_audio=True)
        await av.warm_alt_voices(db, article, storage=storage)
        assert db.scalar(select(AltVoiceAsset).where(AltVoiceAsset.voice == "af_bella"))
        # 清单改名 → 旧音色下架：DB 行与对象都清掉
        monkeypatch.setattr(av, "alt_voices", lambda: ("bf_emma",))
        s = await av.warm_alt_voices(db, article, storage=storage)
        assert s["removed"] == 1 and s["generated"] == 1
        assert db.scalar(select(AltVoiceAsset).where(AltVoiceAsset.voice == "af_bella")) is None
        assert delete.called


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

def test_date_move_has_no_duplicates_in_reachable_daily_indexes(sessions, monkeypatch, tmp_path):
    import json
    from app import exporter, publish
    from app.storage import LocalStorage, day_index_key, index_key

    storage = LocalStorage(tmp_path / "content")
    storage.repo_dir = str(tmp_path)
    storage.commit = lambda message: None
    monkeypatch.setattr(exporter, "get_storage", lambda: storage)
    monkeypatch.setattr(exporter.settings, "public_base_url", "")
    with sessions() as db:
        moved = add_article(db, dt.date(2026, 10, 9), "Moved story")
        add_versions(db, moved, {"en_a1"})
        moved_version = db.scalar(select(ArticleVersion.id).where(ArticleVersion.article_id == moved.id))
        exporter.export_day(db, dt.date(2026, 10, 9))
        for day in (10, 11):
            article = add_article(db, dt.date(2026, 10, day), f"Other story {day}")
            add_versions(db, article, {"en_a1"})
        moved.published_date = dt.date(2026, 10, 12)
        db.commit()
        result = publish.publish_all(db=db, storage=storage, topics_days=3)
        assert result["ok"]
    root = json.loads(storage.read(index_key()))
    assert root["dates"] == ["2026-10-12", "2026-10-11", "2026-10-10"]
    ids = []
    for date in root["dates"]:
        daily = json.loads(storage.read(day_index_key(date)))
        ids.extend(item["version_id"] for levels in daily["versions"].values()
                   for items in levels.values() for item in items)
    assert len(ids) == len(set(ids))
    assert ids.count(moved_version) == 1
    assert storage.exists(day_index_key("2026-10-09"))


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_silent", [False, True])
async def test_audio_failure_stops_daily_before_export(sessions, monkeypatch, legacy_silent):
    from app import exporter, publish, storage_guard
    from app.tts import TTSResult
    with sessions() as db:
        article = add_article(db)
        article.image_provider = "unsplash"
        article.image_url = "https://example.test/image"
        add_versions(db, article)
        versions = list(db.scalars(select(ArticleVersion).order_by(ArticleVersion.id)))
        for v in versions[2:]:
            db.add(AudioAsset(version_id=v.id, lang=v.lang, voice="aria", file_path="old.mp3",
                              duration_ms=1000, size_bytes=10, engine="edge-tts"))
        if legacy_silent:
            db.add(AudioAsset(version_id=versions[0].id, lang="en", voice="aria",
                              file_path="old.wav", duration_ms=1000, size_bytes=10,
                              engine="silent-fallback"))
        failed_id, good_id = versions[0].id, versions[1].id
        db.commit()
    synth = AsyncMock(side_effect=([TTSResult(b"ID3real", 1)] if legacy_silent else
                                  [RuntimeError("refused"), TTSResult(b"ID3real", 1)]))
    storage = SimpleNamespace(put=Mock(return_value=SimpleNamespace(url="https://example.test/audio")))
    monkeypatch.setattr(pipeline, "SessionLocal", sessions)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(pipeline, "synthesize", synth)
    monkeypatch.setattr(pipeline.settings, "tts_enabled", True)
    monkeypatch.setattr(pipeline, "get_audio_storage", lambda: storage)
    monkeypatch.setattr(storage_guard, "check_capacity", lambda: None)
    forbidden = Mock(side_effect=AssertionError("publication must not start"))
    monkeypatch.setattr(exporter, "export_all", forbidden)
    monkeypatch.setattr(publish, "publish_all", forbidden)
    warmup = AsyncMock(side_effect=AssertionError("must not warm up"))
    monkeypatch.setattr(alt_voices, "warm_alt_voices", warmup)
    result = await pipeline.run_daily()
    assert result["status"] == "failed"
    assert result["stats"]["audio_errors"] == 1
    assert result["stats"]["audios"] == 1
    assert storage.put.call_count == 1
    assert synth.await_count == (1 if legacy_silent else 2)
    forbidden.assert_not_called()
    warmup.assert_not_called()
    with sessions() as db:
        assert len(list(db.scalars(select(ArticleVersion)))) == 10
        assert db.scalar(select(Article)).tts_status == "partial"
        failed = db.get(ArticleVersion, failed_id)
        assert (failed.audio.engine == "silent-fallback") if legacy_silent else failed.audio is None
        assert db.get(ArticleVersion, good_id).audio.engine == "edge-tts"
