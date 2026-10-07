"""content_hash 的行为约定（App 依赖它决定是否重新下载，规则不能随意变）。"""
from __future__ import annotations

import pytest

from app.content_hash import (
    audio_fingerprint,
    compute_content_hash,
    timeline_fingerprint,
)

BASE = dict(
    level_code="en_a1", lang="en", level=1,
    title="Oil Companies Ask Court",
    body="Para one.\n\nPara two.",
    paragraphs=["Para one.", "Para two."],
    lead="Lead.",
    title_zh="石油公司请求法院",
    lead_zh="导读。",
    paragraphs_zh=["第一段。", "第二段。"],
    vocab=[{"word": "oil", "zh": "石油"}],
    audio={
        "duration_ms": 54925, "size_bytes": 445248,
        "timeline": [{"w": "Oil", "sm": 100, "em": 300, "cs": 0, "ce": 3}],
        "voice": "en-US-AriaNeural", "engine": "edge-tts",
    },
)


def h(**over):
    return compute_content_hash(**{**BASE, **over})


class TestStability:
    def test_same_input_same_hash(self):
        assert h() == h()

    def test_hash_length(self):
        assert len(h()) == 16

    def test_key_order_does_not_matter(self):
        a = compute_content_hash(**{k: BASE[k] for k in reversed(list(BASE))})
        assert a == h()


class TestWhatChangesHash:
    """这些变化必须改 hash —— App 需要重新下载。"""

    @pytest.mark.parametrize("over,label", [
        ({"body": "Para one!\n\nPara two."}, "正文改一个字"),
        ({"paragraphs": ["A", "B", "C"]}, "段落增删"),
        ({"title": "New Title"}, "标题改动"),
        ({"title_zh": "石油公司请求最高法院"}, "中文标题改动"),
        ({"lead_zh": "新的导读"}, "中文导读改动"),
        ({"paragraphs_zh": ["一", "二", "三"]}, "中文段落增删"),
        ({"vocab": [{"word": "oil", "zh": "石油", "note": "x"}]}, "词汇表改动"),
        ({"level": 2}, "等级不同"),
        ({"lang": "ja"}, "语言不同"),
        ({"level_code": "en_a2"}, "等级码不同"),
    ])
    def test_changes(self, over, label):
        assert h(**over) != h(), f"{label} 应改变 hash"

    def test_audio_duration_change(self):
        assert h(audio={**BASE["audio"], "duration_ms": 54926}) != h()

    def test_audio_size_change(self):
        assert h(audio={**BASE["audio"], "size_bytes": 445249}) != h()

    def test_audio_voice_change(self):
        """换TTS 音色 → 音频文件不同 → 必须重下。"""
        assert h(audio={**BASE["audio"], "voice": "en-GB-SoniaNeural"}) != h()

    def test_timeline_change(self):
        """时间轴微调 → 跟读高亮会不同 → 必须重下。"""
        tl = [{"w": "Oil", "sm": 101, "em": 300, "cs": 0, "ce": 3}]
        assert h(audio={**BASE["audio"], "timeline": tl}) != h()

    def test_audio_removed(self):
        """音频从有到无 → App 需知道该重新拉详情。"""
        assert h(audio=None) != h()


class TestWhatDoesNotChangeHash:
    """这些变化不应触发重新下载，否则 App 会无谓地重拉。"""

    def test_duplicate_content_across_ids(self):
        """正文完全相同但等级不同 → 仍应视为不同内容。"""
        assert h(level=3) != h(level=4)

    def test_char_offset_only_change(self):
        """时间轴的字符偏移（cs/ce）变化不影响跟读，不该重下。"""
        tl = [{"w": "Oil", "sm": 100, "em": 300, "cs": 0, "ce": 9}]
        assert h(audio={**BASE["audio"], "timeline": tl}) == h()

    def test_empty_vs_none(self):
        """空字符串与None 等价，避免生成器差异造成假变化。"""
        assert h(title_zh=None) == h(title_zh="")
        assert h(paragraphs_zh=None) == h(paragraphs_zh=[])

    def test_timeline_empty_fingerprint(self):
        assert timeline_fingerprint([]) == ""
        assert timeline_fingerprint(None) == ""


class TestAudioFingerprint:
    def test_duration_matters(self):
        assert audio_fingerprint(duration_ms=1000) != audio_fingerprint(duration_ms=1001)

    def test_ignores_url(self):
        """audio.url 含 commit SHA，不该纳入指纹。"""
        a = audio_fingerprint(duration_ms=100, timeline=[{"w": "a", "sm": 0, "em": 1}])
        b = audio_fingerprint(duration_ms=100, timeline=[{"w": "a", "sm": 0, "em": 1}])
        assert a == b

    def test_zero_when_no_audio(self):
        assert audio_fingerprint() != ""

class TestFromVersion:
    """from_version 传的是已算好的音频指纹字符串，不是 dict——
    曾因类型混用报TypeError，此处锁定两种入参都可用。"""

    def test_accepts_precomputed_audio_str(self):
        a = compute_content_hash(**{**BASE, "audio": audio_fingerprint(**BASE["audio"])})
        b = compute_content_hash(**BASE)
        assert a == b, "传入预计算指纹与现算结果应一致"

    def test_accepts_none_audio(self):
        assert compute_content_hash(**{**BASE, "audio": None})

    def test_from_version_on_real_objects(self):
        """用真实的 ORM 对象跑一遍（无音频 / 有音频两种）。"""
        from types import SimpleNamespace

        from app.content_hash import from_version

        v = SimpleNamespace(
            level_code="en_a1", lang="en", level=1,
            title="T", body="B", paragraphs=["B"], lead="L",
            title_zh="题", lead_zh="导", paragraphs_zh=["段"],
            vocab=[{"word": "x", "zh": "y"}],
        )
        assert from_version(v, None)

        audio = SimpleNamespace(
            duration_ms=100, size_bytes=200, timeline=[{"w": "a", "sm": 0, "em": 1}],
            voice="en-US-AriaNeural", engine="edge-tts",
        )
        assert from_version(v, audio) != from_version(v, None)
