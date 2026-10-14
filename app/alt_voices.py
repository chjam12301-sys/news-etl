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
- 词级时间轴**单独一个 JSON 对象**：`timeline/<article_id>/<level_code>_<voice>.json`
  （与音频同桶）。**不内联进详情 JSON** —— 内联 4 份会让最大等级的详情膨胀 3~4 倍，
  而用户绝大多数时候并不会切音色；
- 元数据落 `alt_voice_assets`，并**重算该版本的 content_hash** ——否则「后补的音色」
  不会改变指纹，App 会判定内容未更新而永远拉不到。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .audio_store import (
    AUDIO_CONTENT_TYPES,
    get_audio_storage,
    public_audio_url,
    public_timeline_url,
    sniff_audio,
    timeline_object_key,
)
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


def _object_state(storage, key: str) -> bool | None:
    """三态：True=对象在 / False=确定不在 / None=判断不了。

    优先 `storage.probe()`（能给 None）；老实现只有 `exists()` 时退回它，
    但它把「判断不了」压成 False，这里就跟着无法区分了 —— 这是可接受的降级，
    因为给不出答案时我们只在**幂等的小对象（时间轴）**上按「不在」处理。
    """
    if not key:
        return None
    probe = getattr(storage, "probe", None)
    if probe is not None:
        try:
            got = probe(key)
        except Exception:  # noqa: BLE001
            return None
        return got if got is None else bool(got)
    exists = getattr(storage, "exists", None)
    if exists is not None:
        try:
            return bool(exists(key))
        except Exception:  # noqa: BLE001
            return None
    return None


def _drop_stale(
    db: Session, version: ArticleVersion, keep: set[str], storage, stats: dict
) -> None:
    """清理已下架的可选音色：DB 行 + 音频对象 + 时间轴对象（best-effort）。

    顺序是**先提交 DB 删除、再删对象**：
    - 反过来做的话，一旦后面某一行的 TTS 失败触发 `db.rollback()`，被删掉音频的
      行会原地复活并继续对外宣称一个已不存在的 URL（死链）；
    - 先提交后，即使对象删不掉也只是占点空间，DB 行已经不再对外暴露，无正确性风险。
    """
    stale = [
        r
        for r in db.scalars(
            select(AltVoiceAsset).where(AltVoiceAsset.version_id == version.id)
        )
        if r.voice not in keep
    ]
    if not stale:
        return
    pending: list[tuple[str, str]] = []
    for row in stale:
        pending.append(
            (
                row.file_path or "",
                timeline_object_key(version.article_id, version.level_code, row.voice),
            )
        )
        db.delete(row)
        stats["removed"] += 1
    db.commit()

    deleter = getattr(storage, "delete", None)
    if deleter is None:
        return
    for audio_path, tl_key in pending:
        for key in (audio_path, tl_key):
            if not key:
                continue
            try:
                deleter(key)
            except Exception as exc:  # noqa: BLE001
                # 对象删不掉只影响空间回收，不影响正确性（DB 行已删，不会再被导出）
                log.warning("[alt] 删除旧音色对象失败 %s：%s", key, exc)


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
            # 时间轴单独存一个 JSON 对象（与音频同桶），不内联进详情 JSON：
            # 内联会让最大等级的详情膨胀 3~4 倍，而用户通常并不会切音色。
            timeline = res.timeline_dict()
            storage.put(
                timeline_object_key(version.article_id, version.level_code, voice),
                json.dumps(timeline, ensure_ascii=False).encode("utf-8"),
                content_type="application/json; charset=utf-8",
            )
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
            row.timeline_url = public_timeline_url(
                version.article_id, version.level_code, voice
            ) or ""
            row.duration_ms = int(res.duration * 1000)
            row.size_bytes = len(res.audio_bytes)
            row.timeline = timeline
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


def _ensure_timeline(storage, version: ArticleVersion, row: AltVoiceAsset, stats: dict) -> None:
    """自愈：DB 行在、但时间轴对象缺失时补传（**不重新合成**，时间轴就在行里）。

    例：改成"时间轴单独存"之前生成的历史音色，只有音频没有时间轴对象 ——
    线上已发布的详情里 `alternates[].timeline_url` 因此全是 404，
    必须靠这里补（配合 `--warmup-days N` 回填历史天数）。
    幂等覆盖，代价只是一次小 PUT；探测不出结果（None）时也补一次，宁可多传。
    """
    key = timeline_object_key(version.article_id, version.level_code, row.voice)
    if not row.timeline:
        return
    if _object_state(storage, key) is True:
        return
    try:
        storage.put(
            key,
            json.dumps(row.timeline, ensure_ascii=False).encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )
        stats["timeline_uploaded"] += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("[alt] 补传时间轴失败 %s：%s", key, exc)


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
        "timeline_uploaded": 0,
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
        rows = {
            r.voice: r
            for r in db.scalars(
                select(AltVoiceAsset).where(AltVoiceAsset.version_id == version.id)
            )
        }
        _drop_stale(db, version, keep, storage, stats)
        for voice in voices:
            row = rows.get(voice)
            if row is not None:
                # 「有 DB 行」≠「资产还在」：桶被清过 / 换过桶时行在而音频没了，
                # 对外就是死链。只在**能确定对象不在**时才重做；探测不出结果
                # （None，三条通道全哑）就维持现状，否则一次网络抖动就会让
                # 20 份音色全部重新合成一遍。
                if _object_state(storage, row.file_path or "") is False:
                    log.warning("[alt] %s/%s 音频对象缺失，重新合成", version.level_code, voice)
                    await _one(db, version, voice, storage, stats)
                    continue
                stats["skipped"] += 1
                _ensure_timeline(storage, version, row, stats)
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
