"""等级体系定义：英语 5 级（CEFR A1–C1）+ 日语 5 级（JLPT N5–N1）。

指令文本统一用英文撰写（对 LLM 更稳定），实际产出语言由 code 决定。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LevelSpec:
    code: str
    lang: str  # en | ja
    label: str
    level: int  # 1..5
    instruction: str      # 交给 LLM 的改写指令
    target_words: int     # 目标词数
    sentence_hint: str    # 句子复杂度提示
    vocab_count: int = 8  # 词汇注释条数
    tags: tuple[str, ...] = field(default_factory=tuple)


EN_LEVELS: tuple[LevelSpec, ...] = (
    LevelSpec(
        code="en_a1",
        lang="en",
        label="A1 入门",
        level=1,
        instruction=(
            "Rewrite for absolute beginners (CEFR A1). Use only the ~500 most "
            "frequent English words. One idea per sentence, 6-12 words per sentence. "
            "Mostly simple present or simple past. No idioms, no phrasal verbs, no "
            "relative clauses."
        ),
        target_words=110,
        sentence_hint="6-12 word sentences",
        vocab_count=8,
    ),
    LevelSpec(
        code="en_a2",
        lang="en",
        label="A2 初级",
        level=2,
        instruction=(
            "Rewrite for elementary learners (CEFR A2). Everyday vocabulary, simple "
            "past / present perfect / future forms, sentences up to 14 words. Keep "
            "concrete everyday nouns and common verbs."
        ),
        target_words=150,
        sentence_hint="up to 14 word sentences",
        vocab_count=8,
    ),
    LevelSpec(
        code="en_b1",
        lang="en",
        label="B1 中级",
        level=3,
        instruction=(
            "Rewrite for intermediate learners (CEFR B1). Mix tenses (past perfect, "
            "first conditional), common phrasal verbs, and connectors such as because, "
            "although, however. 14-20 words per sentence."
        ),
        target_words=200,
        sentence_hint="two-clause sentences with connectors",
        vocab_count=10,
    ),
    LevelSpec(
        code="en_b2",
        lang="en",
        label="B2 中高级",
        level=4,
        instruction=(
            "Rewrite for upper-intermediate learners (CEFR B2). Abstract nouns, "
            "passive voice, reported speech, and natural collocations. Sentences up to "
            "25 words, two or three clauses each."
        ),
        target_words=250,
        sentence_hint="multi-clause sentences",
        vocab_count=12,
    ),
    LevelSpec(
        code="en_c1",
        lang="en",
        label="C1 高级",
        level=5,
        instruction=(
            "Rewrite for advanced learners (CEFR C1). Keep a near-native journalistic "
            "register: idiomatic collocations, nuanced hedging, formal or technical "
            "vocabulary, inversion and rhetorical structures. Do not over-simplify."
        ),
        target_words=300,
        sentence_hint="dense sophisticated sentences",
        vocab_count=14,
    ),
)

JA_LEVELS: tuple[LevelSpec, ...] = (
    LevelSpec(
        code="ja_n5",
        lang="ja",
        label="N5 初級",
        level=1,
        instruction=(
            "Write in plain simple Japanese (JLPT N5). "
            "Use である / ます style, only grade-1 kanji plus hiragana, no keigo. "
            "Very short sentences of roughly 5-10 morae."
        ),
        target_words=120,
        sentence_hint="very short sentences",
        vocab_count=8,
    ),
    LevelSpec(
        code="ja_n4",
        lang="ja",
        label="N4 初級",
        level=2,
        instruction=(
            "Write in everyday Japanese (JLPT N4). Basic て-form and plain form, "
            "grade-school kanji only, everyday particles. Sentences of roughly "
            "10-18 morae."
        ),
        target_words=160,
        sentence_hint="short everyday sentences",
        vocab_count=10,
    ),
    LevelSpec(
        code="ja_n3",
        lang="ja",
        label="N3 中級",
        level=3,
        instruction=(
            "Write in natural everyday Japanese (JLPT N3). Express opinions, reasons "
            "and conditions with ので / から / たら / けど. Tone like a general-interest "
            "column."
        ),
        target_words=220,
        sentence_hint="short coherent paragraphs",
        vocab_count=12,
    ),
    LevelSpec(
        code="ja_n2",
        lang="ja",
        label="N2 中高級",
        level=4,
        instruction=(
            "Write in formal written Japanese (JLPT N2) suitable for a business or "
            "general-news digest. Full written である style, keigo where appropriate, "
            "compound sentences with subordinate clauses."
        ),
        target_words=280,
        sentence_hint="formal written style",
        vocab_count=14,
    ),
    LevelSpec(
        code="ja_n1",
        lang="ja",
        label="N1 上級",
        level=5,
        instruction=(
            "Write in advanced Japanese (JLPT N1) close to a native newspaper "
            "article: idioms, four-character compounds, abstract nouns, double "
            "negation, and formal written style. Preserve nuance; do not flatten it."
        ),
        target_words=340,
        sentence_hint="newspaper-grade prose",
        vocab_count=16,
    ),
)

ALL_LEVELS: tuple[LevelSpec, ...] = EN_LEVELS + JA_LEVELS
LEVEL_BY_CODE: dict[str, LevelSpec] = {lv.code: lv for lv in ALL_LEVELS}


def levels_for(lang: str) -> tuple[LevelSpec, ...]:
    return EN_LEVELS if lang == "en" else JA_LEVELS