"""TTS + 逐词时间轴。

edge-tts 免费、无需鉴权，且流式返回 WordBoundary 事件（offset/duration 单位为
100ns），正好满足 App 端「跟读高亮 / 单词点击定位」的需求。

合成失败明确抛错，不能把静音占位当作成功音频。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

TICKS_PER_SECOND = 10_000_000  # edge-tts 的 offset/duration 是 100ns


@dataclass(slots=True)
class WordTiming:
    index: int
    text: str
    start: float          # 秒，保留 3 位
    end: float
    start_ms: int
    end_ms: int
    char_start: int       # 在全文纯文本中的字符区间，供 App 高亮定位
    char_end: int
    sentence_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "i": self.index,
            "w": self.text,
            "s": self.start,
            "e": self.end,
            "sm": self.start_ms,
            "em": self.end_ms,
            "cs": self.char_start,
            "ce": self.char_end,
            "si": self.sentence_index,
        }


@dataclass(slots=True)
class TTSResult:
    audio_bytes: bytes
    duration: float
    timings: list[WordTiming] = field(default_factory=list)
    engine: str = "edge-tts"
    voice: str = ""

    def timeline_dict(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self.timings]

    def boundaries_dict(self) -> list[dict[str, Any]]:
        """原始 boundary（100ns 制），便于后续换算/校准。"""
        return [
            {"offset": int(t.start * TICKS_PER_SECOND), "duration": int((t.end - t.start) * TICKS_PER_SECOND), "text": t.text}
            for t in self.timings
        ]


# --------------------------------------------------------------------------- #
# 文本规整：送给 TTS 的文本必须与时间轴对齐的文本完全一致
# --------------------------------------------------------------------------- #
_KANA_ONLY = re.compile(r"^[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf\s、。「」『』（）ー・]+$")


def split_sentences(text: str) -> list[str]:
    """按语言切句：英文按 .!?，日文按 。！？。"""
    text = text.strip()
    if not text:
        return []
    if re.search(r"[぀-ヿ一-鿿]", text):
        parts = re.split(r"(?<=[。！？!?])\s*", text)
    else:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", text)
    return [p.strip() for p in parts if p and p.strip()]


def normalize_for_tts(text: str) -> str:
    """去掉换行并压成单行 —— edge-tts 的 boundary 只给词，段落信息需我们自己补。

    ⚠️ 时间轴里的 cs/ce 字符偏移是相对这个「压平后」的文本，
    App 端做逐词高亮时必须用同一份文本（详情 JSON 的 `text` 字段）来 slice，
    直接用带换行的 body 会错位。
    """
    return re.sub(r"\s*\n\s*", " ", text).strip()


# 语义化别名，便于调用方理解「这个文本才是时间轴的坐标系」
spoken_text = normalize_for_tts


def _tokenize(text: str, lang: str) -> list[tuple[str, int, int]]:
    """把文本切成 (token, char_start, char_end)，日文不按空格切。"""
    out: list[tuple[str, int, int]] = []
    for m in re.finditer(r"[A-Za-z]+(?:['’-][A-Za-z]+)*|[0-9]+(?:[.,][0-9]+)*|[぀-ヿ一-鿿]+|[^\sA-Za-z0-9぀-ヿ一-鿿]", text):
        out.append((m.group(0), m.start(), m.end()))
    return out


# --------------------------------------------------------------------------- #
# edge-tts
# --------------------------------------------------------------------------- #
def _pick_voice(lang: str) -> str:
    return settings.tts_voice_ja if lang == "ja" else settings.tts_voice_en


def _map_boundaries_to_text(
    boundaries: list[dict[str, Any]], spoken: str
) -> list[tuple[str, float, float]]:
    """把 edge-tts 的 boundary 序列对齐到我们自己送进去的文本。

    策略（按可靠性排序）：
      1. 对每个 boundary，在原文中做**精确子串定位**，消费掉对应字符区间；
         定位成功即得到完全可信的时间片段。
      2. 引擎改写文本导致定位失败时（如数字读作 twenty twenty four）直接跳过，
         不猜测、不错位。
      3. 所有 boundary 处理完后，原文里剩下的「未覆盖缺口」（通常是标点、逗号、
         被跳过的词）按字符长度在相邻已知时间点之间**等比插值**。

    由此保证：char 区间单调递增、不重叠、并 100% 覆盖全文 —— 这三点是 App 端
    逐词高亮不跳字的前提。
    """
    # 1) 精确子串定位
    spans: list[tuple[int, int, float, float]] = []  # (cs, ce, t_start, t_end)
    cursor = 0
    for b in boundaries:
        btext = (b.get("text") or "").strip()
        if not btext:
            continue
        idx = spoken.find(btext, cursor)
        if idx == -1:
            idx = spoken.find(btext)
            if idx == -1 or idx < cursor:
                continue
        start = b.get("offset", 0) / TICKS_PER_SECOND
        dur = b.get("duration", 0) / TICKS_PER_SECOND
        spans.append((idx, idx + len(btext), start, start + max(dur, 0.04)))
        cursor = idx + len(btext)

    if not spans:
        return []

    # 2) 补齐首尾与内部缺口
    first_start = spans[0][0]
    if first_start > 0:
        spans.insert(0, (0, first_start, 0.0, max(0.0, spans[0][2] * 0.0)))
    last_end = spans[-1][1]
    if last_end < len(spoken):
        tail_start = spans[-1][3]
        spans.append((last_end, len(spoken), tail_start, tail_start + 0.25))

    filled: list[tuple[int, int, float, float]] = [spans[0]]
    for span in spans[1:]:
        prev = filled[-1]
        gap_cs, gap_ce = prev[1], span[0]
        if gap_ce > gap_cs:
            filled.extend(
                _interpolate_gap(spoken, gap_cs, gap_ce, prev[3], span[2])
            )
        filled.append(span)

    # 3) 在每个字符区间内按 token 切词；无 boundary 的区间整块当作一个单元
    units: list[tuple[str, float, float]] = []
    for cs, ce, t0, t1 in filled:
        seg = spoken[cs:ce]
        if not seg.strip():
            continue
        toks = _tokenize(seg, "en")
        if not toks:
            units.append((seg, t0, t1))
            continue
        total_chars = sum(len(t[0]) for t in toks) or 1
        span_secs = max(t1 - t0, 0.01)
        acc = 0.0
        for i, (tok, s_cs, s_ce) in enumerate(toks):
            share = len(tok) / total_chars * span_secs
            t_start = t0 + acc
            acc += share
            t_end = t0 + acc if i < len(toks) - 1 else t1
            units.append((tok, t_start, max(t_end, t_start + 0.02)))

    return units


def _interpolate_gap(
    spoken: str, gap_start: int, gap_end: int, t0: float, t1: float
) -> list[tuple[int, int, float, float]]:
    """把一个未覆盖的字符区间按 token 字符数比例插值到 [t0, t1]。"""
    seg = spoken[gap_start:gap_end]
    toks = _tokenize(seg, "en")
    if not toks:
        return [(gap_start, gap_end, t0, max(t1, t0 + 0.08))]

    total = sum(len(t[0]) for t in toks) or 1
    span_secs = max(t1 - t0, 0.05)
    out: list[tuple[int, int, float, float]] = []
    acc = 0.0
    for i, (tok, s_cs, _s_ce) in enumerate(toks):
        share = len(tok) / total * span_secs
        s = t0 + acc
        acc += share
        e = t0 + acc if i < len(toks) - 1 else t1
        out.append((gap_start + s_cs, gap_start + s_cs + len(tok), s, max(e, s + 0.02)))
    return out


def _build_char_index(spoken: str, alignments: list[tuple[str, float, float]]) -> list[WordTiming]:
    """_dedupe_and_align 已把文本定位好，这里按顺序填 char 区间并标记句子边界。"""
    timings: list[WordTiming] = []
    cursor = 0
    sent_i = 0
    for word, start, end in alignments:
        idx = spoken.find(word, cursor)
        if idx == -1:
            idx = cursor
        ce = idx + len(word)
        timings.append(
            WordTiming(
                index=len(timings),
                text=word,
                start=round(start, 3),
                end=round(end, 3),
                start_ms=int(start * 1000),
                end_ms=int(end * 1000),
                char_start=idx,
                char_end=ce,
                sentence_index=sent_i,
            )
        )
        cursor = ce
        if word.endswith((".", "!", "?", "。", "！", "？")):
            sent_i += 1
    return timings


# 微软会轮换 readaloud 接受的 Edge 版本；edge-tts 写死的常量过期后会 403。
# 这里在运行时覆盖为当前稳定版（默认 155.0.4283.45，2026-10-08 发布），
# 可用环境变量 EDGE_TTS_CHROMIUM_VERSION 调整，方便下次微软再轮换时只改一处。
_EDGE_TTS_VERSION_PATCHED = False


def _patch_edge_tts_version() -> None:
    global _EDGE_TTS_VERSION_PATCHED
    if _EDGE_TTS_VERSION_PATCHED:
        return
    import edge_tts
    import edge_tts.constants as C

    ver = os.environ.get("EDGE_TTS_CHROMIUM_VERSION", "155.0.4283.45")
    C.CHROMIUM_FULL_VERSION = ver
    C.CHROMIUM_MAJOR_VERSION = ver.split(".", 1)[0]
    C.SEC_MS_GEC_VERSION = f"1-{ver}"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        f" (KHTML, like Gecko) Chrome/{C.CHROMIUM_MAJOR_VERSION}.0.0.0 Safari/537.36"
        f" Edg/{C.CHROMIUM_MAJOR_VERSION}.0.0.0"
    )
    C.BASE_HEADERS["User-Agent"] = ua
    for d in (C.WSS_HEADERS, C.VOICE_HEADERS):
        d.update(C.BASE_HEADERS)
    C.VOICE_HEADERS["Sec-CH-UA"] = (
        f'" Not;A Brand";v="99", "Microsoft Edge";v="{C.CHROMIUM_MAJOR_VERSION}",'
        f' "Chromium";v="{C.CHROMIUM_MAJOR_VERSION}"'
    )
    edge_tts.communicate.SEC_MS_GEC_VERSION = C.SEC_MS_GEC_VERSION
    _EDGE_TTS_VERSION_PATCHED = True


async def _synth_edge(text: str, voice: str) -> TTSResult:
    """合成音频 + 逐词时间轴。

    默认走本地 edge-tts；若设置 TTS_RELAY_URL，则改为调用自建的
    Cloudflare Worker 中继（微软调用从 Cloudflare 非中国边缘发出，同时绕开
    本机/CI 的 403 地域墙与数据中心 IP 墙）。两种路径返回的 boundaries 结构一致，
    下游对齐逻辑 (_map_boundaries_to_text) 无需改动。
    """
    relay_url = os.environ.get("TTS_RELAY_URL") or None
    if relay_url:
        audio_bytes, boundaries = await _synth_via_relay(relay_url, text, voice)
    else:
        audio_bytes, boundaries = await _synth_via_edge_tts(text, voice)

    if not boundaries:
        raise RuntimeError("未收到任何 WordBoundary 事件")

    spoken = normalize_for_tts(text)
    alignments = _map_boundaries_to_text(boundaries, spoken)
    timings = _build_char_index(spoken, alignments)
    duration = timings[-1].end if timings else 0.0
    return TTSResult(
        audio_bytes=audio_bytes,
        duration=round(duration, 3),
        timings=timings,
        engine="edge-tts-relay" if relay_url else "edge-tts",
        voice=voice,
    )


async def _synth_via_edge_tts(text: str, voice: str) -> tuple[bytes, list[dict[str, Any]]]:
    import edge_tts

    _patch_edge_tts_version()

    # 仅当显式设置 EDGE_TTS_PROXY 时才走代理；默认直连（住宅 IP 即可）。
    # 不要自动读全局 HTTPS_PROXY：本机可能挂着未启动/非住宅的全局代理，
    # 一旦误用会导致连代理就被拒（ClientProxyConnectionError），反而掩盖真实问题。
    proxy = os.environ.get("EDGE_TTS_PROXY") or None
    # edge-tts>=6.1 把 boundary 提到 Communicate.__init__，默认只回句边界，
    # 必须显式要 WordBoundary 才有逐词时间戳。
    try:
        communicate = edge_tts.Communicate(text, voice, boundary="WordBoundary", proxy=proxy)
    except TypeError:
        # 兼容旧版（boundary 在 stream 上）
        communicate = edge_tts.Communicate(text, voice, proxy=proxy)

    audio = bytearray()
    boundaries: list[dict[str, Any]] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            boundaries.append(
                {
                    "offset": chunk.get("offset", 0),
                    "duration": chunk.get("duration", 0),
                    "text": chunk.get("text", ""),
                }
            )
        elif chunk["type"] == "SentenceBoundary" and not boundaries:
            boundaries.append(
                {
                    "offset": chunk.get("offset", 0),
                    "duration": chunk.get("duration", 0),
                    "text": chunk.get("text", ""),
                }
            )

    if not audio:
        raise RuntimeError("TTS returned empty audio")
    return bytes(audio), boundaries


async def _synth_via_relay(
    relay_url: str, text: str, voice: str
) -> tuple[bytes, list[dict[str, Any]]]:
    """调用自建 Cloudflare Worker 中继，返回 (audio_bytes, boundaries)。

    中继接口：POST {relay_url}  body={text, voice}
    响应：{audio_base64, boundaries:[{offset,duration,text}], format}
    boundaries 坐标系与 edge-tts 一致（100ns tick，已含跨分片补偿）。
    """
    import base64
    import json
    import urllib.error
    import urllib.request

    key = os.environ.get("TTS_RELAY_KEY") or ""

    def _post() -> tuple[bytes, list[dict[str, Any]]]:
        payload = json.dumps({"text": text, "voice": voice}).encode("utf-8")
        req = urllib.request.Request(
            relay_url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "x-api-key": key},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                pass
            raise RuntimeError(f"relay HTTP {e.code}: {detail}") from e
        data = json.loads(raw)
        if "error" in data:
            raise RuntimeError(f"relay error: {data['error']}")
        audio_b64 = data.get("audio_base64") or ""
        audio_bytes = base64.b64decode(audio_b64)
        boundaries = data.get("boundaries") or []
        if not audio_bytes:
            raise RuntimeError("relay returned empty audio")
        cleaned = [
            {
                "offset": int(b.get("offset", 0)),
                "duration": int(b.get("duration", 0)),
                "text": b.get("text", ""),
            }
            for b in boundaries
        ]
        return audio_bytes, cleaned

    return await asyncio.to_thread(_post)


# --------------------------------------------------------------------------- #
# Kokoro OSS TTS（CI 内自托管，免费不绑卡，替代被微软 403 封锁的 edge-tts）
# --------------------------------------------------------------------------- #
_KOKORO_PIPELINES: dict[str, Any] = {}  # lang_code -> KPipeline（进程内复用，避免重复加载模型）


def _kokoro_voice(lang: str) -> str:
    """Kokoro 模式下的 voice id，由 env TTS_VOICE_EN/JA 驱动，与 edge-tts 的
    微软 voice 解耦。默认 en=af_heart / ja=jf_alpha（均为 Kokoro 官方音色）。"""
    if lang == "ja":
        return os.environ.get("TTS_VOICE_JA") or "jf_alpha"
    return os.environ.get("TTS_VOICE_EN") or "af_heart"


def _get_kokoro_pipeline(lang_code: str):
    if lang_code not in _KOKORO_PIPELINES:
        from kokoro import KPipeline

        _KOKORO_PIPELINES[lang_code] = KPipeline(lang_code=lang_code)
    return _KOKORO_PIPELINES[lang_code]


async def _synth_kokoro(text: str, lang: str) -> TTSResult:
    """Kokoro OSS 合成：返回 WAV 音频 + 词级 boundaries，复用下游 _map_boundaries_to_text。

    Kokoro 的 KPipeline 原生暴露 result.tokens（misaki MToken），每个 token 带
    .text（词级原文）、.start_ts/.end_ts（秒，相对当前 chunk）、.whitespace（词后空白）。
    这里累加每个 chunk 的音频时长得到全局偏移，转成与 edge-tts 一致的
    {offset(100ns), duration(100ns), text} boundaries，App 端时间轴 schema 零改动。

    空音频 / 无 token 时间戳必须 raise（no-silent-tts），不得产出静默占位。
    """
    lang_code = "j" if lang == "ja" else "a"
    voice = _kokoro_voice(lang)
    pipeline = await asyncio.to_thread(_get_kokoro_pipeline, lang_code)
    spoken = normalize_for_tts(text)

    def _run() -> tuple[bytes, list[dict[str, Any]]]:
        import io

        import numpy as np
        import soundfile as sf

        chunks_audio: list[np.ndarray] = []
        boundaries: list[dict[str, Any]] = []
        offset_sec = 0.0
        for result in pipeline(spoken, voice=voice):
            audio = getattr(result, "audio", None)
            toks = getattr(result, "tokens", None)
            if audio is None and isinstance(result, (tuple, list)):
                audio = result[2] if len(result) > 2 else None
            if audio is not None and len(audio):
                chunks_audio.append(audio)
                if toks:
                    for t in toks:
                        txt = getattr(t, "text", "") or ""
                        st = getattr(t, "start_ts", None)
                        et = getattr(t, "end_ts", None)
                        if txt and st is not None and et is not None:
                            boundaries.append(
                                {
                                    "offset": int((offset_sec + float(st)) * TICKS_PER_SECOND),
                                    "duration": max(1, int((float(et) - float(st)) * TICKS_PER_SECOND)),
                                    "text": txt,
                                }
                            )
                offset_sec += len(audio) / 24000.0
        if not chunks_audio:
            raise RuntimeError("Kokoro 未返回音频（模型推理失败或文本为空）")
        full = np.concatenate(chunks_audio) if len(chunks_audio) > 1 else chunks_audio[0]
        buf = io.BytesIO()
        sf.write(buf, full, 24000, format="WAV")
        return buf.getvalue(), boundaries

    audio_bytes, boundaries = await asyncio.to_thread(_run)
    if not audio_bytes:
        raise RuntimeError("Kokoro 返回空音频")
    if not boundaries:
        raise RuntimeError("Kokoro 未返回 token 时间戳（请确认 kokoro>=0.7 且 WLT 已启用）")

    alignments = _map_boundaries_to_text(boundaries, spoken)
    if not alignments:
        raise RuntimeError("Kokoro 时间戳无法对齐到文本")
    timings = _build_char_index(spoken, alignments)
    duration = timings[-1].end if timings else 0.0
    return TTSResult(
        audio_bytes=audio_bytes,
        duration=round(duration, 3),
        timings=timings,
        engine="kokoro",
        voice=voice,
    )


async def synthesize(text: str, lang: str, voice: str | None = None) -> TTSResult:
    """合成音频和时间轴；失败时由调用方保留缺失状态。

    引擎由 env TTS_ENGINE 选择：
      - kokoro（自 v2.8 起默认）：CI 内自托管 OSS，免费不绑卡，替代被微软 403 封锁的 edge-tts
      - edge-tts（回退）：本地 edge-tts 或自建 Cloudflare Worker 中继
    """
    if not text.strip():
        raise ValueError("TTS text is empty")
    if not settings.tts_enabled:
        raise RuntimeError("TTS is disabled")
    engine = os.environ.get("TTS_ENGINE", "edge-tts").lower()
    try:
        if engine == "kokoro":
            res = await asyncio.wait_for(_synth_kokoro(text, lang), timeout=300)
        else:
            res = await asyncio.wait_for(_synth_edge(text, voice or _pick_voice(lang)), timeout=180)
    except Exception as exc:
        status = getattr(exc, "status", None) or getattr(exc, "code", None)
        suffix = f" [HTTP {status}]" if status else ""
        raise RuntimeError(f"TTS synthesis failed ({type(exc).__name__}){suffix}") from exc
    if not res.audio_bytes:
        raise RuntimeError("TTS returned empty audio")
    return res


# --------------------------------------------------------------------------- #
# SRT / VTT 导出（给 App 字幕轨，可选）
# --------------------------------------------------------------------------- #
def to_srt(timings: list[WordTiming]) -> str:
    def ts(ms: int) -> str:
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, msec = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"

    # 每 6 个词合成一条字幕
    out: list[str] = []
    idx = 1
    for i in range(0, len(timings), 6):
        chunk = timings[i : i + 6]
        if not chunk:
            continue
        out.append(
            f"{idx}\n{ts(chunk[0].start_ms)} --> {ts(chunk[-1].end_ms)}\n"
            + " ".join(t.text for t in chunk)
            + "\n"
        )
        idx += 1
    return "\n".join(out)


def timeline_json(timings: list[WordTiming]) -> str:
    return json.dumps([t.to_dict() for t in timings], ensure_ascii=False)