"""本地跑一次完整生成，并把结果真实上传到 R2。

用途：GitHub Actions runner 到 Cloudflare R2 的 TLS 被阻断时，
从本机上传（本机到 R2 的 TLS 正常）。

密钥只从环境变量或交互输入获取，**不写入任何文件**。

用法
----
    .venv/bin/python scripts/run_local.py --topics tech,science

交互提示里除以下几项外，其余直接回车用默认值：
    - Neon DATABASE_URL   （回车=用本地 SQLite，不碰线上库）
    - R2_ACCESS_KEY_ID
    - R2_SECRET_ACCESS_KEY
    - DeepSeek API Key     （回车=跳过，走离线降级）
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULTS = {
    "R2_BUCKET": "news-etl-audio",
    "R2_ENDPOINT": "https://f3e8db4fa711d0a98d3a27def84cd87f.r2.cloudflarestorage.com",
    "R2_PUBLIC_BASE": "https://pub-aba43a6fb1db4dc08fede1dbc81f3241.r2.dev",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
    "DEEPSEEK_MODEL": "deepseek-chat",
}


def ask(label: str, env_key: str, *, secret: bool = False, required: bool = False) -> str:
    """问一次，优先环境变量，其次交互输入。"""
    val = os.environ.get(env_key, "").strip()
    if val:
        return val

    if secret:
        hint = "（输入不回显）" if required else "（输入不回显，可直接回车跳过）"
        val = getpass.getpass(f"  {label} {hint}: ").strip()
    else:
        d = DEFAULTS.get(env_key)
        hint = f"（回车={d}）" if d else ""
        val = input(f"  {label} {hint}: ").strip() or d or ""

    if val:
        os.environ[env_key] = val
    return val


def main() -> int:
    p = argparse.ArgumentParser(description="本地生成并上传到 R2")
    p.add_argument("--topics", default="tech,science")
    p.add_argument("--per-topic", type=int, default=1)
    p.add_argument("--no-tts", action="store_true", help="只生成文本，不配音（快很多）")
    p.add_argument("--force", action="store_true", help="忽略已有结果，重新生成")
    p.add_argument("--use-neon", action="store_true", help="写入线上 Neon（默认用本地 SQLite）")
    args = p.parse_args()

    print("\n" + "=" * 62)
    print(" 本地生成 —— 密钥只输入到本进程，不落盘")
    print("=" * 62 + "\n")

    if args.use_neon:
        ask("Neon DATABASE_URL", "DATABASE_URL", secret=True, required=True)
    else:
        os.environ["DATABASE_URL"] = "sqlite:///./data/news.db"
        print("  数据库          : 本地 SQLite（加 --use-neon 可写线上）")

    ask("R2_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID", required=True)
    ask("R2_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY", secret=True, required=True)
    ask("R2_BUCKET", "R2_BUCKET")
    ask("R2_ENDPOINT", "R2_ENDPOINT")
    ask("R2_PUBLIC_BASE", "R2_PUBLIC_BASE")
    os.environ.setdefault("PUBLIC_BASE_URL", os.environ.get("R2_PUBLIC_BASE", ""))

    if ask("DeepSeek API Key", "DEEPSEEK_API_KEY", secret=True):
        os.environ.setdefault("LLM_PROVIDER", "deepseek")
        os.environ.setdefault("DEEPSEEK_BASE_URL", DEFAULTS["DEEPSEEK_BASE_URL"])
        os.environ.setdefault("DEEPSEEK_MODEL", DEFAULTS["DEEPSEEK_MODEL"])

    from app.config import get_settings
    from app.storage import get_storage

    s = get_settings()
    st = get_storage()

    print("\n" + "-" * 62)
    print(f"  LLM      : {s.llm_provider} / {s.deepseek_model if s.deepseek_api_key else '离线降级'}")
    img_src = "UNSPLASH" if s.unsplash_access_key else (
        "PEXELS" if s.pexels_api_key else "免费源(wikimedia/openverse)")
    print(f"  配图      : {img_src}（{'开' if s.image_fetch_enabled else '关'}，"
          f"链: {s.image_providers}）")
    print(f"  TTS      : {'开' if not args.no_tts else '关'}")
    print(f"  存储      : {st.name}")
    print(f"  CDN       : {s.r2_public_base}")
    db_target = "线上 Neon" if s.database_url.startswith("postgresql") else "本地 SQLite"
    print(f"  数据库    : {db_target}")
    print("-" * 62 + "\n")

    # 容量护栏：上传前先看用量
    try:
        from app.storage_guard import check_capacity, current_usage_gb

        if st.name == "r2":
            print(f"  R2 当前占用: {current_usage_gb(refresh=True):.3f} GB / 上限 8 GB")
            check_capacity()
    except Exception as exc:  # noqa: BLE001
        print(f"  容量检查跳过: {exc}")

    from app.pipeline import run_daily

    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    print(f"\n开始生成：主题={topics}，每主题 {args.per_topic} 篇\n")

    res = asyncio.run(
        run_daily(
            topics=topics,
            per_topic=args.per_topic,
            with_tts=not args.no_tts,
            force=args.force,
        )
    )

    print("\n" + "=" * 62)
    print(" 结果")
    print("=" * 62)
    for k, v in (res.get("stats") or {}).items():
        print(f"  {k:16}= {v}")
    print(f"  {'status':16}= {res.get('status')}")
    if res.get("error"):
        print(f"  error             = {str(res['error'])[:400]}")

    base = s.r2_public_base
    if base and (res.get("stats") or {}).get("export") == "ok":
        print("\n" + "-" * 62)
        print(" 现在可以在浏览器打开：")
        print(f"   索引     {base}/data/index.json")
        print("-" * 62)
    elif (res.get("stats") or {}).get("audio_errors"):
        print("\n⚠️部分音频上传失败，检查上方错误信息。")

    return 0 if res.get("status") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())