"""本地跑一次完整生成，并把结果真实上传到 R2 + Neon。

用途：在 GitHub Actions 故障期间，先验证整条链路并产出真实内容。
密钥从环境变量或交互输入获取，不落盘。

用法：
    .venv/bin/python scripts/run_local.py --topics tech
    .venv/bin/python scripts/run_local.py --topics tech --no-tts
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def ask(label: str, env_key: str, *, secret: bool = False) -> str:
    val = os.environ.get(env_key, "").strip()
    if val:
        print(f"  {label:24}来自环境变量 {env_key}")
        return val
    if secret:
        val = getpass.getpass(f"  {label:24}（输入不回显）: ").strip()
    else:
        val = input(f"  {label:24}: ").strip()
    if val:
        os.environ[env_key] = val
    return val


def main() -> int:
    p = argparse.ArgumentParser(description="本地生成并上传到 R2")
    p.add_argument("--topics", default="tech,science")
    p.add_argument("--per-topic", type=int, default=1)
    p.add_argument("--no-tts", action="store_true")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    print("\n请提供以下配置（已有环境变量会自动跳过）：\n")
    ask("Neon DATABASE_URL", "DATABASE_URL", secret=True)
    ask("R2_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID")
    ask("R2_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY")
    ask("R2_BUCKET（回车=news-etl-audio）", "R2_BUCKET") or os.environ.setdefault(
        "R2_BUCKET", "news-etl-audio"
    )
    ask("R2_ENDPOINT", "R2_ENDPOINT") or os.environ.setdefault(
        "R2_ENDPOINT",
        "https://f3e8db4fa711d0a98d3a27def84cd87f.r2.cloudflarestorage.com",
    )
    ask("R2_PUBLIC_BASE", "R2_PUBLIC_BASE") or os.environ.setdefault(
        "R2_PUBLIC_BASE",
        "https://pub-aba43a6fb1db4dc08fede1dbc81f3241.r2.dev",
    )
    os.environ.setdefault("PUBLIC_BASE_URL", os.environ.get("R2_PUBLIC_BASE", ""))

    ds_key = ask("DeepSeek API Key（可回车跳过）", "DEEPSEEK_API_KEY", secret=True)
    if ds_key:
        os.environ.setdefault("LLM_PROVIDER", "deepseek")
        os.environ.setdefault("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        os.environ.setdefault("DEEPSEEK_MODEL", "deepseek-chat")

    # 本地不写 SQLite 文件，强制走 Postgres
    os.environ["DATABASE_URL"] = os.environ.get("DATABASE_URL", "")

    from app.config import get_settings
    from app.storage import get_storage

    s = get_settings()
    print("\n=== 配置确认 ===")
    print(f"  LLM     : {s.llm_provider} ({'有key' if s.deepseek_api_key else '离线降级'})")
    print(f"  模型    : {s.deepseek_model}")
    print(f"  数据库  : {s.database_url.split('@')[-1] if '@' in s.database_url else '未设置'}")
    st = get_storage()
    print(f"  存储    : {st.name}")
    print(f"  CDN     : {s.r2_public_base}")

    from app.pipeline import run_daily

    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    print(f"\n开始生成：主题={topics} 每主题={args.per_topic} 篇\n")
    res = asyncio.run(
        run_daily(topics=topics, per_topic=args.per_topic, with_tts=not args.no_tts, force=args.force)
    )
    print("\n=== 结果 ===")
    for k, v in res.get("stats", {}).items():
        print(f"  {k:14}= {v}")
    print(f"  status        = {res.get('status')}")
    if res.get("error"):
        print(f"  error         = {res['error'][:300]}")

    base = s.r2_public_base
    if base and res.get("stats", {}).get("export") == "ok":
        print(f"\n=== 现在可以在浏览器打开 ===")
        print(f"  索引    {base}/data/index.json")
        print(f"  音频示例{base}/audio/1/en_a1.mp3")
    return 0


if __name__ == "__main__":
    sys.exit(main())