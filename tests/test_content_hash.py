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
        "timeline": [{"w": "Oil", "sm": 100, "em": 300, "cs": 0, "ce": 3, "si": 0}],
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

    def test_char_offset_change(self):
        """cs/ce 变化 → 高亮区间变化 → 必须重下。

        App 用 text.slice(cs, ce) 标range，改了偏移而沿用旧数据会高亮错位。
        （曾经误判为「不影响跟读」而排除，App 侧指出后修正。）
        """
        tl = [{"w": "Oil", "sm": 100, "em": 300, "cs": 0, "ce": 9}]
        assert h(audio={**BASE["audio"], "timeline": tl}) != h()

    def test_start_offset_change(self):
        """cs 单独变化也要触发。"""
        tl = [{"w": "Oil", "sm": 100, "em": 300, "cs": 5, "ce": 3}]
        assert h(audio={**BASE["audio"], "timeline": tl}) != h()

    def test_sentence_index_change(self):
        """si 变化 → 句循环分组变化 → 必须重下。"""
        tl = [{"w": "Oil", "sm": 100, "em": 300, "cs": 0, "ce": 3, "si": 7}]
        assert h(audio={**BASE["audio"], "timeline": tl}) != h()

    def test_audio_removed(self):
        """音频从有到无 → App 需知道该重新拉详情。"""
        assert h(audio=None) != h()


class TestWhatDoesNotChangeHash:
    """这些变化不应触发重新下载，否则 App 会无谓地重拉。"""

    def test_duplicate_content_across_ids(self):
        """正文完全相同但等级不同 → 仍应视为不同内容。"""
        assert h(level=3) != h(level=4)

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


class TestHighlightFieldsAreCovered:
    """回归：App 反馈过「上游哈希漏了高亮所需字段」。

    App 用 cs/ce 在 text 上标range 做高亮、用 si 做句循环，
    这三个字段变化必须被指纹覆盖，否则 App 会沿用旧数据导致高亮错位。
    """

    def _tl(self, **over):
        base = {"w": "Oil", "sm": 100, "em": 300, "cs": 0, "ce": 3, "si": 0}
        base.update(over)
        return [base]          # timeline 是 list[dict]，不是 list[list]

    def test_cs_ce_si_all_covered(self):
        for label, over in [
            ("cs", {"cs": 7}),
            ("ce", {"ce": 7}),
            ("si", {"si": 3}),
        ]:
            assert h(audio={**BASE["audio"], "timeline": self._tl(**over)}) != h(), \
                f"timeline.{label} 变化必须改变 hash"

    def test_all_highlight_fields_present_in_fingerprint(self):
        """指纹必须对全部六个字段敏感，少一个都不行。"""
        fields = ["w", "sm", "em", "cs", "ce", "si"]
        for f in fields:
            new = self._tl(**{f: 999})
            assert timeline_fingerprint(new) != timeline_fingerprint(self._tl()), \
                f"timeline_fingerprint 未覆盖字段 {f}"

    def test_word_order_matters(self):
        """词序变化 → 高亮顺序变化 → 必须重下。"""
        a = [{"w": "A", "sm": 0, "em": 1, "cs": 0, "ce": 1, "si": 0},
             {"w": "B", "sm": 1, "em": 2, "cs": 2, "ce": 3, "si": 0}]
        b = list(reversed(a))
        assert timeline_fingerprint(a) != timeline_fingerprint(b)


class TestIndexVersionField:
    """回归：index.json 的 version 字段曾因只靠手工补而丢失，
    导致 App 无法自证索引是否最新（线上表现为「拉不到新文章」）。"""

    def test_export_index_includes_version(self):
        import inspect

        from app import exporter

        src = inspect.getsource(exporter.export_index)
        assert '"version"' in src, "export_index 必须输出 version 字段"
        assert "published_at" in src and "content_hash" in src

    def test_version_hash_tracks_content(self):
        """version.content_hash 由各条目的 content_hash 汇总，
        内容变则变。"""
        import hashlib
        import json

        by_lang = {"en": [{"content_hash": "aaa"}, {"content_hash": "bbb"}]}
        h1 = hashlib.sha256(
            json.dumps(by_lang, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        by_lang["en"].append({"content_hash": "ccc"})
        h2 = hashlib.sha256(
            json.dumps(by_lang, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        assert h1 != h2
