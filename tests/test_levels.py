"""等级规格与字数硬上限。

起因：A1 目标写的是 110 词，实测产出 116~230 词 —— 入门级比 B1 还长。
现在每段给区间 + 全局硬上限；rewriter 只验证长度，不再裁剪。
"""
from __future__ import annotations

import pytest

from app.levels import ALL_LEVELS, LEVEL_BY_CODE
from app.rewriter import _valid_length


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


class TestLengthValidation:
    @pytest.mark.parametrize(
        "code,paras",
        [
            ("en_a1", ["This sentence has far too many words for a beginner. " * 30] * 3),
            ("en_a1", [" ".join(f"word{i}" for i in range(30)) + "." for _ in range(4)]),
            ("ja_n5", ["これは長い文章です。" * 30] * 3),
            ("ja_n1", ["これは長い文章です。" * 40 for _ in range(4)]),
        ],
    )
    def test_overlong_is_rejected_not_truncated(self, code, paras):
        spec = LEVEL_BY_CODE[code]
        original = list(paras)
        assert not _valid_length(paras, spec)
        assert paras == original, "validation must not alter text"

    @pytest.mark.parametrize("code,paras", [
        ("en_a1", ["Short one.", "Short two.", "Short three."]),
        ("ja_n5", ["今日は晴れです。", "学校に行きます。", "本を読みます。"]),
    ])
    def test_valid_text_is_accepted_unchanged(self, code, paras):
        assert _valid_length(paras, LEVEL_BY_CODE[code])
        assert paras

    def test_too_few_or_too_many_paragraphs_rejected(self):
        spec = LEVEL_BY_CODE["en_a1"]
        assert not _valid_length(["Short one."], spec)
        assert not _valid_length(["Short."] * (spec.para_max + 1), spec)
        assert not _valid_length([], spec)
