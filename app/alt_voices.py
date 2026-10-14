"""可选音色：给一篇文章的 5 个英语等级各生成 N 个 Kokoro 音色（默认 4 → 20 份）。

背景
----
原先这套走外部 Cloudflare Worker（微软 edge-tts 音色 ava/emma/sonia/guy），
Worker 现返回 `503 generation_unavailable`（微软端点对数据中心 IP 封 403），不可恢复。
改为**本流水线用 Kokoro 自托管生成**，音源与默认音色完全一致。

容错（按用户要求）
------------------
- 每份**独立重试 3 次**（指数退避）；单份失败不影响其它；
- **可续跑**：已存在 DB 行即跳过，重跑只补缺的；
- **非致命**：整体失败也只记录，绝不回滚已发布内容。

产出
----
- 音频进 R2：`audio/<article_id>/<level_code>_<voice>.mp3`（走对象存储，不进 Git）；
- 元数据（含词级时间轴）落 `alt_voice_assets` 表，供导出到详情 JSON。
"""
from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from .audio_store import get_audio_storage, public_audio_url
from .config import settings
from .db import AltVoiceAsset, ArticleVersion
from .levels import EN_LEVELS
from .tts import synthesize

log = logging.getLogger(__name__)

# 默认 4 个 Kokoro 英语音色（美/英 × 男/女各一）；可用 env ALT_VOICES 覆盖（逗号分隔）。
DEFAULT_VOICES = ("af_bella", "am_michael", "bf_emma", "bm_george")
_RETRIES = 3


def alt_voices() -> tuple[str, ...]:
    """可选音色清单：env ALT_VOICES（逗号分隔）优先，否则用默认 4 个。"""
    raw = (os.environ.get("ALT_VOICES") or "").strip()
    if not raw:
        return DEFAULT_VOICES
    return tuple(v.strip() for v in raw.split(",") if v.strip())


def _alt_key(article_id: int, level_code: str, voice: str) -> str:
    return f"audio/{article_id}/{level_code}_{voice}.mp3"


async def _one(
    db: Session, version: ArticleVersion, voice: str, storage, stats: dict
) -> None:
    """生成并上传单份可选音色；带重试，失败只记录。"""
    key = _alt_key(version.article_id, version.level_code, voice)
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            res = await synthesize(version.body, "en", voice)
            storage.put(key, res.audio_bytes, content_type="audio/mpeg")
            row = db.scalar(
                select(AltVoiceAsset).where(
                    AltVoiceAsset.version_id == version.id, AltVoiceAsset.voice == voice
                )
            )
            if row is None:
                row = AltVoiceAsset(version_id=version.id, voice=voice)
                db.add(row)
            row.file_path = key
            row.public_url = public_audio_url(file_path=key) or ""
            row.duration_ms = int(res.duration * 1000)
            row.size_bytes = len(res.audio_bytes)
            row.timeline = res.timeline_dict()
            row.engine = res.engine
            # 逐份提交：中途挂掉时已完成的不会白跑（可续跑）
            db.commit()
            stats["generated"] += 1
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            db.rollback()
            if attempt < _RETRIES - 1:
                await asyncio.sleep(2 ** (attempt + 1))
    stats["failed"] += 1
    stats["items"].append(
        {
            "level": version.level_code,
            "voice": voice,
            "status": "failed",
            "error": f"{type(last).__name__}: {str(last)[:160]}",
        }
    )
    log.warning(
        "[alt] %s/%s 失败（重试 %d 次后放弃）：%s", version.level_code, voice, _RETRIES, last
    )


async def warm_alt_voices(db: Session, article, *, storage=None) -> dict:
    """为 article 的 5 个英语等级生成全部可选音色。返回统计，绝不抛错给调用方。"""
    voices = alt_voices()
    stats: dict = {
        "article_id": article.id,
        "voices": list(voices),
        "generated": 0,
        "skipped": 0,
        "failed": 0,
        "items": [],
    }
    if not voices:
        return {**stats, "status": "skipped", "reason": "no_voices"}
    if not settings.tts_enabled:
        return {**stats, "status": "skipped", "reason": "tts_disabled"}

    storage = storage or get_audio_storage()
    versions = list(
        db.scalars(
            select(ArticleVersion).where(
                ArticleVersion.article_id == article.id, ArticleVersion.lang == "en"
            )
        )
    )
    by_code = {v.level_code: v for v in versions}
    for level in EN_LEVELS:
        version = by_code.get(level.code)
        if version is None:
            continue
        have = {
            r.voice
            for r in db.scalars(
                select(AltVoiceAsset).where(AltVoiceAsset.version_id == version.id)
            )
        }
        for voice in voices:
            if voice in have:
                stats["skipped"] += 1
                continue
            await _one(db, version, voice, storage, stats)

    stats["status"] = "ok" if stats["failed"] == 0 else "partial"
    log.info(
        "[alt] article=%s 可选音色：生成 %d / 跳过 %d / 失败 %d（%d 音色 × %d 级）",
        article.id,
        stats["generated"],
        stats["skipped"],
        stats["failed"],
        len(voices),
        len(EN_LEVELS),
    )
    return stats
