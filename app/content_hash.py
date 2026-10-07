"""内容指纹（content_hash）：让 App 判断「这篇是否需要重新下载」。

为什么需要
----------
CDN 的commit SHA 是**全局**的：今天新增了另一篇文章，所有文章的 SHA 都变了，
但老文章内容一个字都没动。App 若只看 URL 里的 SHA，会无谓地重新拉取全部内容。

`content_hash` 是**单篇粒度**的：只有这篇的正文 / 译文 / 音频真的变了才变。

App 用法::

    if (local.content_hash !== remote.content_hash) {
      // 重新下载详情 + 音频
    } else {
      // 命中本地缓存，什么都不用下
    }

计算范围（任一变化则 hash 变化）
-------------------------------
    正文      body / paragraphs / title / lead
    中文译文   title_zh / lead_zh / paragraphs_zh
    词汇表     vocab
    音频       时长、体积、音色、时间轴**完整字段**
              （w / sm / em / cs / ce / si 全部纳入 —— App 用 cs/ce 做
                高亮区间、si 做句循环，缺一不可）

不参与计算
----------
    published_date / image（换配图不算内容变化，不该触发重下）
    audio.url 里的 commit SHA（同上，避免全局 SHA 变化导致误判）
    阅读时长、字数等派生值（由正文决定，重复计入无意义）
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# 保留 16 位hex（约 64bit），足够短又不至于撞
HASH_LEN = 16


def _digest(payload: Any) -> str:
    """稳定序列化后取 sha256。ensure_ascii=False 让中文按原字符参与。"""
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:HASH_LEN]


def timeline_fingerprint(timeline: list[dict[str, Any]] | None) -> str:
    """时间轴指纹：纳入 App 做高亮与句循环所需的全部字段。

    字段与用途的对应关系（App 侧）：
        w   词文本—— 高亮显示
        sm  起始毫秒 —— 播放定位
        em  结束毫秒 —— 播放定位
        cs  起始字符 —— **高亮区间**（在 text 上 setStart）
        ce  结束字符 —— **高亮区间**（在 text 上 setEnd）
        si  句子编号 —— **句循环 / 分句播放**

    曾一度只取 [w, sm, em] 而忽略 cs/ce/si，理由是「字符偏移不影响跟读」。
    这是错的 —— cs/ce 决定高亮落到哪一段字符、si 决定句循环的分组，
    二者变化都会让 App 界面表现不同，必须参与指纹，否则 App 会误判
    「内容没更新」而沿用旧数据，导致高亮错位。
    """
    if not timeline:
        return ""
    return _digest(
        [
            [w.get("w"), w.get("sm"), w.get("em"),
             w.get("cs"), w.get("ce"), w.get("si")]
            for w in timeline
        ]
    )


def audio_fingerprint(
    *,
    duration_ms: int = 0,
    size_bytes: int = 0,
    timeline: list[dict[str, Any]] | None = None,
    voice: str = "",
    engine: str = "",
) -> str:
    return _digest(
        {
            "dur": duration_ms,
            "size": size_bytes,
            "tl": timeline_fingerprint(timeline),
            "voice": voice,
            "engine": engine,
        }
    )


def compute_content_hash(
    *,
    level_code: str,
    lang: str,
    level: int,
    title: str,
    body: str,
    paragraphs: list[Any] | None = None,
    lead: str = "",
    title_zh: str | None = None,
    lead_zh: str | None = None,
    paragraphs_zh: list[Any] | None = None,
    vocab: list[Any] | None = None,
    audio: dict[str, Any] | str | None = None,
) -> str:
    """计算单篇内容指纹。

    参数刻意保持扁平，方便调用方从 ORM 对象直接取。
    audio 传 None 表示无音频；传 dict 时现算音频指纹；
    传 str 时视为已算好的音频指纹（from_version 走这条路径）。
    """
    payload = {
        # 身份：不同文章/等级天然隔离
        "id": [level_code, lang, level],
        # 正文
        "en": [title, body, paragraphs or [], lead],
        # 中文译文
        "zh": [title_zh or "", lead_zh or "", paragraphs_zh or []],
        # 词汇表
        "vocab": vocab or [],
        # 音频（传dict 时现算指纹，传 str 时视为已算好的指纹）
        "au": (
            audio if isinstance(audio, str)
            else (audio_fingerprint(**audio) if audio else "")
        ),
    }
    return _digest(payload)


def from_version(v, audio=None) -> str:
    """从 ArticleVersion (+ AudioAsset) 计算。音频为空时也可算。"""
    au = None
    if audio is not None:
        au = audio_fingerprint(
            duration_ms=audio.duration_ms or 0,
            size_bytes=audio.size_bytes or 0,
            timeline=audio.timeline,
            voice=audio.voice or "",
            engine=audio.engine or "",
        )
    return compute_content_hash(
        level_code=v.level_code,
        lang=v.lang,
        level=v.level,
        title=v.title or "",
        body=v.body or "",
        paragraphs=v.paragraphs or [],
        lead=v.lead or "",
        title_zh=v.title_zh or "",
        lead_zh=v.lead_zh or "",
        paragraphs_zh=v.paragraphs_zh or [],
        vocab=v.vocab or [],
        audio=au,
    )


def diff(old: str | None, new: str) -> str:
    """给日志用：说明为什么变了。"""
    if not old:
        return "首次生成"
    if old == new:
        return "未变化"
    return "内容已更新"