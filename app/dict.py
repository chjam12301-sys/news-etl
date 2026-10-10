"""词条字典：ECDICT 高频子集的上线（R2 存储后端）。

## 它解决什么

App 端词汇卡需要音标（/treɪd/）与英文释义，这两样来自离线词典 ECDICT
（MIT，可商用），**不是 LLM 改写能编出来的**。数据由 `scripts/build_dict.py`
从 ECDICT 构建成 R2 分片（`dict/en/<前两字母>.json`）并提交进仓库的
`data/dict/ecdict-subset.json` 快照。

## 运行时查词，不在生成端烘焙

早期做法是在 pipeline 里调用 `enrich_vocab` 把 `phonetic` / `en` 写进词条
payload，导致改音标必须重跑内容生成。现在改为 App 在运行时直读 R2 分片回填
（见 Day 仓库的 `shared/.../reading/DictionaryClient.kt`），生成端不再把词典
数据写进文章。词典更新对所有历史文章立即生效，无需重跑。

本模块只保留上传用的存储后端 `get_dict_storage()`；词条加载、变形词回退、
词性映射等查词逻辑都在 App（Kotlin）与 Worker（`workers/dict-api/src/index.mjs`）
里实现，三处共用同一套分片键与回退规则（见 `scripts/build_dict.py` 头部的说明）。
"""
from __future__ import annotations

import logging

from .config import settings
from .storage import R2Storage

log = logging.getLogger(__name__)


def get_dict_storage():
    """词条对象的存储后端。与音频共用同一组 R2 密钥，只是 key 前缀不同。"""
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
