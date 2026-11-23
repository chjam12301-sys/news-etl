import json

import pytest

from app.levels import LEVEL_BY_CODE
from app.rewriter import rewrite_article, _word_count


class FakeLLM:
    name = "fake"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.prompts = []

    async def complete(self, system, user, *, max_tokens=2400):
        self.prompts.append(user)
        return json.dumps(next(self.replies), ensure_ascii=False)


def draft(paragraphs):
    return {"title": "News", "lead": "Summary.", "paragraphs": paragraphs,
            "paragraphs_zh": ["STALE"] * len(paragraphs), "vocab": []}


def translated(paragraphs):
    return {"title_zh": "新闻", "lead_zh": "摘要。", "paragraphs_zh": paragraphs}


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["en_a1", "en_b2", "ja_n5", "ja_n4"])
async def test_overlimit_rewrites_without_trimming(code):
    spec = LEVEL_BY_CODE[code]
    oversized = (["word " * 200] * 3 if spec.lang == "en"
                 else ["日本語です。" * 100] * 3)
    revised = (["A complete short report."] * 3 if spec.lang == "en"
               else ["今日はニュースがあります。"] * 3)
    llm = FakeLLM([draft(oversized), draft(revised),
                   translated(["完整译文。"] * 3)])
    result = await rewrite_article("topic", "title", "source", spec, llm)
    assert not result.degraded
    assert result.paragraphs == revised
    assert result.paragraphs_zh == ["完整译文。"] * 3
    assert len(llm.prompts) == 3
    assert "REVISION REQUIRED" in llm.prompts[1]
    assert "STALE" not in result.paragraphs_zh
    assert sum(_word_count(p, spec.lang) for p in result.paragraphs) <= spec.max_words


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["en_b2", "ja_n4"])
async def test_valid_article_translates_final_text(code):
    spec = LEVEL_BY_CODE[code]
    paragraphs = (["The market grew by 123%."] * 3 if spec.lang == "en"
                  else ["市場は123%成長しました。"] * 3)
    llm = FakeLLM([draft(paragraphs), translated(["市场增长了123%。"] * 3)])
    result = await rewrite_article("topic", "title", "source", spec, llm)
    assert result.paragraphs == paragraphs
    assert result.paragraphs_zh == ["市场增长了123%。"] * 3
    assert "123%" in llm.prompts[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["en_a1", "ja_n5"])
async def test_repeated_overlimit_is_degraded(code):
    spec = LEVEL_BY_CODE[code]
    oversized = (["word " * 200] * 3 if spec.lang == "en"
                 else ["日本語です。" * 100] * 3)
    llm = FakeLLM([draft(oversized)] * 3)
    result = await rewrite_article("topic", "title", "source", spec, llm)
    assert result.degraded
    assert not result.paragraphs_zh
    assert len(llm.prompts) == 3


@pytest.mark.asyncio
async def test_translation_mismatch_is_degraded():
    spec = LEVEL_BY_CODE["en_a1"]
    llm = FakeLLM([draft(["A complete sentence."] * 3)]
                  + [translated(["只有一段。"])] * 3)
    result = await rewrite_article("topic", "title", "source", spec, llm)
    assert result.degraded
    assert len(llm.prompts) == 4
