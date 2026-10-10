"""增量、可断点续传地从 dictionaryapi.dev 拉取真 IPA，沉淀为仓库内缓存。

## 为什么独立成一个脚本，而不是塞进 build_dict.py

dictionaryapi.dev 免费无 key，但实测高并发会 522 / 超时（约半数请求会超时）。
直接塞进构建流程会让「构建词库」workflow 极不稳定。拆成独立步骤后：

- 本脚本只负责把「词 → 真 IPA」的映射沉淀成 `data/dict/ipa.json`，可多次续跑，
  只补缺失词；中断/断电也不丢已拉到的（每 200 词落盘一次）。
- `build_dict.py` 只读这份缓存：有缓存用缓存（真 IPA），没有就回退到 ECDICT
  自身记音经 `to_ipa()` 转换。构建/上传不再依赖外网实时可达性。

## 许可证提醒

dictionaryapi.dev 数据源自 WordNet / Wiktionary 一系，商用建议在 App 内加一行
署名（如「发音数据：dictionaryapi.dev」）。本脚本只取 phonetic 字段、去斜杆后存储。

## 用法

    python -m scripts.fetch_ipa                 # 拉全部缺失词
    python -m scripts.fetch_ipa --workers 8     # 调并发
    python -m scripts.fetch_ipa --limit 200     # 先小批量试跑
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

log = logging.getLogger("fetch_ipa")

REPO = pathlib.Path(__file__).resolve().parent.parent
INDEX = REPO / "data" / "dict" / "ecdict-subset.json"
CACHE = REPO / "data" / "dict" / "ipa.json"
API = "https://api.dictionaryapi.dev/api/v2/entries/en/{}"

import re

_SLASH = re.compile(r"^/|/$")

# 全局限速：dictionaryapi.dev 免费无 key，经共享出口会被限流（429）。
# 用一把锁把请求最小间隔撑开，既尊重 Retry-After，也避免一上来就触发限流。
_MIN_INTERVAL = 0.0
_throttle_lock = threading.Lock()
_last_req = 0.0


def _rate_limit(min_interval: float) -> None:
    global _last_req
    if min_interval <= 0:
        return
    with _throttle_lock:
        now = time.time()
        wait = min_interval - (now - _last_req)
        if wait > 0:
            time.sleep(wait)
        _last_req = time.time()


def _strip_slashes(s: str) -> str:
    return _SLASH.sub("", s)


def _extract_ipa(data) -> str | None:
    """从 dictionaryapi.dev 的响应里取第一个有效音标（去斜杆）。"""
    cands: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("phonetic"):
                cands.append(o["phonetic"])
            for v in o.get("phonetics", []):
                if isinstance(v, dict) and v.get("text"):
                    cands.append(v["text"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    for c in cands:
        c = _strip_slashes(c).strip()
        if c:
            return c
    return None


def fetch_one(client: httpx.Client, word: str, tries: int = 8):
    url = API.format(word)
    delay = 2.0
    err = None
    for attempt in range(tries):
        _rate_limit(_MIN_INTERVAL)
        try:
            r = client.get(url, timeout=12.0)
            if r.status_code == 200:
                return word, _extract_ipa(r.json()), None
            if r.status_code == 404:
                return word, None, "404"
            # 429 / 503 限流：尊重 Retry-After，否则按退避重试
            if r.status_code in (429, 503):
                ra = r.headers.get("Retry-After")
                if ra and ra.isdigit():
                    time.sleep(min(float(ra), 30.0))
                    continue
                err = f"HTTP {r.status_code}"
            else:
                err = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
        if attempt < tries - 1:
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    return word, None, err or "retries-exhausted"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    # dictionaryapi.dev 的 httpx 请求日志太吵，压到 WARNING
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="从 dictionaryapi.dev 拉取真 IPA 并缓存")
    ap.add_argument("--workers", type=int, default=3, help="并发数（限流严时调小）")
    ap.add_argument("--min-interval", type=float, default=0.4,
                    help="请求最小间隔秒（限流严时调大，如 1.0）")
    ap.add_argument("--tries", type=int, default=8, help="单词最大重试次数")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个缺失词（调试用）")
    args = ap.parse_args()

    global _MIN_INTERVAL
    _MIN_INTERVAL = args.min_interval

    idx = json.loads(INDEX.read_text(encoding="utf-8"))
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.is_file() else {}
    log.info("索引 %d 词，缓存已含 %d 条", len(idx), len(cache))

    todo = [w for w in idx if w.lower().replace(" ", "_") not in cache]
    if args.limit:
        todo = todo[: args.limit]
    log.info("需拉取 %d 词", len(todo))
    if not todo:
        return 0

    done = ok = miss = 0
    with httpx.Client(headers={"User-Agent": "daydaynews-dict/1.0"}, http2=False) as client:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = [pool.submit(fetch_one, client, w) for w in todo]
            for fut in as_completed(futs):
                word, ipa, _err = fut.result()
                done += 1
                if ipa:
                    # 用与 build_dict.shard_of 一致的查找键存储
                    cache[_key(word)] = ipa
                    ok += 1
                else:
                    miss += 1
                if done % 200 == 0:
                    _dump(cache)
                    log.info("  %d/%d  命中 %d  失败 %d", done, len(todo), ok, miss)
    _dump(cache)
    log.info("完成：命中 %d，失败 %d；缓存共 %d 条", ok, miss, len(cache))
    return 0


def _key(word: str) -> str:
    return word.lower().replace(" ", "_")


def _dump(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
