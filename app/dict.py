"""词条字典：ECDICT 高频子集的加载、查询与词表富化。

## 它解决什么

LLM 改写时只产出 `word / pos / zh / note` —— **没有音标，也没有英文释义**。
这两样是词典数据，不是改写能编出来的，必须外挂一份离线词典：

- 音标：App 词汇卡在单词下方显示 `/treɪd/`
- 英文释义：中文主释义下面那一行，与中文是**同一含义的另一种语言**，
  不是补充说明

数据由 `scripts/build_dict.py` 从 ECDICT（MIT，可商用）构建后提交到
`data/dict/ecdict-subset.json`，覆盖按词频排的前 2 万词。

## 为什么不用在线词典 API

`dictionaryapi.dev` 免费无 key，但实测连发几次就 522，且数据是 CC BY-SA 3.0、
商用前要确认署名义务。ECDICT 是 MIT + 全离线，构建期查一次即可。

## 设计取舍

- **只富化英文等级**。日语等级的 `word` 是汉字/假名，查英汉词典没有意义。
- **只填空，不覆盖**。LLM 已经给了 `pos` 就保留，避免把 "noun" 悄悄改成 "n."。
- **变形词回退是启发式的**（`taxes→tax`、`agreed→agree`）。权威做法是加载
  ECDICT 的 `lemma.en.txt`，但那是 2.2 MB 的额外依赖；先上启发式，命中率不
  够再换。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config import BASE_DIR, settings

log = logging.getLogger(__name__)

DEFAULT_INDEX = BASE_DIR / "data" / "dict" / "ecdict-subset.json"

# 变形词回退：按「先长后短」的顺序试，命中即停。
# 顺序很重要 —— "houses" 必须先试 es→e（house）再试 s→""（house）之外的歧义。
_SUFFIX_RULES: tuple[tuple[str, str], ...] = (
    ("ies", "y"),   # studies → study
    ("es", "e"),    # houses → house
    ("es", ""),     # taxes → tax / boxes → box
    ("s", ""),      # goods → good
    ("ing", "e"),   # making → make
    ("ing", ""),    # working → work
    ("ed", "e"),    # agreed → agree
    ("ed", ""),     # imported → import
    ("d", ""),      # raised → raise
)

# 只对纯英文词条做查表；日文、数字、短语里的日文一律跳过
_ASCII_WORD = re.compile(r"^[A-Za-z][A-Za-z'’\-]*$")

# ECDICT 用标准词典记法（n. / v. / adj.），但线上已有内容的词性来自 LLM，
# 是「不带点的短标签」（noun / verb / adj / phrase，实测 998 条）。两种混在
# 同一张词表里会一眼看出不一致，所以只有**从词库补进来的**那部分做一次映射，
# 向已有风格对齐 —— 不改动任何已经发布过的词条。
# 词库接口本身仍返回标准记法（n.），那是词典该有的样子。
_POS_TO_CONTENT = {
    "n.": "noun", "v.": "verb", "adj.": "adj", "adv.": "adv",
    "prep.": "prep", "conj.": "conj", "pron.": "pron", "num.": "num",
    "art.": "art", "int.": "int", "aux.": "aux", "abbr.": "abbr",
}


def content_pos(raw: str) -> str:
    """把词库里的标准词性记法映射成内容里在用的短标签风格。"""
    key = (raw or "").strip()
    return _POS_TO_CONTENT.get(key, key)


class Dictionary:
    """ECDICT 子集的本地查表。索引常驻内存（2 万词约 2–3 MB）。"""

    def __init__(self, index: dict[str, dict]) -> None:
        self._index = index

    def __len__(self) -> int:
        return len(self._index)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_INDEX) -> "Dictionary | None":
        p = Path(path)
        if not p.is_file():
            log.warning("[dict] 没有词库索引 %s —— 词条将缺少音标与英文释义", p)
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("[dict] 索引解析失败 %s: %s", p, exc)
            return None
        if not isinstance(data, dict) or not data:
            log.warning("[dict] 索引为空 %s", p)
            return None
        log.info("[dict] 载入词库 %d 词（%s）", len(data), p.name)
        return cls(data)

    # ------------------------------------------------------------------ #
    def lookup(self, word: str) -> dict | None:
        """查词。先精确、再小写、最后走变形词回退。"""
        w = (word or "").strip()
        if not w:
            return None
        hit = self._index.get(w) or self._index.get(w.lower())
        if hit:
            return hit

        low = w.lower()
        for suffix, repl in _SUFFIX_RULES:
            if not low.endswith(suffix):
                continue
            stem = low[: -len(suffix)] + repl
            if len(stem) < 3:
                continue
            hit = self._index.get(stem)
            if hit:
                return hit
        return None


# --------------------------------------------------------------------------- #
# 进程级单例：一次流水线里 10 个等级共用同一份索引，不要每个等级重读
# --------------------------------------------------------------------------- #
_cached: Dictionary | None = None
_loaded = False


def get_dictionary() -> Dictionary | None:
    global _cached, _loaded
    if not _loaded:
        _cached = Dictionary.load()
        _loaded = True
    return _cached


def reset_dictionary() -> None:
    """仅供测试用。"""
    global _cached, _loaded
    _cached = None
    _loaded = False


def enrich_vocab(vocab: list[dict], lang: str) -> list[dict]:
    """给词表补 `phonetic` / `en`（以及缺省时的 `pos`）。

    只填空不覆盖：LLM 已经给出的字段保持原样。
    索引缺失或查不到时原样返回 —— App 端对空值已有保护，不会渲染空行。
    """
    if not vocab:
        return vocab
    if lang != "en":
        return vocab

    dic = get_dictionary()
    if dic is None:
        return vocab

    out: list[dict] = []
    hits = 0
    for item in vocab:
        word = str(item.get("word") or "").strip()
        if not word or not _ASCII_WORD.match(word):
            out.append(item)
            continue
        hit = dic.lookup(word)
        if not hit:
            out.append(item)
            continue
        hits += 1
        merged = dict(item)
        if not str(merged.get("phonetic") or "").strip() and hit.get("phonetic"):
            merged["phonetic"] = hit["phonetic"]
        if not str(merged.get("en") or "").strip() and hit.get("en"):
            merged["en"] = hit["en"]
        if not str(merged.get("pos") or "").strip() and hit.get("pos"):
            merged["pos"] = content_pos(hit["pos"])
        out.append(merged)

    log.info("[dict] %s 词表富化：%d/%d 命中", lang, hits, len(vocab))
    return out


# --------------------------------------------------------------------------- #
# 词条对象存储（给 scripts/build_dict.py 上传用）
# --------------------------------------------------------------------------- #
def get_dict_storage():
    """词条对象的存储后端。与音频共用同一组 R2 密钥，只是 key 前缀不同。"""
    from .storage import R2Storage

    bucket = getattr(settings, "r2_bucket", "") or ""
    key_id = getattr(settings, "r2_access_key_id", "") or ""
    secret = getattr(settings, "r2_secret_access_key", "") or ""
    endpoint = getattr(settings, "r2_endpoint", "") or ""
    if not (bucket and key_id and secret and endpoint):
        return None
    return R2Storage(
        bucket=bucket,
        access_key=key_id,
        secret_key=secret,
        endpoint=endpoint,
        public_base=getattr(settings, "r2_public_base", "") or "",
        provider="r2",
    )
