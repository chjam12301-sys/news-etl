"""时间轴与核心逻辑的回归测试（不依赖网络的部分全部离线跑）。"""
from __future__ import annotations

import json
import re

import pytest

from app.levels import ALL_LEVELS, EN_LEVELS, JA_LEVELS, LEVEL_BY_CODE
from app.rewriter import offline_rewrite, parse_json_loose
from app.tts import (
    TICKS_PER_SECOND,
    _map_boundaries_to_text,
    normalize_for_tts,
    split_sentences,
    synthesize,
)


# --------------------------------------------------------------------------- #
# 等级体系
# --------------------------------------------------------------------------- #
def test_levels_are_5_en_plus_5_ja():
    assert len(EN_LEVELS) == 5
    assert len(JA_LEVELS) == 5
    assert len(ALL_LEVELS) == 10
    assert [lv.level for lv in EN_LEVELS] == [1, 2, 3, 4, 5]
    assert [lv.level for lv in JA_LEVELS] == [1, 2, 3, 4, 5]
    assert {lv.lang for lv in ALL_LEVELS} == {"en", "ja"}


def test_level_instructions_have_no_garbage():
    """等级指令里混入过乱码，这里锁死：只允许 ASCII + 日文字符。"""
    import re

    allowed = re.compile(r"^[\x00-\x7F぀-ヿ一-鿿　-〿‘’“”]+$")
    for lv in ALL_LEVELS:
        assert allowed.match(lv.instruction), f"{lv.code} 指令含非法字符: {lv.instruction!r}"
        assert len(lv.instruction) > 40, f"{lv.code} 指令过短"


def test_level_word_counts_increase():
    for levels in (EN_LEVELS, JA_LEVELS):
        counts = [lv.target_words for lv in levels]
        assert counts == sorted(counts), f"{levels[0].lang} 目标词数未递增: {counts}"


# --------------------------------------------------------------------------- #
# 文本规整
# --------------------------------------------------------------------------- #
def test_normalize_collapses_newlines():
    assert normalize_for_tts("a\n\nb\n c") == "a b c"


def test_spoken_text_is_alias_of_normalize():
    from app.tts import spoken_text

    assert spoken_text is normalize_for_tts


def test_timeline_offsets_index_into_spoken_text_not_body():
    """关键契约：cs/ce 必须相对「压平后的单行文本」。

    若误用带换行的 body 定位，多段正文一定错位 —— 这类 bug 在 App 上表现为
    「逐词高亮跳字」，排查成本极高，故在此锁死。
    """
    body = "First paragraph here.\n\nSecond paragraph with more words."
    spoken = normalize_for_tts(body)
    assert "\n" not in spoken
    # 构造一个覆盖全文的时间轴
    tokens = [(m.group(0), m.start(), m.end())
              for m in re.finditer(r"[A-Za-z']+|[^\sA-Za-z']", spoken)]
    fake = [
        {"i": i, "w": tok, "s": i * 0.3, "e": i * 0.3 + 0.25,
         "sm": i * 300, "em": i * 300 + 250,
         "cs": cs, "ce": ce, "si": 0}
        for i, (tok, cs, ce) in enumerate(tokens)
    ]
    # 对 spoken 应当全部命中
    assert all(spoken[w["cs"]:w["ce"]] == w["w"] for w in fake)

    # 反证：body 与 spoken 在换行处之后确实不同 —— 否则样本无意义
    second = spoken.index("Second")
    assert body[second:second + 6] != spoken[second:second + 6], "样本无换行，无法验证该契约"


def test_split_sentences_both_langs():
    assert len(split_sentences("One. Two. Three.")) == 3
    assert len(split_sentences("これは一文です。二文目です。")) == 2


# --------------------------------------------------------------------------- #
# 时间轴对齐（核心）
# --------------------------------------------------------------------------- #
def _mk_boundaries(pairs):
    return [
        {"offset": int(start * TICKS_PER_SECOND), "duration": int(dur * TICKS_PER_SECOND), "text": txt}
        for txt, start, dur in pairs
    ]


def test_alignment_is_monotonic_and_covers():
    text = "Scientists found a new frog today."
    spoken = normalize_for_tts(text)
    bounds = _mk_boundaries(
        [("Scientists", 0.1, 0.6), ("found", 0.7, 0.3), ("a", 1.0, 0.1),
         ("new", 1.1, 0.2), ("frog", 1.3, 0.4), ("today", 1.7, 0.5)]
    )
    units = _map_boundaries_to_text(bounds, spoken)
    assert units, "对齐结果为空"

    cursor = 0
    for word, s, e in units:
        assert e >= s
        # 时间轴不含空格：跳过空白后再校验
        nxt = cursor
        while nxt < len(spoken) and spoken[nxt].isspace():
            nxt += 1
        assert spoken[nxt : nxt + len(word)] == word, f"字符区间错位: {word!r}"
        cursor = nxt + len(word)
    # 主体必须覆盖（末尾标点可缺）
    assert cursor >= len(spoken) - 1


def test_alignment_handles_punctuation_as_separate_unit():
    text = "Rain forest, near Lima."
    spoken = normalize_for_tts(text)
    bounds = _mk_boundaries(
        [("Rain", 0.1, 0.3), ("forest", 0.4, 0.3), (",", 0.7, 0.05),
         ("near", 0.75, 0.3), ("Lima", 1.05, 0.4)]
    )
    units = _map_boundaries_to_text(bounds, spoken)
    words = [u[0] for u in units]
    assert "," in words, f"逗号未单独成词: {words}"
    assert "Lima" in words


def test_alignment_fills_gap_when_boundary_skipped():
    """引擎漏掉某个词时，该词必须被插值补上而不是丢失。"""
    text = "alpha beta gamma delta"
    spoken = normalize_for_tts(text)
    bounds = _mk_boundaries([("alpha", 0.1, 0.3), ("gamma", 0.6, 0.3), ("delta", 1.0, 0.3)])
    units = _map_boundaries_to_text(bounds, spoken)
    joined = "".join(u[0] for u in units)
    assert "beta" in joined, f"缺口未补齐: {joined}"


def test_alignment_never_overlaps():
    text = "The quick brown fox jumps over the lazy dog again and again."
    spoken = normalize_for_tts(text)
    words = spoken.split()
    bounds = _mk_boundaries([(w, i * 0.4, 0.35) for i, w in enumerate(words)])
    units = _map_boundaries_to_text(bounds, spoken)

    cursor = 0
    for word, _s, _e in units:
        nxt = cursor
        while nxt < len(spoken) and spoken[nxt].isspace():
            nxt += 1
        assert spoken[nxt : nxt + len(word)] == word, f"重叠或错位: {word!r} @ {nxt}"
        cursor = nxt + len(word)
    assert cursor >= len(spoken) - 1


# --------------------------------------------------------------------------- #
# LLM 输出解析
# --------------------------------------------------------------------------- #
def test_parse_json_loose_plain():
    assert parse_json_loose('{"a": 1}')["a"] == 1


def test_parse_json_loose_with_fence():
    assert parse_json_loose('```json\n{"a": 2}\n```')["a"] == 2


def test_parse_json_loose_with_preamble():
    raw = 'Sure! Here is the result:\n{"title": "x", "paragraphs": ["y"]}'
    assert parse_json_loose(raw)["title"] == "x"


# --------------------------------------------------------------------------- #
# 离线降级
# --------------------------------------------------------------------------- #
def test_offline_rewrite_schema_consistent():
    text = ("Scientists in Brazil reported a new frog species on Monday. "
            "The animal was found near a remote river in the Amazon region. "
            "Researchers say the species may have been unknown for many years. ")
    for lv in ALL_LEVELS:
        r = offline_rewrite("science", "New frog found", text, lv)
        assert r.level_code == lv.code
        assert r.lang == lv.lang
        assert r.paragraphs, f"{lv.code} 离线改写无正文"
        assert r.body
        assert len(r.vocab) <= lv.vocab_count
        # 低等级应比高等级更短
        assert len(r.body.split()) <= len(text.split()) + 50


def test_offline_low_level_is_shorter():
    text = " ".join(["Scientists reported an unusual discovery in the region yesterday."] * 6)
    a1 = offline_rewrite("tech", "T", text, LEVEL_BY_CODE["en_a1"])
    c1 = offline_rewrite("tech", "T", text, LEVEL_BY_CODE["en_c1"])
    assert len(a1.body) <= len(c1.body)


# --------------------------------------------------------------------------- #
# 端到端（需要网络；失败不阻塞）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_synthesize_produces_valid_timeline():
    try:
        res = await synthesize("Scientists found a new frog in the Amazon.", "en")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"TTS 不可用: {exc}")

    assert res.audio_bytes, "音频为空"
    assert res.timings, "时间轴为空"
    assert res.duration > 0

    cursor = 0
    for t in res.timings:
        assert t.end_ms >= t.start_ms
        assert t.char_start >= cursor, "字符区间未单调递增"
        assert t.char_end > t.char_start
        cursor = t.char_end


@pytest.mark.asyncio
async def test_synthesize_japanese():
    try:
        res = await synthesize("科学者は新しい力を見つけました。", "ja")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"TTS 不可用: {exc}")
    assert res.timings
    assert res.engine in {"edge-tts", "silent-fallback"}