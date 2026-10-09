"""把历史文章的配图刷成合规图源（优先 Unsplash）。

背景
----
抓取阶段 `fetcher._entry_image` 会把**新闻原图**写进 `image_url`。那不是 CC0，
商用有版权风险，且各家 CDN 域名千变万化（`scx1.b-cdn.net` / `cdn.arstechnica.net` / …），
域名黑名单根本兜不住。所以现在的规则是：

    **只有 `image_provider` 在合规图源白名单里的，才对外输出实图；否则一律渐变占位块。**

这意味着配图改造之前入库的老文章，现在全都显示渐变块 —— 需要刷一次。

本脚本**只改 image_* 六个字段**，不调用 LLM、不动改写版本、不重新合成音频，
所以**零 DeepSeek 消耗**，几十秒跑完。

用法
----
    # 本地（需要 .env 里有 UNSPLASH_ACCESS_KEY）
    .venv/bin/python scripts/backfill_images.py

    # 只看会改哪些，不写库
    .venv/bin/python scripts/backfill_images.py --dry-run

    # 强制全部重抓（包括已经是 Unsplash 的）
    .venv/bin/python scripts/backfill_images.py --force
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import Article, SessionLocal, init_db  # noqa: E402
from app.images import PROVIDER_NAMES, find_image  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("backfill-images")


def _needs_image(a: Article, *, force: bool) -> bool:
    prov = (a.image_provider or "").strip().lower()
    return force or prov not in PROVIDER_NAMES or not (a.image_url or "").strip()


async def run(*, force: bool = False, dry_run: bool = False) -> None:
    init_db()
    db = SessionLocal()
    try:
        arts = list(db.scalars(select(Article).order_by(Article.id)))
        targets = [a for a in arts if _needs_image(a, force=force)]
        log.info("共 %d 篇，其中 %d 篇需要刷图", len(arts), len(targets))
        if not targets:
            return

        ok = failed = 0
        for i, a in enumerate(targets, 1):
            # 抓取阶段的媒体原图先抹掉，避免失败后又把原图写回去
            if not dry_run:
                a.image_url = ""
            img = await find_image(a.topic, a.title_original)
            if img:
                ok += 1
                log.info("[%d/%d] id=%s → %s | %s", i, len(targets), a.id,
                         img.provider, img.attribution)
                if not dry_run:
                    a.image_url = img.url
                    a.image_credit = img.attribution
                    a.image_provider = img.provider
                    a.image_credit_url = img.creator_url
                    a.image_source_url = img.source_url
                    a.image_author = img.creator
            else:
                failed += 1
                log.info("[%d/%d] id=%s → 全部图源未命中，保持渐变占位",
                         i, len(targets), a.id)
            if not dry_run:
                db.flush()

        if dry_run:
            db.rollback()
            log.info("dry-run：未写库（成功 %d / 失败 %d）", ok, failed)
        else:
            db.commit()
            log.info("已写库：成功 %d / 失败 %d", ok, failed)
    finally:
        db.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="补刷历史文章配图（零 LLM 消耗）")
    ap.add_argument("--force", action="store_true", help="连已是 Unsplash 的也重抓")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = ap.parse_args()

    if not settings.unsplash_access_key:
        log.warning("未配置 UNSPLASH_ACCESS_KEY，将降级到免费图源")

    asyncio.run(run(force=args.force, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
