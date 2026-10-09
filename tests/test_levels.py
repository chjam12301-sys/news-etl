"""等级规格与字数硬上限。

起因：A1 目标写的是 110 词，实测产出 116~230 词 —— 入门级比 B1 还长。
现在每段给区间 + 全局硬上限，并由 rewriter 强制裁剪。
"""
from __future__ import annotations

import pytest

from app.levels import ALL_LEVELS, LEVEL_BY_CODE
from app.rewriter import _trim_to_limit, _word_count


class TestLevelSpec:
    def test_a1_is_short(self):
        a1 = LEVEL_BY_CODE["en_a1"]
        assert a1.target_words <= 100
        assert a1.max_words <= 100

    def test_every_level_has_hard_cap_above_target(self):
        for spec in ALL_LEVELS:
            assert spec.target_words < spec.max_words <= spec.target_words * 1.25, spec.code

    def test_paragraph_range_can_reach_target(self):
        """段落区间必须能装下目标字数，否则 prompt 自相矛盾。"""
        for spec in ALL_LEVELS:
            lo = spec.para_min * spec.para_words[0]
            hi = spec.para_max * spec.para_words[1]
            assert lo <= spec.target_words <= hi, (
                f"{spec.code}: 目标 {spec.target_words} 不在 [{lo}, {hi}] 内"
            )

    def test_lengths_are_monotonic(self):
        for levels in (
            [LEVEL_BY_CODE[c] for c in ("en_a1", "en_a2", "en_b1", "en_b2", "en_c1")],
            [LEVEL_BY_CODE[c] for c in ("ja_n5", "ja_n4", "ja_n3", "ja_n2", "ja_n1")],
        ):
            targets = [s.target_words for s in levels]
            assert targets == sorted(targets), targets


class TestTrimToLimit:
    @pytest.mark.parametrize(
        "code,paras",
        [
            ("en_a1", ["This is one very long sentence that just keeps going and going "
                       "without any punctuation to break it up at all " * 5]),
            ("en_a1", [" ".join(f"word{i}" for i in range(30)) + "." for _ in range(4)]),
            ("ja_n5", ["これは長い文章です。" * 30]),
            ("ja_n1", ["これは長い文章です。" * 40 for _ in range(4)]),
        ],
    )
    def test_never_exceeds_hard_cap(self, code, paras):
        spec = LEVEL_BY_CODE[code]
        out, _zh = _trim_to_limit(list(paras), [], spec)
        total = sum(_word_count(p, spec.lang) for p in out)
        assert total <= spec.max_words, f"{code}: {total} > {spec.max_words}"
        assert out, "不能裁成空文"

    def test_short_enough_is_untouched(self):
        spec = LEVEL_BY_CODE["en_a1"]
        paras = ["Short one.", "Short two.", "Short three."]
        out, _ = _trim_to_limit(list(paras), [], spec)
        assert out == paras

    def test_chinese_paragraphs_stay_aligned(self):
        """中译必须与正文段落一一对应，裁剪时同步裁。"""
        spec = LEVEL_BY_CODE["en_a1"]
        paras = [" ".join(f"word{i}" for i in range(30)) + "." for _ in range(5)]
        zh = [f"第{i}段" for i in range(5)]
        out, out_zh = _trim_to_limit(paras, zh, spec)
        assert len(out) == len(out_zh)
