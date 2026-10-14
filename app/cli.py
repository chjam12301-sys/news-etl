"""命令行入口：本地跑流水线 / 定时任务。"""
from __future__ import annotations

import argparse
import logging
import asyncio
import datetime as dt
import json
import sys


def main() -> int:
    # CLI 也要输出日志 —— 否则 pipeline/images 的 log.info 全部丢失，
    # Actions 日志里看不到 [pipe] / [img] 任何痕迹，排查无从下手。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-10s | %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    p = argparse.ArgumentParser(description="每日英语听力内容后台")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daily", help="执行每日抓取+改写+TTS")
    d.add_argument("--topics", help="逗号分隔，如 tech,science")
    d.add_argument("--per-topic", type=int, default=1)
    d.add_argument("--no-tts", action="store_true")
    d.add_argument("--force", action="store_true", help="仅补当前发行日缺失译文，不新增文章")
    d.add_argument("--warmup-only", action="store_true",
                   help="只补今日已有文章的可选音色（有变化时会重发一次 JSON）")
    d.add_argument("--date", help="回填指定发行日 YYYY-MM-DD 缺失的音频（留空=今天）")
    d.add_argument("--warmup-days", type=int, default=0,
                   help="回填最近 N 天（含 --date 当日）每日文章的可选音色："
                        "音频缺失重生成、时间轴对象缺失补传。不抓新闻、不改写、不动默认音频")

    sub.add_parser("serve", help="启动 API 服务")

    a = p.parse_args()

    if a.cmd == "daily":
        from .pipeline import run_daily

        day_override = dt.date.fromisoformat(a.date) if a.date else None
        res = asyncio.run(
            run_daily(
                topics=[t.strip() for t in a.topics.split(",")] if a.topics else None,
                per_topic=a.per_topic,
                with_tts=not a.no_tts,
                force=a.force,
                warmup_only=a.warmup_only,
                day_override=day_override,
                warmup_days=a.warmup_days,
            )
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("status") == "success" else 1

    if a.cmd == "serve":
        import uvicorn

        uvicorn.run("app.api:app", host="0.0.0.0", port=8000, reload=False)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())