"""重算全部 content_hash 并重新导出。

用途：哈希规则变更（如 v2.4 补入 cs/ce/si）后，让存量内容的
hash 跟上新规则，无需重新生成正文与音频。

用法：
    .venv/bin/python scripts/rehash.py           # 重算 + 导出 + 提交
    .venv/bin/python scripts/rehash.py --dry     # 只看差异，不写入
"""
from __future__ import annotations

import argparse
import json
import os
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

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_MbZs6iN8axdj@"
    "ep-quiet-art-b3edqp3u-pooler.c-4.ap-southeast-1.aws.neon.tech/neondb?sslmode=require",
)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.content_hash import from_version  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.exporter import export_all  # noqa: E402
from app.storage import get_storage  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="重算 content_hash 并导出")
    ap.add_argument("--dry", action="store_true", help="只看差异，不写入")
    args = ap.parse_args()

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db import ArticleVersion

    init_db()
    db = SessionLocal()

    rows = db.scalars(
        select(ArticleVersion)
        .options(selectinload(ArticleVersion.audio))
        .order_by(ArticleVersion.id)
    ).all()

    changed = 0
    print(f"共{len(rows)} 个版本\n")
    for v in rows:
        old = v.content_hash
        new = from_version(v, v.audio)
        if old != new:
            changed += 1
            v.content_hash = new
            print(f"  {v.level_code:7} v{v.id:3} {old or '(空)'} → {new}")

    print(f"\n需更新 {changed} / {len(rows)} 个")

    if args.dry:
        db.rollback()
        db.close()
        print("\n(--dry，未写入)")
        return 0

    db.commit()
    export_all(db)

    # 补 version 自证字段
    st = get_storage()
    import hashlib

    for key in ("data/index.json", "index.json"):
        raw = st.read(key)
        if not raw:
            continue
        d = json.loads(raw)
        lat = d["latest"]
        h = hashlib.sha256(
            json.dumps(
                [v["detail_url"] for v in lat["versions"]["en"] + lat["versions"]["ja"]],
                sort_keys=True,
            ).encode()
        ).hexdigest()[:12]
        d["version"] = {
            "published_at": d["generated_at"],
            "content_hash": h,
            "tip": "本文件即最新版；若published_at 明显早于当前时间，说明命中了 CDN 旧缓存",
        }
        st.put(key, json.dumps(d, ensure_ascii=False, indent=1).encode(),
               content_type="application/json; charset=utf-8")

    st.commit("content: 重算 content_hash（规则变更后重刷）")
    print("✓ 已提交到仓库")

    d = json.loads(st.read("index.json"))
    vals = [it.get("content_hash")
            for l in ("en", "ja") for it in d["latest"]["versions"][l]]
    print(f"  线上索引 hash: {sum(1 for h in vals if h)}/{len(vals)} 有值")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
