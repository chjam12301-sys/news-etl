"""词条字典的回归测试。

覆盖三件容易悄悄坏掉的事：
1. 变形词回退规则（这套规则同时存在于 app/dict.py、scripts/build_dict.py、
   workers/dict-api/src/index.mjs 三处，任何一处改动都要在这里对上）；
2. 富化只填空、不覆盖（LLM 已给的 pos 不能被词典值顶掉）；
3. 词性记法映射 —— 词库给 n./v.，内容里在用的是 noun/verb，混用会一眼看出不一致。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.dict import Dictionary, content_pos, enrich_vocab, get_dictionary, reset_dictionary

# 与文档里列出的样例一致
INDEX = {
    "trade": {"word": "trade", "phonetic": "/treɪd/", "pos": "n.",
              "en": "Buying and selling of goods.", "zh": "贸易"},
    "tax": {"word": "tax", "phonetic": "/tæks/", "pos": "n.",
            "en": "A compulsory contribution.", "zh": "税"},
    "house": {"word": "house", "phonetic": "/haʊs/", "pos": "n.",
              "en": "A building for living in.", "zh": "房子"},
    "agree": {"word": "agree", "phonetic": "/əˈɡriː/", "pos": "v.",
              "en": "Have the same opinion.", "zh": "同意"},
    "good": {"word": "good", "phonetic": "/ɡʊd/", "pos": "adj.",
             "en": "To be desired.", "zh": "好的"},
    "import": {"word": "import", "phonetic": "/ɪmˈpɔːt/", "pos": "v.",
               "en": "Bring goods in.", "zh": "进口"},
    "business": {"word": "business", "phonetic": "/ˈbɪznəs/", "pos": "n.",
                 "en": "Trade and commerce.", "zh": "生意"},
    "make": {"word": "make", "phonetic": "/meɪk/", "pos": "v.",
             "en": "Form by shaping.", "zh": "做"},
    "study": {"word": "study", "phonetic": "/ˈstʌdi/", "pos": "v.",
              "en": "Learn about a subject.", "zh": "研究"},
}


@pytest.fixture(autouse=True)
def _patched(monkeypatch):
    """把进程级单例换成测试索引，避免依赖仓库里那份 2 万词的索引。"""
    import app.dict as mod

    monkeypatch.setattr(mod, "_cached", Dictionary(INDEX), raising=False)
    monkeypatch.setattr(mod, "_loaded", True, raising=False)
    yield
    reset_dictionary()


# --------------------------------------------------------------------------- #
# 词形回退
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "surface,headword",
    [
        ("trade", "trade"),        # 精确
        ("Trade", "trade"),        # 大小写
        ("taxes", "tax"),          # es -> ""
        ("houses", "house"),       # es -> e
        ("businesses", "business"),
        ("agreed", "agree"),       # ed -> e
        ("imported", "import"),    # ed -> ""
        ("goods", "good"),         # s -> ""
        ("making", "make"),        # ing -> e
        ("studies", "study"),      # ies -> y
    ],
)
def test_lookup_falls_back_to_headword(surface, headword):
    hit = Dictionary(INDEX).lookup(surface)
    assert hit is not None, f"{surface} 应该能回退到 {headword}"
    assert hit["word"] == headword


def test_lookup_misses_cleanly():
    assert Dictionary(INDEX).lookup("quixotic") is None
    assert Dictionary(INDEX).lookup("") is None


# --------------------------------------------------------------------------- #
# 富化
# --------------------------------------------------------------------------- #
def test_enrich_fills_empty_fields_only():
    vocab = [{"word": "trade", "pos": "noun", "zh": "贸易", "note": "自定义",
              "phonetic": "", "en": ""}]
    out = enrich_vocab(vocab, "en")[0]
    assert out["phonetic"] == "/treɪd/"          # 空 -> 填
    assert out["en"] == "Buying and selling of goods."
    assert out["pos"] == "noun"                  # LLM 给了就保留，不被改成 n.
    assert out["zh"] == "贸易"                   # 中文释义不动
    assert out["note"] == "自定义"


def test_enrich_maps_dict_pos_style():
    out = enrich_vocab([{"word": "trade", "pos": "", "zh": "贸易"}], "en")[0]
    assert out["pos"] == "noun"                  # n. -> noun（向内容风格对齐）


def test_enrich_skips_japanese():
    vocab = [{"word": "利用規約", "pos": "noun", "zh": "使用条款"}]
    assert enrich_vocab(vocab, "ja") == vocab


def test_enrich_keeps_unknown_words_untouched():
    vocab = [{"word": "quixotic", "pos": "", "zh": "不切实际的"}]
    out = enrich_vocab(vocab, "en")[0]
    assert out == vocab[0]


def test_enrich_without_index_is_noop(monkeypatch):
    import app.dict as mod

    monkeypatch.setattr(mod, "_cached", None, raising=False)
    monkeypatch.setattr(mod, "_loaded", True, raising=False)
    vocab = [{"word": "trade", "pos": "", "zh": "贸易"}]
    assert enrich_vocab(vocab, "en") == vocab


# --------------------------------------------------------------------------- #
# 加载与词性映射
# --------------------------------------------------------------------------- #
def test_load_missing_file_returns_none(tmp_path: Path):
    assert Dictionary.load(tmp_path / "nope.json") is None


def test_load_broken_file_returns_none(tmp_path: Path):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    assert Dictionary.load(p) is None


def test_load_empty_index_returns_none(tmp_path: Path):
    p = tmp_path / "empty.json"
    p.write_text("{}", encoding="utf-8")
    assert Dictionary.load(p) is None


def test_load_roundtrip(tmp_path: Path):
    p = tmp_path / "idx.json"
    p.write_text(json.dumps(INDEX), encoding="utf-8")
    dic = Dictionary.load(p)
    assert dic is not None and len(dic) == len(INDEX)
    assert dic.lookup("trade")["zh"] == "贸易"


@pytest.mark.parametrize(
    "raw,expected",
    [("n.", "noun"), ("v.", "verb"), ("adj.", "adj"), ("adv.", "adv"),
     ("prep.", "prep"), ("conj.", "conj"),
     ("noun", "noun"), ("phrase", "phrase"), ("", ""), ("  ", "")],
)
def test_content_pos_mapping(raw, expected):
    assert content_pos(raw) == expected


def test_get_dictionary_uses_repo_index():
    """仓库里必须真的带上索引 —— 没有它，生成的词条就没有音标与英文释义。

    注意是直接 Dictionary.load()，不能走 get_dictionary()：本文件的 autouse
    fixture 已经把单例换成了 9 词的小索引，走单例只会读到那份桩数据。
    """
    p = Path(__file__).resolve().parent.parent / "data" / "dict" / "ecdict-subset.json"
    if not p.is_file():
        pytest.skip("索引尚未生成（先在 CI 跑一次「构建词库」workflow）")
    dic = Dictionary.load(p)
    assert dic is not None and len(dic) > 1000
    # 抽查几个高频词，确认音标、英文释义、词性都真的落在里面
    for word in ("trade", "economy", "artificial", "regulate"):
        hit = dic.lookup(word)
        assert hit is not None, f"{word} 应该收录在索引里"
        assert hit["phonetic"], f"{word} 缺音标"
        assert hit["en"], f"{word} 缺英文释义"
        assert hit["pos"], f"{word} 缺词性"
        assert len(hit["en"]) <= 121, f"{word} 的英文释义没被截到两行以内"
