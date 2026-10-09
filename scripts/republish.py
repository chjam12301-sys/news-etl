"""重新发布索引：修正内部链接 → 全量校验 → 更新指针。

为什么需要
----------
历史故障：导出时用`current_ref()` 取CDN 基址，而此刻新文件**尚未提交**，
于是索引里的 `detail_url` / `audio.url` 指向了不包含这些文件的旧 commit。
表现是**首页卡片正常**（标题、图片、译文都在索引里），**点进详情却 404**。

关键：**只更新 latest.json 修不好** —— 失效地址写死在索引内容里，
必须重新生成索引，让链接基于「确实包含这些文件」的 commit。

正确顺序（app/publish.publish_all）
------------------------------------
    ① 提交详情与音频
    ② 用该 commit 生成索引
    ③ 校验索引引用的每一份详情与音频（含 version/article 身份与 hash）
    ④ 全部通过后才更新 latest 指针

用法
----
    .venv/bin/python scripts/republish.py --dry   # 只校验现状，不改动
    .venv/bin/python scripts/republish.py         # 重新发布
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.is_file():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ[_k.strip()] = _v.strip()

# 数据库连接一律从环境取（DATABASE_URL）。本仓库是 public，
# 任何凭据都不能写进代码 —— 需要时放进 .env 或 Actions Secrets。
if not os.environ.get("DATABASE_URL"):
    print("✗ 缺少 DATABASE_URL（用 .env 或 Actions Secrets 注入）", file=sys.stderr)
    raise SystemExit(2)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-12s | %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.db import SessionLocal, init_db  # noqa: E402
from app.publish import publish_all, verify_published  # noqa: E402
from app.storage import get_storage  # noqa: E402

INDEX_KEY = "data/index.json"


def _read_index_at(repo_dir: str, rev: str) -> dict | None:
    p = subprocess.run(
        ["git", "cat-file", "-p", f"{rev}:content/{INDEX_KEY}"],
        cwd=repo_dir, capture_output=True,
    )
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="重新发布并校验索引")
    ap.add_argument("--dry", action="store_true", help="只校验现状，不改动")
    ap.add_argument("--days", type=int, default=3,
                    help="重新导出最近几天的详情 JSON（默认 3；配图/字段结构变更时传 30）")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    st = get_storage()

    if getattr(st, "provider", "") != "github":
        print("当前存储不是 github，跳过")
        db.close()
        return 0

    rev = st.current_ref()
    print(f"当前 commit: {rev[:7]}\n")

    # ── 先校验现状 ──────────────────────────────────────
    index = _read_index_at(st.repo_dir, rev)
    if index is None:
        print("❌ 读不到索引文件")
        db.close()
        return 1

    res = verify_published(repo_dir=st.repo_dir, rev=rev, index=index)
    print(f"现状校验: {res.summary()}\n")

    if args.dry:
        db.close()
        return 0 if res.ok else 1

    # ── 四阶段重新发布 ──────────────────────────────────
    if res.ok:
        print("现状已健康，仍完整跑一遍发布流程以确保顺序正确。\n")
    else:
        print("现状有问题，按四阶段重新发布。\n")

    out = publish_all(db=db, storage=st, topics_days=args.days,
                      commit_msg="content: 重新发布（修正链接）")
    print(f"\n发布结果 ok={out.get('ok')}")
    print(f"  校验    : {out.get('verify')}")
    print(f"  索引rev : {str(out.get('index_rev', ''))[:7]}")

    if not out.get("ok"):
        print("\n❌ 校验未通过 —— 已保留上一份可用索引，App 不受影响")
        db.close()
        return 1

    # ── 发布后复核 ──────────────────────────────────────
    final_rev = out["index_rev"]
    new_index = _read_index_at(st.repo_dir, final_rev) or {}
    recheck = verify_published(repo_dir=st.repo_dir, rev=final_rev, index=new_index)
    print(f"\n发布后复核: {recheck.summary()}")

    latest_path = Path("content/latest.json")
    latest_ref = ""
    if latest_path.is_file():
        latest_ref = json.loads(
            latest_path.read_text(encoding="utf-8")
        ).get("ref", "")
    print(f"  latest.ref = {latest_ref[:7]}")

    ok = (
        recheck.ok
        and latest_ref.startswith(final_rev[:7])
    )
    print(f"\n{'✅ 发布成功，索引内每一条链接都已验证可访问' if ok else '❌ 仍有问题'}")

    if ok:
        items = [
            it
            for lang in ("en", "ja")
            for it in new_index.get("latest", {}).get("versions", {}).get(lang) or []
        ]
        print(f"   共校验 {len(items)} 条（详情 + 音频 + 身份 + hash）")
        print(f"   索引地址: {latest_ref[:7]}")
        sample = items[0] if items else None
        if sample:
            print(f"   示例 v{sample['version_id']}: {sample['detail_url']}")

    db.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())