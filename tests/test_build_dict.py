"""词库构建脚本的回归测试。

这些函数决定了两万个词条最终长什么样，而它们的输入（ECDICT 全量 CSV）
有 62.9MB、在 CI 里跑，本机不复现。所以规则必须在这里钉死，
否则错一次要等一轮 CI（约 6 分钟）才发现。

实测背景（决定了下面几条设计）：
- ECDICT 的 `pos` 列 **20000 条全为空**，词性其实藏在释义前缀里（`n. 贸易`）。
- 英文释义长度 P50=48 / P90=96 / P95=118 / P99=166 / 最长 382 字符。
"""
from __future__ import annotations

import pytest

from scripts.build_dict import (
    EN_MAX_CHARS,
    clip,
    normalize_pos,
    parse_csv,
    shard_of,
    split_tag,
)


# --------------------------------------------------------------------------- #
# CSV 解析
# --------------------------------------------------------------------------- #
def test_parse_csv_handles_quotes_commas_and_newlines():
    text = 'a,b\n"x,1","line1\nline2"\n"say ""hi""",z\n'
    rows = list(parse_csv(text))
    assert rows[0] == ["a", "b"]
    assert rows[1] == ["x,1", "line1\nline2"]
    assert rows[2] == ['say "hi"', "z"]


def test_parse_csv_handles_crlf():
    rows = list(parse_csv("a,b\r\n1,2\r\n"))
    assert rows == [["a", "b"], ["1", "2"]]


# --------------------------------------------------------------------------- #
# 词性
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("n:46/v:54", "v."),      # 语料占比：取高的那个
        ("v:54/n:46", "v."),
        ("n", "n."),
        ("vt", "v."),             # 及物动词归一成 v.
        ("vi", "v."),
        ("a", "adj."),            # ECDICT 用 a. 表示形容词
        ("s", "adj."),            # s. = adjective satellite
        ("ad", "adv."),
        ("", ""),
        ("   ", ""),
        ("zzz", ""),              # 不认识的不乱猜
    ],
)
def test_normalize_pos(raw, expected):
    assert normalize_pos(raw) == expected


@pytest.mark.parametrize(
    "text,tag,body",
    [
        ("n. 贸易, 商业, 交易", "n.", "贸易, 商业, 交易"),
        ("vt. 管理, 控制", "v.", "管理, 控制"),
        ("a. 人造的, 假的", "adj.", "人造的, 假的"),
        ("n. the commercial exchange of goods", "n.", "the commercial exchange of goods"),
        ("ad. 向前", "adv.", "向前"),
        ("prep. 在...周围", "prep.", "在...周围"),
    ],
)
def test_split_tag(text, tag, body):
    assert split_tag(text) == (tag, body)


def test_split_tag_leaves_plain_text_alone():
    assert split_tag("plain text") == ("", "plain text")
    # 标记不认识时立刻停手，别把正文当词性吃掉
    assert split_tag("no. 1 thing") == ("", "no. 1 thing")


def test_split_tag_does_not_eat_capitalised_initials():
    """`A. Lincoln` 是大写人名首字母，不是形容词 a.。"""
    assert split_tag("A. Lincoln was president") == ("", "A. Lincoln was president")


def test_split_tag_handles_double_marker():
    """`v. i.` 这种双标记只吃第一级，剩下的留给正文。"""
    assert split_tag("v. i. See Thee.") == ("v.", "i. See Thee.")


# --------------------------------------------------------------------------- #
# 英文释义截断
# --------------------------------------------------------------------------- #
def test_clip_keeps_short_text():
    assert clip("short one") == "short one"
    assert clip("x" * EN_MAX_CHARS) == "x" * EN_MAX_CHARS


def test_clip_cuts_at_word_boundary_and_marks_truncation():
    long = "Buying and selling of goods and services on a market in exchange for money today"
    out = clip(long, limit=40)
    assert len(out) <= 40
    assert out.endswith("…")
    # 不能把单词切成两半：去掉省略号后必须是原串的前缀，且以完整单词结尾
    stem = out[:-1]
    assert long.startswith(stem)
    assert not stem.endswith(" ")
    assert long[len(stem)] == " "


def test_clip_handles_no_space():
    out = clip("y" * 200, limit=30)
    assert len(out) <= 30


def test_clip_default_limit_is_the_documented_one():
    assert EN_MAX_CHARS == 120


# --------------------------------------------------------------------------- #
# 分片键（必须与 workers/dict-api/src/index.mjs 的 shardOf 逐字符一致）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "word,shard",
    [
        ("trade", "tr"), ("a", "a_"), ("about", "ab"), ("the", "th"),
        ("work", "wo"), ("don't", "do"), ("e-mail", "e_"), ("o'clock", "o_"),
        ("x", "x_"), ("3d", "3d"), ("it", "it"), ("by_pass", "by"),
        ("état", "_t"), ("ünïcode", "_n"), ("", "__"),
    ],
)
def test_shard_of(word, shard):
    assert shard_of(word) == shard


def test_every_shard_name_is_two_chars():
    """Worker 靠「文件名恰好两字符」来区分 shard 与早期的一词一对象布局。"""
    for w in ("a", "about", "zzz", "don't", "état", "3d"):
        assert len(shard_of(w)) == 2
