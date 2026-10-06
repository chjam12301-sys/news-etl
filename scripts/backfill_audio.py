"""为数据库里已有正文但缺音频的版本补齐 TTS + 上传 R2 + 导出 JSON。

背景
----
GitHub Actions runner 到 Cloudflare R2 的 TLS 被阻断（boto3 与 aws cli 均报
SSLV3_ALERT_HANDSHAKE_FAILURE），而正文改写已经成功落库了。此时只需：
    合成音频 → 上传 R2 → 导出 JSON
**完全不调用 LLM，零 DeepSeek 消耗。**

用法
----
    .venv/bin/python scripts/backfill_audio.py

密钥只从交互输入获取，不落盘。
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 优先从项目根的 .env 读取（scripts/save_r2_keys.sh 会写入），这样只需输入一次
_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.is_file():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

DEFAULTS = {
    "R2_BUCKET": "news-etl-audio",
    "R2_ENDPOINT": "https://f3e8db4fa711d0a98d3a27def84cd87f.r2.cloudflarestorage.com",
    "R2_PUBLIC_BASE": "https://pub-aba43a6fb1db4dc08fede1dbc81f3241.r2.dev",
}


def ask(label: str, env_key: str, *, secret: bool = False, required: bool = True) -> str:
    val = os.environ.get(env_key, "").strip()
    if val:
        return val
    if secret:
        val = getpass.getpass(f"  {label} （不回显）: ").strip()
    else:
        d = DEFAULTS.get(env_key)
        val = input(f"  {label} （回车={d}）: ").strip() or d or ""
    if val:
        os.environ[env_key] = val
    return val


def have_all() -> bool:
    """若 .env 已含全部必需项，则无需交互。"""
    return all(os.environ.get(k) for k in
               ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT", "R2_BUCKET"))


async def main() -> int:
    p = argparse.ArgumentParser(description="补齐音频（不调用 LLM）")
    p.add_argument("--limit", type=int, default=0, help="最多处理多少个版本，0=全部")
    p.add_argument("--dry-run", action="store_true", help="只看要做什么，不实际执行")
    args = p.parse_args()

    print("\n" + "=" * 60)
    print(" 补齐音频 + 导出 —— 不抓新闻、不调用 LLM")
    print("=" * 60 + "\n")

    # 数据库：读线上 Neon（正文在那里）
    ds = os.environ.get("DATABASE_URL", "")
    if not ds.startswith("postgresql"):
        ds = (
            "postgresql://neondb_owner:npg_MbZs6iN8axdj@"
            "ep-quiet-art-b3edqp3u-pooler.c-4.ap-southeast-1.aws.neon.tech/"
            "neondb?sslmode=require"
        )
    os.environ["DATABASE_URL"] = ds
    print("  数据库      : 线上 Neon")

    ask("R2_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID")
    ask("R2_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY", secret=True)
    ask("R2_BUCKET", "R2_BUCKET")
    ask("R2_ENDPOINT", "R2_ENDPOINT")
    ask("R2_PUBLIC_BASE", "R2_PUBLIC_BASE")
    os.environ.setdefault("PUBLIC_BASE_URL", os.environ.get("R2_PUBLIC_BASE", ""))
    os.environ.setdefault("EXPORT_JSON", "true")

    if not os.environ.get("R2_SECRET_ACCESS_KEY"):
        print("\n缺少 R2 密钥。先执行一次下面这条，之后就不用再输了：")
        print("  bash scripts/save_r2_keys.sh\n")
        return 1

    from app.db import init_db, SessionLocal
    from app.tts import synthesize
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.db import Article, ArticleVersion, AudioAsset
    from app.storage import audio_key, get_storage

    init_db()
    db = SessionLocal()
    storage = get_storage()

    rows = db.execute(
        select(ArticleVersion, Article)
        .join(Article, Article.id == ArticleVersion.article_id)
        .options(selectinload(ArticleVersion.audio))
        .order_by(Article.id, ArticleVersion.lang, ArticleVersion.level)
    ).all()

    todo = [(v, a) for v, a in rows if v.audio is None]
    if args.limit:
        todo = todo[: args.limit]

    print(f"\n  存储: {storage.name}")
    print(f"  CDN : {os.environ.get('R2_PUBLIC_BASE')}")
    print(f"\n  数据库共 {len(rows)} 个版本，其中 {len(todo)} 个缺音频\n")

    if args.dry_run:
        for v, a in todo:
            print(f"    将生成: article {a.id} / {v.level_code:7} {v.word_count:4}词")
        db.close()
        return 0

    if not todo:
        print("  没有需要补的音频，跳过。\n")
    ok = fail = 0
    total = len(todo)

    for i, (v, a) in enumerate(todo, 1):
        tag = f"[{i}/{total}] article{a.id}/{v.level_code}"
        try:
            res = await synthesize(v.body, v.lang)
            is_mp3 = res.engine == "edge-tts"
            key = audio_key(v.article_id, v.level_code, "mp3" if is_mp3 else "wav")
            obj = storage.put(key, res.audio_bytes,
                              content_type="audio/mpeg" if is_mp3 else "audio/wav")
            if v.audio is not None:
                db.delete(v.audio)
                db.flush()
            db.add(AudioAsset(
                version_id=v.id, lang=v.lang, voice=res.voice,
                file_path=key, public_url=obj.url or "",
                duration_ms=int(res.duration * 1000), size_bytes=len(res.audio_bytes),
                timeline=res.timeline_dict(), boundaries=res.boundaries_dict(),
                engine=res.engine,
            ))
            db.commit()
            ok += 1
            print(f"  ✓ {tag} {res.duration:6.1f}s {len(res.audio_bytes)//1024:4d}KB "
                  f"{len(res.timings)} 词时间轴")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"  ✗ {tag} {type(exc).__name__}: {str(exc)[:110]}")
            db.rollback()

    # 导出 JSON
    print("\n  导出 JSON ...")
    try:
        from app.exporter import export_all

        r = export_all(db)
        print(f"  ✓ 导出完成: {r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 导出失败: {exc}")

    base = os.environ.get("R2_PUBLIC_BASE", "")
    print("\n" + "=" * 60)
    print(f"  成功 {ok} / 失败 {fail}")
    if base:
        print("\n  在浏览器打开：")
        print(f"   {base}/data/index.json")
        print(f"   {base}/data/versions/1.json")
    print("=" * 60)

    db.close()
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))