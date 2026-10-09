"""选题去重：同一事件被多家媒体改写后标题不同，仅靠 URL 指纹拦不住。

背景（实测）：源指纹只按归一化 URL + 标题算，于是
- 同一条 Ars Technica 头条隔天改了几个词 → 新指纹 → 又写一篇
  （"Oil companies ask top court…" / "Oil Companies Ask Supreme Court…"）
- 同一事件 BBC / Nature 各报一次 → 两篇
  （"Former German spy chief arrested…" / "Germany arrests former spy chief…"）
结果北极冻土写了 4 篇、Nvidia 3 篇、石油公司 3 篇。

这里按「标题实词集合」判重，跨源、跨天都生效。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 阈值（用真实历史标题调出来的，见 tests/test_topic_quality.py）
JACCARD_DUP = 0.45      # 整体相似度
MIN_SHARED = 3          # 至少共享 3 个实词
MIN_CONTAIN = 0.40      # 且占较短标题实词数的比例
MIN_TOKENS = 3          # 太短的标题不做包含判断，避免误杀

# 标题里几乎每篇都出现的词。不去掉的话任意两条新闻都会"相似"。
_STOP = frozenset(
    """
a an the and or but if of to in on at for from by as is are was were be been being
will would can could should may might must has have had do does did this that these
those it its his her their our your my he she they we you i not no new now more most
many much one two three four five first last next over into than then after before
about says said say tell tells told year years day days week weeks month months time
make made get got take taken also just like amid via us uk world news report reports
update live latest top best big small amid wins win
""".split()
)

# Google News 的标题形如 "Apple launches X - MacRumors"，尾部是发布方
_PUB_SUFFIX = re.compile(r"\s+[|\-–—]\s+(?P<pub>[^|\-–—]{2,40})\s*$")
# 非字母数字（含中日文保留）
_SPLIT = re.compile(r"[^0-9a-z一-鿿぀-ヿ]+")


def strip_publisher(title: str, publisher: str = "") -> str:
    """去掉 Google News 标题尾部的 " - Publisher"。

    publisher 已知（RSS 的 <source> 标签）时按精确后缀剥；
    否则只在「尾部是单个词」时才剥 —— " - Supreme Court" 是标题的一部分，
    剥掉会把 "Oil firms sue - Supreme Court" 变成 "Oil firms sue"。
    """
    t = (title or "").strip()
    if publisher and t.endswith(f" - {publisher}"):
        head = t[: -len(publisher) - 3].strip()
        if len(head) >= 8:
            return head
    m = _PUB_SUFFIX.search(t)
    if m:
        pub = m.group("pub").strip()
        head = t[: m.start()].strip()
        if " " not in pub and len(head) >= 12:
            return head
    return t


def content_tokens(
    title: str, *, publisher_suffix: bool = False, publisher: str = ""
) -> set[str]:
    """标题 → 实词集合（小写、去停用词、去标点）。"""
    t = strip_publisher(title, publisher) if publisher_suffix else (title or "")
    out: set[str] = set()
    for tok in _SPLIT.split(t.lower()):
        if len(tok) < 3 or tok in _STOP:
            continue
        out.add(tok)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _containment(a: set[str], b: set[str]) -> float:
    """交集占较小集合的比例 —— 短标题被长标题完整包含时 Jaccard 会偏低。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def is_duplicate(new: str, existing: str) -> bool:
    """两条标题是否同一事件。"""
    a = content_tokens(new)
    b = content_tokens(existing)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < MIN_TOKENS or len(b) < MIN_TOKENS:
        return _jaccard(a, b) >= JACCARD_DUP
    shared = len(a & b)
    return _jaccard(a, b) >= JACCARD_DUP or (
        shared >= MIN_SHARED and _containment(a, b) >= MIN_CONTAIN
    )


@dataclass(slots=True)
class TopicIndex:
    """一批已用过的标题。跨源、跨天共用同一个实例。"""

    titles: list[str] = field(default_factory=list)
    _tokens: list[set[str]] = field(default_factory=list)

    def add(self, title: str) -> None:
        self.titles.append(title)
        self._tokens.append(content_tokens(title))

    def find(self, title: str) -> str | None:
        """命中的历史标题；不重复则返回 None。"""
        a = content_tokens(title)
        if not a:
            return None
        for tok, old in zip(self._tokens, self.titles):
            if not tok:
                continue
            if tok == a:
                return old
            if len(a) < MIN_TOKENS or len(tok) < MIN_TOKENS:
                if _jaccard(a, tok) >= JACCARD_DUP:
                    return old
                continue
            shared = len(a & tok)
            if _jaccard(a, tok) >= JACCARD_DUP or (
                shared >= MIN_SHARED and _containment(a, tok) >= MIN_CONTAIN
            ):
                return old
        return None

    def is_duplicate(self, title: str) -> bool:
        return self.find(title) is not None


def build_index(titles) -> TopicIndex:
    idx = TopicIndex()
    for t in titles:
        if t:
            idx.add(t)
    return idx
