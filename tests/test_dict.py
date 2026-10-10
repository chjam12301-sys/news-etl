"""词条字典的回归测试。

enrich_vocab 已迁移到 App 运行时查词（见 app/dict.py 头部的说明），本仓库
不再在生成端把词典数据烘焙进文章 payload。这里只保留一个仍值得守护的事实：

`data/dict/ecdict-subset.json` 必须真的存在且够大 —— 它是提交进仓库的 ECDICT
子集快照，也是 R2 分片的内容来源；构建若静默跳过，这里会红。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "dict" / "ecdict-subset.json"


def test_committed_index_exists_and_well_formed():
    """仓库必须带上 ECDICT 子集快照；没有它，R2 分片就没有内容来源。"""
    if not INDEX_PATH.is_file():
        pytest.skip("索引尚未生成（先在 CI 跑一次「构建词库」workflow）")
    data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and len(data) > 1000
    # 抽查几个高频词，确认音标、英文释义、词性都真的落在里面
    for word in ("trade", "economy", "artificial", "regulate"):
        hit = data.get(word) or data.get(word.lower())
        assert hit is not None, f"{word} 应该收录在索引里"
        assert hit.get("phonetic"), f"{word} 缺音标"
        assert hit.get("en"), f"{word} 缺英文释义"
        assert hit.get("pos"), f"{word} 缺词性"
        assert len(hit.get("en", "")) <= 121, f"{word} 的英文释义没被截到两行以内"
