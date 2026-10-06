"""TTS + 逐词时间轴。

edge-tts 免费、无需鉴权，且流式返回 WordBoundary 事件（offset/duration 单位为
100ns），正好满足 App 端「跟读高亮 / 单词点击定位」的需求。

同时保留一个本地静音兜底（按词长估算时长），保证离线/CI 环境也能产出结构完整数据。
"""
from __future__ import annotations

import asyncio
import json
import logging
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
    """去掉换行并压成单行 —— edge-tts 的 boundary 只给词，段落信息需我们自己补。"""
    return re.sub(r"\s*\n\s*", " ", text).strip()


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


async def _synth_edge(text: str, voice: str) -> TTSResult:
    import edge_tts

    # edge-tts>=6.1 把 boundary 提到 Communicate.__init__，默认只回句边界，
    # 必须显式要 WordBoundary 才有逐词时间戳。
    try:
        communicate = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    except TypeError:
        # 兼容旧版（boundary 在 stream 上）
        communicate = edge_tts.Communicate(text, voice)

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

    if not boundaries:
        raise RuntimeError("未收到任何 WordBoundary 事件")

    spoken = normalize_for_tts(text)
    alignments = _map_boundaries_to_text(boundaries, spoken)
    timings = _build_char_index(spoken, alignments)
    duration = timings[-1].end if timings else 0.0
    return TTSResult(
        audio_bytes=bytes(audio),
        duration=round(duration, 3),
        timings=timings,
        engine="edge-tts",
        voice=voice,
    )


def _synth_silent(text: str, voice: str, lang: str) -> TTSResult:
    """兜底：生成极短静音 WAV + 按词长估算的时间轴，保证 schema 一致。"""
    import struct

    spoken = normalize_for_tts(text)
    tokens = _tokenize(spoken, lang)
    rate = 0.22 if lang == "ja" else 0.28
    timings: list[WordTiming] = []
    t = 0.0
    for tok, cs, ce in tokens:
        est = max(0.14, min(0.9, len(tok) * rate))
        timings.append(
            WordTiming(
                index=len(timings), text=tok,
                start=round(t, 3), end=round(t + est, 3),
                start_ms=int(t * 1000), end_ms=int((t + est) * 1000),
                char_start=cs, char_end=ce,
            )
        )
        t += est
    duration = round(t, 3)
    # 8kHz 单声道 8bit WAV
    rate_hz, n = 8000, max(1, int(duration * rate_hz))
    header = b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate_hz, rate_hz, 1, 8) + b"data" + struct.pack("<I", n)
    return TTSResult(audio_bytes=header + bytes(n), duration=duration, timings=timings, engine="silent-fallback", voice=voice)


async def synthesize(text: str, lang: str, voice: str | None = None) -> TTSResult:
    """合成音频 + 逐词时间轴。edge-tts 失败自动降级为静音占位。"""
    voice = voice or _pick_voice(lang)
    if not text.strip():
        return _synth_silent("", voice, lang)
    if not settings.tts_enabled:
        return _synth_silent(text, voice, lang)
    try:
        res = await asyncio.wait_for(_synth_edge(text, voice), timeout=180)
        if not res.audio_bytes:
            raise RuntimeError("空音频")
        return res
    except Exception as exc:  # noqa: BLE001
        log.warning("[tts] edge-tts 失败(%s)，使用静音兜底: %s", exc, voice)
        return _synth_silent(text, voice, lang)


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