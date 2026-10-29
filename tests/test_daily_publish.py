import ast
import asyncio
import datetime as dt
import json
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
    monkeypatch.setattr(fetcher, "fetch_feed", lambda feed, client, limit=12: [candidates[feed]])
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
async def test_warmup_only_skips_pipeline_but_republishes_changes(sessions, monkeypatch):
    """--warmup-only：不抓新闻、不改写、不重跑默认音频；但**有变化就要重发 JSON**。

    下架音色（removed>0）尤其必须重发：DB 行与对象都删了，已发布详情却仍列着
    这个 voice 与其 URL —— 用户侧就是死链，而这条路径原本没有任何出口去修正。
    """
    with sessions() as db:
        add_article(db)
    monkeypatch.setattr(pipeline, "SessionLocal", sessions)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    process = AsyncMock(side_effect=AssertionError("must not process"))
    monkeypatch.setattr(pipeline, "_process_article", process)
    monkeypatch.setattr(pipeline, "get_storage",
                        lambda: SimpleNamespace(provider="github", prune=lambda n: {"deleted": 0}))
    from app import exporter, publish
    monkeypatch.setattr(exporter, "export_all", lambda db, days=3: {"exported": True})
    pub = Mock(return_value={"ok": True, "verify": "校验通过", "index_rev": "r"})
    monkeypatch.setattr(publish, "publish_all", pub)
    monkeypatch.setattr(alt_voices, "warm_alt_voices",
                        AsyncMock(return_value={"generated": 0, "removed": 2, "status": "ok"}))
    result = await pipeline.run_daily(warmup_only=True)
    assert result["status"] == "success"
    process.assert_not_called()
    assert result["stats"]["alt_publish_ok"] is True
    pub.assert_called_once()


@pytest.mark.asyncio
async def test_warmup_days_backfills_recent_articles_only(sessions, monkeypatch):
    """`--warmup-days N` 是历史缺口的回填口子：只补最近 N 天的可选音色，
    不抓新闻、不改写、不动默认音频。没有文章的那天跳过而不是报错。

    为什么必须有它：日常 run 只碰当天那篇，历史文章的 timeline_url 一旦
    404（音色在「时间轴独立存」改造之前生成）就永远没人补。
    """
    day = dt.date(2026, 10, 14)
    with sessions() as db:
        a_today = add_article(db, day, title="Today story")
        a_y1 = add_article(db, day - dt.timedelta(days=1), title="Yesterday story")
        wanted = sorted([a_today.id, a_y1.id])
    monkeypatch.setattr(pipeline, "SessionLocal", sessions)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(pipeline, "collect", Mock(side_effect=AssertionError("must not fetch")))
    monkeypatch.setattr(pipeline, "_process_article", AsyncMock(side_effect=AssertionError("must not rewrite")))
    # 有增量才重发一次 JSON（音频在对象存储不进 Git，重发的是详情里的 alternates）
    from app import exporter, publish
    monkeypatch.setattr(pipeline, "get_storage",
                        lambda: SimpleNamespace(provider="github", prune=lambda n: {"deleted": 0}))
    export = Mock(return_value={"exported": True})
    monkeypatch.setattr(exporter, "export_all", export)
    pub = Mock(return_value={"ok": True, "verify": "校验通过"})
    monkeypatch.setattr(publish, "publish_all", pub)

    seen: list[int] = []

    async def fake_warm(db, article, *, with_tts=True):
        seen.append(article.id)
        return {"generated": 0, "timeline_uploaded": 20, "removed": 0, "status": "ok"}

    monkeypatch.setattr(alt_voices, "warm_alt_voices", fake_warm)

    result = await pipeline.run_daily(warmup_days=3, day_override=day)
    assert result["status"] == "success"
    bf = result["stats"]["backfill"]
    # 3 天窗口里有 2 天有文章，第 3 天没有 → 跳过而不是失败
    assert bf["articles"] == 2 and bf["timeline_uploaded"] == 40
    assert sorted(seen) == wanted
    assert sum(1 for i in bf["items"] if i.get("status") == "no_article") == 1
    assert "article_id" not in result["stats"]
    # 补到了东西 → 重导出 + 重发一次
    assert result["stats"]["publish_ok"] is True
    export.assert_called_once()
    assert "回填" in pub.call_args.kwargs["commit_msg"]


@pytest.mark.asyncio
async def test_backfill_window_drives_export_and_publish_days(sessions, monkeypatch):
    """回填窗口必须驱动导出与发布的**天数**：写死 3 的话第 4 天及更早只传对象、
    不重发 JSON，用户侧零变化而 job 照样报成功。"""
    day = dt.date(2026, 10, 14)
    with sessions() as db:
        add_article(db, day, title="Window story")
    monkeypatch.setattr(pipeline, "SessionLocal", sessions)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(pipeline, "collect", Mock(side_effect=AssertionError("must not fetch")))
    from app import exporter, publish

    export_days: list[int] = []

    def fake_export(db, days=3):
        export_days.append(days)
        return {"exported": True}

    monkeypatch.setattr(exporter, "export_all", fake_export)
    pub = Mock(return_value={"ok": True, "verify": "校验通过", "index_rev": "r"})
    monkeypatch.setattr(publish, "publish_all", pub)
    monkeypatch.setattr(pipeline, "get_storage",
                        lambda: SimpleNamespace(provider="github", prune=lambda n: {"deleted": 0}))

    async def fake_warm(db, article, *, with_tts=True):
        return {"generated": 1, "timeline_uploaded": 0, "removed": 0, "status": "ok"}

    monkeypatch.setattr(alt_voices, "warm_alt_voices", fake_warm)

    result = await pipeline.run_daily(warmup_days=5, day_override=day)
    assert result["status"] == "success"
    assert export_days == [5]
    # 2026-10-29 起：发布不再按回填窗口缩短索引 —— 索引天数由
    # exporter.MAX_DAYS_IN_INDEX（=仓库保留期）统一决定，回填只要 ≤ 它，
    # 就会被这次发布覆盖到。这里守住"回填内容真的被发布"这个原始意图。
    from app.exporter import MAX_DAYS_IN_INDEX
    assert MAX_DAYS_IN_INDEX >= 5
    assert pub.call_args.kwargs.get("topics_days") is None


@pytest.mark.asyncio
async def test_backfill_covers_every_article_of_the_day(sessions, monkeypatch):
    """同一天有多篇文章时都要回填 —— 导出与发布是按 published_date 全量覆盖的，
    只暖 daily_target 那 1 篇会让同日其它文章继续留着 404。"""
    day = dt.date(2026, 10, 14)
    with sessions() as db:
        a1 = add_article(db, day, title="First story of the day")
        a2 = add_article(db, day, title="Second story of the day")
        wanted = sorted([a1.id, a2.id])
    monkeypatch.setattr(pipeline, "SessionLocal", sessions)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(pipeline, "collect", Mock(side_effect=AssertionError("must not fetch")))

    seen: list[int] = []

    async def fake_warm(db, article, *, with_tts=True):
        seen.append(article.id)
        return {"generated": 0, "timeline_uploaded": 0, "removed": 0, "status": "ok"}

    monkeypatch.setattr(alt_voices, "warm_alt_voices", fake_warm)
    result = await pipeline.run_daily(warmup_days=1, day_override=day)
    assert result["status"] == "success"
    assert sorted(seen) == wanted
    assert result["stats"]["backfill"]["articles"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["raise", "engine_mismatch", "no_voices"])
async def test_backfill_zero_output_is_not_silent_success(sessions, monkeypatch, mode):
    """回填「一篇都没跑成」不能是绿色 success：TTS 全挂 / 引擎不匹配 / 没配音色
    都会让每天返回空统计，若不看 status 就会留下一条 success 却零产出的 JobRun。"""
    day = dt.date(2026, 10, 14)
    with sessions() as db:
        add_article(db, day, title="Zero output story")
    monkeypatch.setattr(pipeline, "SessionLocal", sessions)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(pipeline, "collect", Mock(side_effect=AssertionError("must not fetch")))

    if mode == "raise":

        async def fake_warm(db, article, *, with_tts=True):
            raise RuntimeError("tts down")

    else:

        async def fake_warm(db, article, *, with_tts=True):  # noqa: F811
            return {"status": "skipped", "reason": mode, "generated": 0,
                    "timeline_uploaded": 0, "removed": 0}

    monkeypatch.setattr(alt_voices, "warm_alt_voices", fake_warm)
    result = await pipeline.run_daily(warmup_days=1, day_override=day)
    assert result["status"] == "failed"
    assert "backfill produced nothing" in (result["error"] or "")
    bf = result["stats"]["backfill"]
    assert bf["articles_failed"] == bf["articles"] == 1
    assert bf["reasons"]
    with sessions() as db:
        assert db.scalar(select(JobRun)).status == "failed"


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
        # 词级时间轴单独存一个 JSON 对象（与音频同桶），不内联进详情
        tl_call = put.call_args_list[1]
        assert tl_call.args[0] == f"timeline/{article.id}/en_a1_af_bella.json"
        assert tl_call.kwargs["content_type"].startswith("application/json")
        assert json.loads(tl_call.args[1].decode()) == [{"i": 0}]

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
        # 清单改名 → 旧音色下架：**音频对象与时间轴对象都要删**，
        # 且 DB 行的删除必须先提交（否则后续某份音色的 rollback 会让行复活，
        # 而它的音频已经被删了 → 对外死链）。
        monkeypatch.setattr(av, "alt_voices", lambda: ("bf_emma",))
        s = await av.warm_alt_voices(db, article, storage=storage)
        assert s["removed"] == 1 and s["generated"] == 1
        assert db.scalar(select(AltVoiceAsset).where(AltVoiceAsset.voice == "af_bella")) is None
        deleted = {c.args[0] for c in delete.call_args_list}
        assert f"audio/{article.id}/en_a1_af_bella.mp3" in deleted
        assert f"timeline/{article.id}/en_a1_af_bella.json" in deleted
        # 提交已落：模拟「后面某一份音色失败触发回滚」，行不能复活
        db.rollback()
        assert db.scalar(select(AltVoiceAsset).where(AltVoiceAsset.voice == "af_bella")) is None


@pytest.mark.asyncio
async def test_alt_voices_regenerates_when_audio_object_is_gone(sessions, monkeypatch):
    """DB 行在、但对象存储的音频确实没了（桶被清/换桶）→ 必须重新合成。

    反向约束：只有**确定不在**才重做；probe 返回 None（三条通道都判断不了）
    时要维持现状，否则一次网络抖动就会让 20 份音色全部重新合成。
    """
    from app import alt_voices as av
    from app.db import AltVoiceAsset

    monkeypatch.setattr(av.settings, "tts_enabled", True)
    monkeypatch.setattr(av, "_engine", lambda: "kokoro")
    monkeypatch.setattr(av, "alt_voices", lambda: ("af_bella",))
    synth = AsyncMock(return_value=SimpleNamespace(
        audio_bytes=b"ID3xxxx", duration=1.0, engine="kokoro",
        timeline_dict=lambda: [{"i": 0}]))
    monkeypatch.setattr(av, "synthesize", synth)
    monkeypatch.setattr(av, "_sleep", AsyncMock())
    storage = SimpleNamespace(put=Mock(), delete=Mock(), probe=lambda key: False)

    with sessions() as db:
        article = add_article(db, title="Lost audio object story")
        add_versions(db, article, codes={"en_a1"}, with_audio=True)
        v = db.scalar(select(ArticleVersion).where(
            ArticleVersion.level_code == "en_a1", ArticleVersion.article_id == article.id))
        db.add(AltVoiceAsset(version_id=v.id, voice="af_bella",
                             file_path=f"audio/{article.id}/en_a1_af_bella.mp3",
                             duration_ms=1000, size_bytes=100,
                             timeline=[{"i": 0}], engine="kokoro"))
        db.commit()
        s = await av.warm_alt_voices(db, article, storage=storage)
        assert s["generated"] == 1 and s["skipped"] == 0
        synth.assert_awaited_once()

    # 判断不了（None）→ 不重做，也不算「跳过就万事大吉」地假装资产齐全
    storage_unknown = SimpleNamespace(put=Mock(), delete=Mock(), probe=lambda key: None)
    with sessions() as db:
        article = add_article(db, title="Unknown audio state story")
        add_versions(db, article, codes={"en_a1"}, with_audio=True)
        v = db.scalar(select(ArticleVersion).where(
            ArticleVersion.level_code == "en_a1", ArticleVersion.article_id == article.id))
        db.add(AltVoiceAsset(version_id=v.id, voice="af_bella",
                             file_path=f"audio/{article.id}/en_a1_af_bella.mp3",
                             duration_ms=1000, size_bytes=100,
                             timeline=[{"i": 0}], engine="kokoro"))
        db.commit()
        synth.reset_mock()
        s = await av.warm_alt_voices(db, article, storage=storage_unknown)
        assert s["generated"] == 0 and s["skipped"] == 1
        synth.assert_not_awaited()


@pytest.mark.asyncio
async def test_alt_voices_self_heals_missing_timeline_object(sessions, monkeypatch):
    """历史音色只有音频、没有独立时间轴对象时，补传即可，**不重新合成**。"""
    from app import alt_voices as av
    from app.db import AltVoiceAsset

    monkeypatch.setattr(av.settings, "tts_enabled", True)
    monkeypatch.setattr(av, "_engine", lambda: "kokoro")
    monkeypatch.setattr(av, "alt_voices", lambda: ("af_bella",))
    synth = AsyncMock(side_effect=AssertionError("must not re-synthesize"))
    monkeypatch.setattr(av, "synthesize", synth)
    # 自愈路径也要把 timeline_url 写回行里，否则存量行的该列恒为 NULL
    monkeypatch.setattr(av, "public_timeline_url",
                        lambda aid, code, voice: f"https://cdn.test/timeline/{aid}/{code}_{voice}.json")
    put = Mock()
    # 音频对象在、时间轴对象不在 —— 只有后者需要补（probe 给三态，
    # 这里模拟真实历史数据：音频是当年传的，时间轴对象从没传过）
    present: set[str] = set()
    storage = SimpleNamespace(put=put, delete=Mock(), probe=lambda key: key in present)

    with sessions() as db:
        article = add_article(db, title="Self heal timeline story")
        add_versions(db, article, codes={"en_a1"}, with_audio=True)
        v = db.scalar(select(ArticleVersion).where(ArticleVersion.level_code == "en_a1"))
        audio_path = f"audio/{article.id}/en_a1_af_bella.mp3"
        present.add(audio_path)
        db.add(AltVoiceAsset(version_id=v.id, voice="af_bella",
                             file_path=audio_path,
                             duration_ms=1000, size_bytes=100,
                             timeline=[{"i": 0, "w": "x", "cs": 0, "ce": 1}], engine="kokoro"))
        db.commit()
        s = await av.warm_alt_voices(db, article, storage=storage)
        assert s["skipped"] == 1 and s["generated"] == 0
        assert s["timeline_uploaded"] == 1
        synth.assert_not_awaited()
        put.assert_called_once()
        assert put.call_args.args[0] == f"timeline/{article.id}/en_a1_af_bella.json"
        row = db.scalar(select(AltVoiceAsset))
        assert row.timeline_url == f"https://cdn.test/timeline/{article.id}/en_a1_af_bella.json"


def test_exporter_exposes_timeline_url_and_never_inlines_it(monkeypatch, sessions):
    """可选音色的时间轴必须是**独立文件的 URL**，不能内联进详情。

    内联 4 份会让最大等级（en_c1）的详情从 ~59KB 涨到 ~217KB，而用户通常并不切音色。
    """
    from app import exporter
    from app.db import AltVoiceAsset

    monkeypatch.setattr(exporter, "public_audio_url",
                        lambda **kw: "https://cdn.test/" + (kw.get("file_path") or ""))
    monkeypatch.setattr(exporter, "public_timeline_url",
                        lambda aid, code, voice: f"https://cdn.test/timeline/{aid}/{code}_{voice}.json")
    with sessions() as db:
        article = add_article(db, title="Timeline url story")
        add_versions(db, article, codes={"en_a1"}, with_audio=True)
        v = db.scalar(select(ArticleVersion).where(ArticleVersion.level_code == "en_a1"))
        big_tl = [{"i": i, "w": "x", "cs": i, "ce": i + 1} for i in range(300)]
        db.add(AltVoiceAsset(version_id=v.id, voice="af_bella",
                             file_path=f"audio/{article.id}/en_a1_af_bella.mp3",
                             duration_ms=1000, size_bytes=100, timeline=big_tl, engine="kokoro"))
        db.commit()

        (alt,) = exporter._audio_meta(v, base="", rev="")["alternates"]
        assert alt["timeline_url"].endswith(f"/timeline/{article.id}/en_a1_af_bella.json")
        assert "timeline" not in alt

        detail = exporter._version_detail(v, article, base="", rev="")
        assert all("timeline" not in x for x in detail["audio"]["alternates"])
        # 体积守卫：把 300 词的时间轴换成 1 词，详情体积几乎不变 → 证明详情不含它
        size_big = len(json.dumps(detail, ensure_ascii=False))
        row = db.scalar(select(AltVoiceAsset))
        row.timeline = [{"i": 0, "w": "x", "cs": 0, "ce": 1}]
        db.commit()
        size_small = len(json.dumps(exporter._version_detail(v, article, base="", rev=""), ensure_ascii=False))
        assert abs(size_big - size_small) < 300


def test_exporter_timeline_url_follows_stored_value(sessions, monkeypatch):
    """没有词级时间轴就不给地址（否则 App 去拉必然 404 的 JSON）；
    有则以**库里存的 URL** 为准 —— 现拼的话，换桶后新拼的时间轴会指向新桶，
    而音频还在旧桶，两者分家。"""
    from app import exporter
    from app.db import AltVoiceAsset

    monkeypatch.setattr(exporter, "public_audio_url",
                        lambda **kw: "https://cdn.test/" + (kw.get("file_path") or ""))
    monkeypatch.setattr(exporter, "public_timeline_url",
                        lambda aid, code, voice: f"https://cdn.test/timeline/{aid}/{code}_{voice}.json")
    with sessions() as db:
        article = add_article(db, title="No timeline story")
        add_versions(db, article, codes={"en_a1"}, with_audio=True)
        v = db.scalar(select(ArticleVersion).where(
            ArticleVersion.level_code == "en_a1", ArticleVersion.article_id == article.id))
        db.add(AltVoiceAsset(version_id=v.id, voice="af_bella",
                             file_path=f"audio/{article.id}/en_a1_af_bella.mp3",
                             duration_ms=1000, size_bytes=100, timeline=[], engine="kokoro"))
        db.commit()

        (alt,) = exporter._audio_meta(v, base="", rev="")["alternates"]
        assert alt["has_timeline"] is False
        assert "timeline_url" not in alt

        row = db.scalar(select(AltVoiceAsset))
        row.timeline = [{"i": 0, "w": "x", "cs": 0, "ce": 1}]
        row.timeline_url = "https://old-bucket.test/timeline/x.json"
        db.commit()
        (alt,) = exporter._audio_meta(v, base="", rev="")["alternates"]
        assert alt["has_timeline"] is True
        assert alt["timeline_url"] == "https://old-bucket.test/timeline/x.json"


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


def test_size_gate_prunes_with_repository_retention(monkeypatch, tmp_path):
    """体积闸门清理的天数必须等于仓库保留期（也就是索引窗口）。

    按 3 天清理、却按 14 天列索引，等于给 App 发 11 个必然 404 的日期，
    发布校验也会当场失败。这是一条纯一致性约束，用假存储钉住调用参数。
    """
    from app import publish
    from app.config import settings

    prune = Mock(return_value={"deleted": 0})
    storage = SimpleNamespace(provider="github", repo_dir=str(tmp_path), prune=prune)
    # 第一次读体积触发闸门，清理后仍超上限 → 走 size_exceeded 提前返回，
    # 这样不必构造真实 DB 与导出链就能钉住 prune 的参数
    sizes = iter([publish.JSDELIVR_LIMIT_MB, publish.JSDELIVR_LIMIT_MB * 2])
    monkeypatch.setattr(publish, "repo_size_mb", lambda _: next(sizes))
    result = publish.publish_all(db=None, storage=storage)
    prune.assert_called_once_with(keep_days=settings.github_keep_days)
    assert result["ok"] is False and result["reason"] == "size_exceeded"


def test_republish_default_days_follows_index_window():
    """`scripts/republish.py --days` 的缺省值必须交给 publish_all 取索引窗口。

    这是"修索引"的唯一入口：它若按 3 天发一份索引，就会把刚放开的历史日期
    又收窄回 3 天，而且症状是"索引看起来正常、只是历史变少了"，很难发现。
    """
    tree = ast.parse(Path("scripts/republish.py").read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "add_argument" and n.args
        and isinstance(n.args[0], ast.Constant) and n.args[0].value == "--days"
    ]
    assert len(calls) == 1
    default = next(k.value for k in calls[0].keywords if k.arg == "default")
    assert isinstance(default, ast.Constant) and default.value is None
