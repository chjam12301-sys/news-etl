"""Legacy silent assets stay stored but never become playable API/export output."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import api, exporter
from app.audio_store import is_playable_audio
from app.content_hash import from_version
from app.db import Base, ArticleVersion, AudioAsset
from app.schemas import ArticleSummaryOut, ArticleDetailOut
from test_daily_publish import add_article, add_versions


@pytest.mark.parametrize("engine", ["silent-fallback", "edge-tts"])
def test_legacy_audio_all_output_paths(engine, monkeypatch):
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    with Session(database) as db:
        article = add_article(db)
        add_versions(db, article, {"en_a1"})
        v = db.query(ArticleVersion).one()
        asset = AudioAsset(version_id=v.id, lang="en", voice="aria", engine=engine,
                           file_path=f"audio/{article.id}/en_a1.wav",
                           public_url="https://example.test/audio", duration_ms=1000, size_bytes=10,
                           timeline=[{"i": 0, "w": "Hello", "s": 0, "e": 1,
                                      "sm": 0, "em": 1000, "cs": 0, "ce": 5, "si": 0}])
        db.add(asset)
        db.commit()
        playable = engine == "edge-tts"
        assert is_playable_audio(v.audio) == playable
        summary = exporter._version_summary(v, article, "")
        detail = exporter._version_detail(v, article, "")
        assert summary["has_audio"] == playable
        assert (detail["audio"] is not None) == playable
        assert bool(detail["timeline"]) == playable
        assert (ArticleSummaryOut.from_row(v, article).audio is not None) == playable
        assert (ArticleDetailOut.from_row(v, article, full=True).audio is not None) == playable
        assert (api._audio_out(v) is not None) == playable
        if not playable:
            assert detail["content_hash"] == from_version(v)
            assert from_version(v, asset) == from_version(v)
        else:
            assert detail["content_hash"] == v.content_hash
            assert from_version(v, asset) != from_version(v)
        read = Mock(return_value=b"fixture")
        from app import storage
        monkeypatch.setattr(storage, "get_storage", lambda: SimpleNamespace(read=read))
        endpoints = [lambda: api.version_audio(v.id, db),
                     lambda: api.serve_audio(asset.id, db),
                     lambda: api.version_timeline(v.id, "compact", db),
                     lambda: api.version_srt(v.id, db),
                     lambda: api.local_files(asset.file_path, db)]
        for endpoint in endpoints:
            if playable:
                assert endpoint() is not None
            else:
                with pytest.raises(HTTPException) as exc:
                    endpoint()
                assert exc.value.status_code == 404
        if not playable:
            read.assert_not_called()
        assert api.local_files("data/test.json", db).body == b"fixture"
        assert db.get(AudioAsset, asset.id) is not None


def test_backfill_keeps_silent_out_of_todo_and_catches_failures():
    # Inspect just the processing block; the script's operational setup must never run.
    tree = ast.parse(Path("scripts/backfill_audio.py").read_text())
    main = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    todo = next(n for n in main.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "todo" for t in n.targets))
    silent = SimpleNamespace(audio=SimpleNamespace(engine="silent-fallback"))
    missing = SimpleNamespace(audio=None)
    scope = {"rows": [(silent, None), (missing, None)]}
    exec(compile(ast.Module(body=[todo], type_ignores=[]), "backfill-todo", "exec"), scope)
    assert scope["todo"] == [(missing, None)]
    loop = next(n for n in main.body if isinstance(n, ast.For) and isinstance(n.iter, ast.Call)
                and isinstance(n.iter.func, ast.Name) and n.iter.func.id == "enumerate")
    async def fail(*args):
        raise RuntimeError("TTS refused")
    import asyncio
    db = SimpleNamespace(rollback=Mock(), add=Mock(), commit=Mock())
    store = SimpleNamespace(put=Mock())
    v = SimpleNamespace(article_id=1, level_code="en_a1", body="Hello.", lang="en")
    scope = {"todo": [(v, SimpleNamespace(id=1))], "total": 1,
             "ok": 0, "fail": 0, "synthesize": fail, "db": db, "storage": store}
    wrapper = ast.AsyncFunctionDef(name="run", args=ast.arguments(posonlyargs=[], args=[],
                                  kwonlyargs=[], kw_defaults=[], defaults=[]),
                                  body=[ast.Global(names=["ok", "fail"]), loop], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                 "backfill-loop", "exec"), scope)
    asyncio.run(scope["run"]())
    assert scope["fail"] == 1 and scope["ok"] == 0
    db.add.assert_not_called()
    db.commit.assert_not_called()
    store.put.assert_not_called()
    db.rollback.assert_called_once()


@pytest.mark.parametrize("stored_key", ["audio//1/en_a1.wav", "audio/1/./en_a1.wav",
                                        r"audio\1\en_a1.wav", "audio/2/../1/en_a1.wav"])
def test_legacy_database_key_alias_is_filtered(tmp_path, monkeypatch, stored_key):
    from app import storage
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    local = storage.LocalStorage(tmp_path)
    local.put("audio/1/en_a1.wav", b"legacy-silent")
    local.put("audio/1/en_a2.mp3", b"real-fixture")
    local.put("data/index.json", b"{}")
    monkeypatch.setattr(storage, "get_storage", lambda: local)
    with Session(database) as db:
        db.add(AudioAsset(version_id=1, lang="en", voice="aria", engine="silent-fallback",
                          file_path=stored_key, duration_ms=1000, size_bytes=13))
        db.commit()
        with pytest.raises(HTTPException) as failure:
            api.local_files("audio/1/en_a1.wav", db)
        assert failure.value.status_code == 404
        assert api.local_files("audio/1/./en_a2.mp3", db).body == b"real-fixture"
        assert api.local_files("data//index.json", db).body == b"{}"
