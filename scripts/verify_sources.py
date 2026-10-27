"""换源/改红线/改 prompt 之后的验收：抓一遍 + 改一遍，只看结果，不落库。

为什么单独写这个：`app.cli daily` 一天只产一篇，而且当天发行日一旦有文章
（`daily_target` 命中）就**完全不抓取**——想验证新的源表和改写质量，
直接跑 daily 会得到 `fetched: 0` 的空跑。

本脚本做三件事，全部只读：
  1. 打印当前源表，确认主题归属没串。
  2. 真抓一轮（红线 / 去重 / https / 正文抽取全走生产代码），
     打印入选文章的来源、URL、正文长度与前 120 字。
  3. 对每篇跑 4 个代表等级（en_a1 / en_b1 / en_c1 / ja_n5）的改写，
     打印标题 + 首段 + 中文译文，并统计英文里的 CJK 字数
     （>0 就是「中文残留」，是必查项）。

不写数据库、不上传 R2、不发布 JSON、不生成音频。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys

from app.config import settings
from app.fetcher import FEEDS, collect
from app.levels import LEVEL_BY_CODE
from app.rewriter import get_llm, rewrite_article

# 汉字 + 假名：出现在「本该是纯英文」的产物里就是没洗干净
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff]")
DEFAULT_LEVELS = "en_a1,en_b1,en_c1,ja_n5"


def _cjk_count(text: str) -> int:
    return len(_CJK.findall(text or ""))


def _hr(title: str = "") -> None:
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--topics", default="tech,lifestyle",
                   help="逗号分隔；留空 = settings.topics 全部")
    p.add_argument("--count", type=int, default=3, help="入选几篇（全局排名取前 N）")
    p.add_argument("--levels", default=DEFAULT_LEVELS, help="要改写验证的等级，逗号分隔")
    a = p.parse_args()

    topics = [t.strip() for t in a.topics.split(",") if t.strip()] or None

    _hr("1. 源表")
    for f in FEEDS:
        print(f"  {f.topic:<10} {f.key:<14} {f.name:<12} {f.url}")
    print(f"\n  共 {len(FEEDS)} 个源；本次主题：{topics or settings.topics}")

    llm = get_llm()
    _hr("2. 抓取（红线 / 去重 / https / 正文抽取均走生产代码）")
    print(f"  LLM provider = {getattr(llm, 'name', 'OFFLINE(无 key)')}")
    # recent_titles 传空：跨天去重在这里没有历史可比，只测当批
    raws = collect(topics, per_topic=1, recent_titles=[], total_limit=a.count)
    print(f"  入选 {len(raws)} 篇")
    for i, r in enumerate(raws, 1):
        print(f"\n  [{i}] source={r.source} topic={r.topic} lang={getattr(r, 'lang', '?')}")
        print(f"      url   = {r.source_url}")
        print(f"      title = {r.title}")
        print(f"      body  = {len(r.body or '')} 字符")
        print(f"      head  = {(r.body or '')[:120].replace(chr(10), ' ')}")

    if not raws:
        print("\n  !! 一篇都没抓到：源表或红线有问题")
        return 1

    codes = [c.strip() for c in a.levels.split(",") if c.strip()]
    problems: list[str] = []
    rewritten = 0

    _hr("3. 改写验证")
    for i, r in enumerate(raws, 1):
        for code in codes:
            spec = LEVEL_BY_CODE.get(code)
            if spec is None:
                print(f"  ?? 未知等级 {code}")
                continue
            res = asyncio.run(rewrite_article(r.topic, r.title, r.body or "", spec, llm))
            rewritten += 1

            first = next((p for p in res.paragraphs if p), "")
            print(f"\n  --- [{i}] {code} ({'degraded' if res.degraded else 'ok'}) ---")
            print(f"      title : {res.title}")
            print(f"      title_zh: {res.title_zh}")
            print(f"      lead  : {res.lead}")
            print(f"      p1    : {first[:220]}")
            if res.paragraphs_zh:
                print(f"      p1_zh : {res.paragraphs_zh[0][:140]}")
            print(f"      vocab : {len(res.vocab)} 个")

            if res.degraded:
                problems.append(f"[{i}] {code} 降级离线（LLM 失败，正文是原文裁剪）")
            if code.startswith("en"):
                hits = _cjk_count(res.title) + sum(_cjk_count(x) for x in res.paragraphs)
                if hits:
                    problems.append(f"[{i}] {code} 英文里含 {hits} 个中日文字符（残留）")
            if not res.title_zh and not res.lead_zh:
                problems.append(f"[{i}] {code} 缺中文译文")

    _hr("4. 结论")
    print(f"  改写 {rewritten} 份，问题 {len(problems)} 条")
    for msg in problems:
        print(f"  - {msg}")
    if not problems:
        print("  全部通过：选题来自国内源，英文无中文残留，中译齐全")
    return 0 if not problems else 2


if __name__ == "__main__":
    sys.exit(main())
