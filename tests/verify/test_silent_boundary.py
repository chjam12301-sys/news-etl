"""Independent offline checks for TTS timeout and legacy file aliases."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import api, storage, tts
from app.db import Base, AudioAsset


@pytest.mark.asyncio
async def test_timeout_propagates_with_original_cause(monkeypatch):
    original = asyncio.TimeoutError('fixture timeout')
    monkeypatch.setattr(tts.settings, 'tts_enabled', True)
    monkeypatch.setattr(tts, '_synth_edge', AsyncMock(side_effect=original))
    with pytest.raises(RuntimeError) as failure:
        await tts.synthesize('Hello.', 'en')
    assert failure.value.__cause__ is original


@pytest.mark.parametrize('key', ['audio/1/en_a1.wav', 'audio//1/en_a1.wav',
                                 'audio/1/./en_a1.wav', r'audio\1\en_a1.wav'])
def test_legacy_local_alias_is_not_served(tmp_path, monkeypatch, key):
    db_engine = create_engine('sqlite://')
    Base.metadata.create_all(db_engine)
    local = storage.LocalStorage(tmp_path)
    local.put('audio/1/en_a1.wav', b'legacy-silent-fixture')
    monkeypatch.setattr(storage, 'get_storage', lambda: local)
    with Session(db_engine) as db:
        db.add(AudioAsset(version_id=1, lang='en', voice='fixture', engine='silent-fallback',
                          file_path='audio/1/en_a1.wav', duration_ms=1000, size_bytes=21))
        db.commit()
        with pytest.raises(HTTPException) as failure:
            api.local_files(key, db)
        assert failure.value.status_code == 404
