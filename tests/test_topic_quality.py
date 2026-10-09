"""选题质量：去重 + 打分。用例全部取自线上真实出过的问题标题。"""
from __future__ import annotations

import pytest

from app.dedup import TopicIndex, build_index, content_tokens, is_duplicate, strip_publisher
from app.selector import score_title, select


# --------------------------------------------------------------------------- #
# 真实重复稿（线上 article_id 标注）
# --------------------------------------------------------------------------- #
DUP_PAIRS = [
    # 3 / 5 / 8 石油公司诉气候案，Ars Technica 隔天改了标题
    ("Oil companies ask top court to stop climate cases",
     "Oil Companies Ask Supreme Court to Stop Climate Cases"),
    ("Oil Companies Ask Supreme Court to Stop Climate Cases",
     "Oil Companies Ask Supreme Court to Stop Climate Lawsuits"),
    # 4 / 7 德国前间谍局长被捕，BBC 两条
    ("Former German spy chief arrested for spying",
     "Germany arrests former spy chief August Hanning"),
    # 6 / 9 Nature 的"科学需要公平规则"
    ("Science needs fair rules in a world of conflict",
     "Science Needs Fair Rules in Conflict"),
    # 23 / 27 / 30 / 32 北极冻土，写了四遍
    ("Company uses wood to save frozen Arctic soil",
     "Wood blankets slow melting Arctic soil"),
    ("Wood blankets slow melting Arctic soil",
     "Wood Blankets Help Save Frozen Arctic Soil"),
    # 26 / 29 / 31 Nvidia 机器人安全
    ("Nvidia builds safety system for robots and self-driving cars",
     "Nvidia Bets on Safe Robots and Self-Driving Cars"),
]

NOT_DUP_PAIRS = [
    ("OpenAI agents tried to hack Wikipedia tools",
     "Microsoft Shows New AI Laptop and Windows Changes"),
    ("Drones sink two ships near NATO countries",
     "US and Lebanon protect a Syrian general, BBC says"),
    ("Harry Kane: 125 games for England, but no big trophy yet",
     "Arteta becomes the highest-paid manager in the Premier League"),
    ("Plague Is Still Here: Cases in Africa and US",
     "US woman Christa Pike survives failed execution"),
    ("Company uses wood to save frozen Arctic soil",
     "Oscar-winning actress Eva Marie Saint dies at 102"),
]


class TestDedup:
    @pytest.mark.parametrize("a,b", DUP_PAIRS)
    def test_real_duplicates_are_caught(self, a, b):
        assert is_duplicate(a, b), f"应判为重复但没挡住：\n  {a}\n  {b}"

    @pytest.mark.parametrize("a,b", NOT_DUP_PAIRS)
    def test_distinct_stories_pass(self, a, b):
        assert not is_duplicate(a, b), f"误杀：\n  {a}\n  {b}"

    def test_symmetric(self):
        for a, b in DUP_PAIRS + NOT_DUP_PAIRS:
            assert is_duplicate(a, b) == is_duplicate(b, a)

    def test_index_dedups_across_days_and_sources(self):
        idx = build_index(["Wood blankets slow melting Arctic soil"])
        assert idx.is_duplicate("Company uses wood to save frozen Arctic soil")
        assert idx.find("Company uses wood to save frozen Arctic soil") is not None
        assert not idx.is_duplicate("Eva Marie Saint dies at 102")

    def test_index_is_case_insensitive(self):
        idx = build_index(["NVIDIA builds safety system for robots"])
        assert idx.is_duplicate("Nvidia Builds Safety System For Robots")

    def test_empty_titles_never_dup(self):
        assert not is_duplicate("", "Anything at all here")
        idx = TopicIndex()
        assert not idx.is_duplicate("")

    def test_stopwords_removed(self):
        assert "the" not in content_tokens("The oil and the gas and the court")
        assert content_tokens("Arctic soil melts") == {"arctic", "soil", "melts"}

    def test_strip_publisher_suffix(self):
        assert strip_publisher("Apple launches new iPhone - MacRumors") == "Apple launches new iPhone"
        # 正文源的标题不能被误伤（这里尾部不是发布方）
        assert strip_publisher("Oil firms sue - Supreme Court") == "Oil firms sue - Supreme Court"

    def test_publisher_suffix_only_when_asked(self):
        t = "Amazon is phasing out Fire Tablets - The Verge"
        assert "verge" in content_tokens(t)
        assert "verge" not in content_tokens(t, publisher_suffix=True, publisher="The Verge")


# --------------------------------------------------------------------------- #
# 选题打分
# --------------------------------------------------------------------------- #
REJECTED = [
    ("How Nature Index Tables Are Made", "nature"),            # 科研计量
    ("Scientists Fix a Small Mistake in a Science Paper", "nature"),
    ("Science Needs Fair Rules in Conflict", "nature"),        # 倡议，没有事件
    ("Why the Arctic matters", "phys_org"),                    # 解释型
    ("Correction: the earlier figure was wrong", "nature"),
]

ACCEPTED = [
    ("Germany arrests former spy chief August Hanning", "bbc_world"),
    ("Spanish woman, 87, dies after eviction", "bbc_world"),
    ("Oscar-winning actress Eva Marie Saint dies at 102", "bbc_culture"),
    ("Oil Companies Ask Supreme Court to Stop Climate Cases", "arstechnica"),
    ("Drones sink two ships near NATO countries", "arstechnica"),
]


class TestSelector:
    @pytest.mark.parametrize("title,source", REJECTED)
    def test_boring_academic_titles_rejected(self, title, source):
        score, reasons, rejected = score_title(title, source)
        assert rejected or score < 0.35, f"{title} 不该入选（得分 {score}）"

    @pytest.mark.parametrize("title,source", ACCEPTED)
    def test_real_news_accepted(self, title, source):
        score, _reasons, rejected = score_title(title, source)
        assert not rejected and score >= 0.35, f"{title} 应入选（得分 {score}）"

    def test_mainstream_source_beats_academic(self):
        """同一件事，BBC 的写法要比 Nature 的高。"""
        good, _, _ = score_title("Amazon cuts 14,000 jobs as AI spending rises", "bbc_business")
        weak, _, _ = score_title("Amazon cuts 14,000 jobs as AI spending rises", "nature")
        assert good > weak

    def test_heat_raises_score(self):
        base, _, _ = score_title("Microsoft unveils new AI laptop", "bbc_tech", heat=0)
        hot, reasons, _ = score_title("Microsoft unveils new AI laptop", "bbc_tech", heat=5)
        assert hot > base
        assert any("外媒同报" in r for r in reasons)

    def test_heat_is_capped(self):
        s6, _, _ = score_title("Microsoft unveils new AI laptop", "bbc_tech", heat=6)
        s99, _, _ = score_title("Microsoft unveils new AI laptop", "bbc_tech", heat=99)
        assert s6 == s99

    def test_empty_title_rejected(self):
        assert score_title("", "bbc_world")[2] == "空标题"


class _Raw:
    def __init__(self, title, source):
        self.title = title
        self.source = source
        self.source_url = f"https://example.com/{abs(hash(title))}"
        self.topic = "science"
        self.summary = "x" * 500
        self.body = ""
        self.image_url = None
        self.published_at = None
        self.fingerprint = str(abs(hash(title)))
        self.heat = 0


class TestSelect:
    def test_picks_best_and_drops_academic(self):
        cands = [
            _Raw("How Nature Index Tables Are Made", "nature"),
            _Raw("Spain floods kill 200 as rescue crews search", "bbc_world"),
        ]
        picked, scored = select(cands, 1)
        assert len(picked) == 1
        assert picked[0].title.startswith("Spain floods")

    def test_all_bad_falls_back_to_best(self):
        """一条都不出比出一条平庸的更糟 —— 空一天 App 就没东西。"""
        cands = [_Raw("How Nature Index Tables Are Made", "nature")]
        picked, _ = select(cands, 1)
        assert len(picked) == 1

    def test_fallback_prefers_unrejected_over_rejected(self):
        """兜底不能把「已判出局」的稿子捞回来。

        "How Nature Index Tables Are Made" 分数恒为 0，若只按分数排序会排到
        低分但可用的稿子前面 —— 实测兜底选中的正是它。
        """
        cands = [
            _Raw("How Nature Index Tables Are Made", "nature"),      # 出局，0.00
            _Raw("Arctic ice shrinks again this winter", "nature"),  # 未出局，低分
        ]
        picked, _ = select(cands, 1)
        assert picked[0].title.startswith("Arctic ice")

    def test_respects_per_topic(self):
        cands = [
            _Raw("Spain floods kill 200 as rescue crews search", "bbc_world"),
            _Raw("Japan quake shakes Tokyo, 12 injured", "bbc_world"),
            _Raw("France bans smoking on beaches", "bbc_world"),
        ]
        picked, _ = select(cands, 2)
        assert len(picked) == 2
