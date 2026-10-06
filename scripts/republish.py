"""发布内容到CDN，并让 JSON 里的链接固定到具体 commit。

架构
----
jsDelivr 对分支名 `@main` 缓存较久（实测四个节点有三个返回旧版），
但对 commit SHA 是精确的。于是：

    data/latest.json   ← 唯一需要「绕过缓存」的小文件，只含 { "ref": "<sha>" }
    data/index.json    ← 里面所有链接都带 @<sha>，可永久缓存

App 只需：
    1. 拉 latest.json?t=<ts>      （强制绕过缓存）
    2. 拼 {cdn}/{ref}/content/...拉后续所有内容
这样详情与音频永久缓存，只有几百字节的 latest.json 需要每次校验。
"""
from __future__ import annotations

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

from app.db import SessionLocal, init_db  # noqa: E402
from app.exporter import export_all  # noqa: E402
from app.storage import get_storage  # noqa: E402

ROOT = Path("content")


def main() -> int:
    init_db()
    db = SessionLocal()
    st = get_storage()

    # 1) 先导出（此时 HEAD =代码最后一次提交的 SHA）
    head = st.current_ref()
    export_all(db)
    print(f"导出时 HEAD = {head}")

    # 2) 提交内容（会产生新 commit，但内容里链接指向 head —— head 一定包含全部内容）
    st.commit(f"content: 更新（链接指向 {head}）")
    after = st.current_ref()
    print(f"提交后 HEAD = {after}")

    # 3) 写 latest.json（它的内容就是 after，供 App 拼 URL 用）
    (ROOT / "latest.json").write_text(
        json.dumps(
            {
                "ref": after,
                "content_ref": head,
                "updated_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                "hint": "App 先拉本文件（加 ?t= 时间戳），再用 ref 拼后续 URL",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    st.commit(f"content: latest.json → {after}")

    final = st.current_ref()
    idx = json.loads((ROOT / "data" / "index.json").read_text(encoding="utf-8"))
    item = idx["latest"]["versions"]["en"][0]
    used = item["detail_url"].split("@")[1].split("/")[0]

    print()
    print(f"latest.json ref   = {final}")
    print(f"JSON 内 detail_url= @{used}")
    print(f"  detail_url: {item['detail_url']}")
    print(f"  audio.url : {item['audio']['url']}")

    # 校验：JSON 指向的 commit 必须包含全部 content
    r = st._git("cat-file", "-e", f"{used}:content/data/versions/1.json", check=False)
    ok = r.returncode == 0
    print(f"\n{'✓' if ok else '✗'} 链接指向的 commit {used} 包含完整内容: {ok}")

    db.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())