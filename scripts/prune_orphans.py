"""清理 content/ 下「数据库里已不存在」的孤儿文件。

背景
----
`article` / `article_versions` 被 reset 清掉过（RSS 去重抓不到新文章时用过 `--reset`），
但 `content/` 里的详情 JSON 还留着：380 份里有 **290 份是孤儿**，占约 15MB。
jsDelivr 单仓库 50MB 硬上限，超限后新路径一律 403 —— 这些垃圾必须清。

判定规则（保守，只删「不可能再被访问」的文件）
------------------------------------------------
1. `content/data/versions/<version_id>.json`：**version_id 不在数据库** → 孤儿。
   App 只能通过索引里的 `detail_url` 访问详情，而索引只引用库内记录，
   所以这些文件删掉不会让任何当前可访问的内容 404。
2. `content/data/<YYYY-MM-DD>/`：该目录下涉及的所有 article_id 都不在库 → 孤儿。
   部分命中的保留（还有库内文章）。

⚠️ 代价要知道：已经**缓存过旧 detail_url 的 App**（比如用户收藏过某篇）
再点进去会 404。这是清理的固有代价，靠 GITHUB_KEEP_DAYS 慢慢淘汰也行，
但那样仓库会一直顶在 20MB。

用法
----
    .venv/bin/python scripts/prune_orphans.py --dry-run   # 只打印清单
    .venv/bin/python scripts/prune_orphans.py             # 真删 + 提交
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import Article, ArticleVersion, SessionLocal, init_db  # noqa: E402
from app.storage import get_storage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("prune-orphans")

VERSIONS_DIR = "content/data/versions"


def _mb(paths: list[str]) -> float:
    return sum(os.path.getsize(p) for p in paths if os.path.isfile(p)) / 1024 / 1024


def main() -> None:
    ap = argparse.ArgumentParser(description="清理数据库里已不存在的孤儿详情文件")
    ap.add_argument("--dry-run", action="store_true", help="只打印清单，不删除")
    ap.add_argument("--commit", action="store_true",
                    help="删除后自行提交（默认不提交，交给后续 publish 一起带上）")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        db_vids = {v for (v,) in db.execute(select(ArticleVersion.id)).all()}
        db_aids = {a for (a,) in db.execute(select(Article.id)).all()}
    finally:
        db.close()

    # ── ① 孤儿详情 ────────────────────────────────────────────────
    files = glob.glob(f"{VERSIONS_DIR}/*.json")
    orphan_files = [
        f for f in files
        if int(re.search(r"(\d+)\.json$", f).group(1)) not in db_vids
    ]
    keep_files = [f for f in files if f not in orphan_files]

    # ── ② 孤儿按日目录（该目录涉及的文章全部不在库）─────────────────
    orphan_days: list[str] = []
    for day_dir in sorted(glob.glob("content/data/20??-??-??")):
        if not os.path.isdir(day_dir):
            continue
        names = set()
        for meta in glob.glob(f"{day_dir}/*.json"):
            try:
                import json
                d = json.load(open(meta, encoding="utf-8"))
                for a in d.get("articles", []):
                    if a.get("article_id"):
                        names.add(a["article_id"])
            except Exception:  # noqa: BLE001
                continue
        if names and names.isdisjoint(db_aids):
            orphan_days.append(day_dir)

    # ── 报告 ─────────────────────────────────────────────────────
    log.info("库内 %d 篇 / %d 版本；磁盘 %d 份详情",
             len(db_aids), len(db_vids), len(files))
    log.info("孤儿详情 %d 份（%.1fMB），保留 %d 份（%.1fMB）",
             len(orphan_files), _mb(orphan_files),
             len(keep_files), _mb(keep_files))
    if orphan_days:
        log.info("孤儿按日目录 %d 个：%s", len(orphan_days),
                 ", ".join(os.path.basename(d) for d in orphan_days))

        if args.dry_run:
            log.info("dry-run：未删除")
            return

    if not orphan_files and not orphan_days:
        log.info("没有孤儿，无需清理")
        return

    for f in orphan_files:
        os.remove(f)
    for d in orphan_days:
        for f in glob.glob(f"{d}/*"):
            if os.path.isfile(f):
                os.remove(f)
        os.rmdir(d)
    log.info("已删除 %d 份详情 + %d 个按日目录", len(orphan_files), len(orphan_days))

    if args.commit:
        get_storage().commit("chore: 清理孤儿内容（数据库已无对应记录）")
        log.info("已提交（删除已留在工作区，交给 publish 一起提交也可以）")


if __name__ == "__main__":
    main()
