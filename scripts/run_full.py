"""完整重跑内容：抓取 → AI 改写（含中文译文）→ TTS + 时间轴 → 导出 → 提交。

密钥只从环境变量/交互输入获取，不落盘。

用法：
    bash scripts/regen.sh --force
    DEEPSEEK_API_KEY=sk-xxx .venv/bin/python scripts/run_full.py --force
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.is_file():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ[_k.strip()] = _v.strip()
    _cfg = sys.modules.get("app.config")
    if _cfg is not None and hasattr(_cfg, "get_settings"):
        _cfg.get_settings.cache_clear()

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_MbZs6iN8axdj@"
    "ep-quiet-art-b3edqp3u-pooler.c-4.ap-southeast-1.aws.neon.tech/neondb?sslmode=require",
)


async def main() -> int:
    ap = argparse.ArgumentParser(description="完整重跑内容")
    ap.add_argument("--topics", default="tech,science,health")
    ap.add_argument("--per-topic", type=int, default=1)
    ap.add_argument("--force", action="store_true", help="重建已有内容（加中文译文需加）")
    ap.add_argument("--no-tts", action="store_true")
    args = ap.parse_args()

    from app.config import get_settings
    from app.storage import get_storage

    s = get_settings()
    st = get_storage()

    print("\n" + "=" * 62)
    print(f" 存储      : {st.name}")
    print(f" LLM       : {s.llm_provider} ({'有 key' if s.deepseek_api_key else '离线降级'})")
    print(f" 配图       : Openverse CC0（{ '开' if s.image_fetch_enabled else '关'}，"
          f"允许媒体原图兜底: {'是' if getattr(s, 'image_allow_source_fallback', False) else '否'}）")
    print(f" TTS       : {'开' if not args.no_tts else '关'}")
    print("=" * 62 + "\n")

    from app.pipeline import run_daily

    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    print(f"开始生成：主题={topics}，每主题 {args.per_topic} 篇\n")

    res = await run_daily(
        topics=topics,
        per_topic=args.per_topic,
        with_tts=not args.no_tts,
        force=args.force,
    )

    print("\n" + "=" * 62)
    for k, v in (res.get("stats") or {}).items():
        print(f"  {k:16}= {v}")
    print(f"  {'status':16}= {res.get('status')}")
    if res.get("error"):
        print(f"  error             = {str(res['error'])[:300]}")

    # GitHub 存储：提交内容 + 清理旧文件
    if getattr(st, "provider", "") == "github":
        print("\n  提交到 GitHub ...")
        try:
            pushed = st.commit(f"content: 重新生成（含中文译文）")
            print(f"  {'✓ 已推送' if pushed else '（无变更）'}")
            keep = getattr(s, "github_keep_days", 14)
            r = st.prune(keep)
            if r["deleted"]:
                print(f"  ✓ 清理 {r['deleted']} 个超{keep} 天的文件"
                      f"（{r['bytes'] / 1024 / 1024:.1f} MB）")
            print(f"  仓库内容 {st.used_bytes() / 1024 / 1024:.1f} MB")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ 推送失败: {exc}")

    base = os.environ.get("PUBLIC_BASE_URL", "")
    if base:
        print("\n" + "-" * 62)
        print(" 验证地址：")
        print(f"   {base}/index.json")
        print("-" * 62)

    return 0 if res.get("status") == "success" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
