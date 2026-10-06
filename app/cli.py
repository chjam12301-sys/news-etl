"""命令行入口：本地跑流水线 / 定时任务。"""
from __future__ import annotations

import argparse
import logging
import asyncio
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
    d.add_argument("--force", action="store_true", help="忽略已有结果重新生成")
    d.add_argument("--reset", action="store_true",
                   help="清空数据库后重跑（RSS 去重导致抓不到新文章时用）")

    sub.add_parser("serve", help="启动 API 服务")

    a = p.parse_args()

    if a.cmd == "daily":
        from .pipeline import run_daily

        res = asyncio.run(
            run_daily(
                topics=[t.strip() for t in a.topics.split(",")] if a.topics else None,
                per_topic=a.per_topic,
                with_tts=not a.no_tts,
                force=a.force,
                reset_all=a.reset,
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