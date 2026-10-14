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
- **非致命**：整体失败也只记录，绝不回滚已发布内容；
- **格式安全**：上传前按字节嗅探（`sniff_audio`），识别不出就判失败——
  绝不把 WAV 字节以 `.mp3`/`audio/mpeg` 上传（no-silent-tts 禁止的静默坏资产）。

配置
----
音色清单来自 `ALT_VOICES`（Settings，读 .env；默认 4 个 Kokoro 英语音色）。
改动清单后重跑，会**清理**已下架音色的 DB 行与对象存储对象（否则会继续对外宣称）。

产出
----
- 音频进对象存储：`audio/<article_id>/<level_code>_<voice>.<ext>`（不进 Git）；
- 元数据（含词级时间轴）落 `alt_voice_assets`，并**重算该版本的 content_hash**
  ——否则「后补的音色」不会改变指纹，App 会判定内容未更新而永远拉不到。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .audio_store import AUDIO_CONTENT_TYPES, get_audio_storage, public_audio_url, sniff_audio
from .config import settings
from .content_hash import from_version
from .db import AltVoiceAsset, ArticleVersion
from .levels import EN_LEVELS
from .tts import synthesize

log = logging.getLogger(__name__)

# Kokoro 音色名形如 af_heart / bf_emma / bm_george：前缀 2 字母（性别+口音）+ 下划线 + 名字
_VOICE_RE = re.compile(r"^[a-z]{2}_[a-z][a-z0-9_]*$")
_RETRIES = 3

# 供测试打补丁的别名（直接 patch asyncio.sleep 会污染整个进程）
_sleep = asyncio.sleep


def alt_voices() -> tuple[str, ...]:
    """可选音色清单：来自 Settings（env ALT_VOICES / .env），带格式校验。

    非法名字**在这里**就拦下并整体跳过，而不是让 20 个文件各失败 3 次才发现。
    """
    raw = (settings.alt_voices or "").strip()
    voices = tuple(v.strip() for v in raw.split(",") if v.strip())
    if not voices:
        return ()
    bad = [v for v in voices if not _VOICE_RE.match(v)]
    if bad:
        log.warning("[alt] ALT_VOICES 含非法音色名 %s，已整体跳过", bad)
        return ()
    return voices


def _engine() -> str:
    return (os.environ.get("TTS_ENGINE") or "kokoro").strip().lower()


def _alt_key(article_id: int, level_code: str, voice: str, ext: str) -> str:
    return f"audio/{article_id}/{level_code}_{voice}.{ext}"


def _refresh_hash(db: Session, version: ArticleVersion) -> None:
    """可选音色变了就重算 content_hash，否则 App 认为内容没更新、永远拉不到。"""
    rows = list(
        db.scalars(select(AltVoiceAsset).where(AltVoiceAsset.version_id == version.id))
    )
    version.content_hash = from_version(version, version.audio, rows)


def _drop_stale(
    db: Session, version: ArticleVersion, keep: set[str], storage, stats: dict
) -> None:
    """清理已下架的可选音色：DB 行 + 对象存储对象（best-effort）。"""
    stale = [
        r
        for r in db.scalars(
            select(AltVoiceAsset).where(AltVoiceAsset.version_id == version.id)
        )
        if r.voice not in keep
    ]
    for row in stale:
        path = row.file_path or ""
        db.delete(row)
        stats["removed"] += 1
        deleter = getattr(storage, "delete", None)
        if deleter and path:
            try:
                deleter(path)
            except Exception as exc:  # noqa: BLE001
                # 对象删不掉只影响空间回收，不影响正确性（DB 行已删，不会再被导出）
                log.warning("[alt] 删除旧音色对象失败 %s：%s", path, exc)
    if stale:
        db.flush()


async def _one(
    db: Session, version: ArticleVersion, voice: str, storage, stats: dict
) -> None:
    """生成并上传单份可选音色；带重试，失败只记录。"""
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            res = await synthesize(version.body, "en", voice)
            # 先嗅探再决定 key/Content-Type：MP3 编码器不可用时 Kokoro 会回退 WAV，
            # 此时若仍按 .mp3 上传，App 会拿到解不出来的音频（静默坏资产）。
            fmt = sniff_audio(res.audio_bytes)
            if not fmt:
                raise RuntimeError("音频字节无法识别格式，拒绝上传坏资产")
            ext, ctype = AUDIO_CONTENT_TYPES[fmt]
            key = _alt_key(version.article_id, version.level_code, voice, ext)
            storage.put(key, res.audio_bytes, content_type=ctype)
            row = db.scalar(
                select(AltVoiceAsset).where(
                    AltVoiceAsset.version_id == version.id, AltVoiceAsset.voice == voice
                )
            )
            if row is None:
                row = AltVoiceAsset(version_id=version.id, voice=voice)
                db.add(row)
            elif row.file_path != key and row.file_path:
                # 换了扩展名（如上次回退成 wav、这次 MP3 编码恢复）：清掉旧对象
                deleter = getattr(storage, "delete", None)
                if deleter:
                    try:
                        deleter(row.file_path)
                    except Exception:  # noqa: BLE001
                        pass
            row.file_path = key
            row.public_url = public_audio_url(file_path=key) or ""
            row.duration_ms = int(res.duration * 1000)
            row.size_bytes = len(res.audio_bytes)
            row.timeline = res.timeline_dict()
            row.engine = res.engine
            # 逐份提交：下一份失败时的 rollback 不能把这一份已完成的成果带走
            #（否则「可续跑」名存实亡，重跑会全部重来）。
            db.commit()
            stats["generated"] += 1
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            db.rollback()
            if attempt < _RETRIES - 1:
                await _sleep(2 ** (attempt + 1))
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


async def warm_alt_voices(
    db: Session, article, *, storage=None, with_tts: bool = True
) -> dict:
    """为 article 的 5 个英语等级生成全部可选音色。返回统计，绝不抛错给调用方。"""
    stats: dict = {
        "article_id": article.id,
        "generated": 0,
        "skipped": 0,
        "failed": 0,
        "removed": 0,
        "items": [],
    }
    if not with_tts or not settings.tts_enabled:
        return {**stats, "status": "skipped", "reason": "tts_disabled"}
    if _engine() != "kokoro":
        # 可选音色是 Kokoro 专有概念；edge-tts 模式下把这些 voice id 塞过去只会
        # 20×3 次必失败，纯浪费。
        return {**stats, "status": "skipped", "reason": "engine_mismatch"}
    voices = alt_voices()
    if not voices:
        return {**stats, "status": "skipped", "reason": "no_voices"}
    stats["voices"] = list(voices)

    storage = storage or get_audio_storage()
    versions = list(
        db.scalars(
            select(ArticleVersion).where(
                ArticleVersion.article_id == article.id, ArticleVersion.lang == "en"
            )
        )
    )
    by_code = {v.level_code: v for v in versions}
    keep = set(voices)
    for level in EN_LEVELS:
        version = by_code.get(level.code)
        if version is None:
            continue
        # 默认音频都没有 → 该等级的 audio 在导出时是 null，alternates 不会被对外暴露
        # （_audio_meta 提前返回 None）。此时生成纯属浪费，跳过。
        if version.audio is None:
            stats["items"].append(
                {"level": level.code, "status": "skipped", "reason": "no_default_audio"}
            )
            continue
        have = {
            r.voice
            for r in db.scalars(
                select(AltVoiceAsset).where(AltVoiceAsset.version_id == version.id)
            )
        }
        _drop_stale(db, version, keep, storage, stats)
        for voice in voices:
            if voice in have:
                stats["skipped"] += 1
                continue
            await _one(db, version, voice, storage, stats)
        _refresh_hash(db, version)
        # 逐等级提交：中途挂掉时已完成的不会白跑（可续跑）
        db.commit()

    stats["status"] = "ok" if stats["failed"] == 0 else "partial"
    log.info(
        "[alt] article=%s 可选音色：生成 %d / 跳过 %d / 失败 %d / 下架 %d（%d 音色 × %d 级）",
        article.id,
        stats["generated"],
        stats["skipped"],
        stats["failed"],
        stats["removed"],
        len(voices),
        len(EN_LEVELS),
    )
    return stats
