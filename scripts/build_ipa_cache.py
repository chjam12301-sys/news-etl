"""从 open-dict-data/ipa-dict 构建真 IPA 缓存 data/dict/ipa.json。

## 为什么是 ipa-dict 而不是 dictionaryapi.dev

dictionaryapi.dev 免费无 key，但经本机共享出口会被限流（429），2 万词拉不动
（实测首波成功、随后整段被 429 卡死）。ipa-dict 是 CC0 的离线词表
（en_US 12.6 万条 + en_UK 6.5 万条），一次下载、无频率限制，对本项目 2 万高频词
的覆盖率 98%+。脚本下载两份 txt，取 en_US 优先、en_UK 兜底；多音标变体只取第一个
（用 / 分隔），剥掉斜杠后写入缓存。剩余极少数缺失词由 build_dict 的 to_ipa()
回退兜底。

缓存键约定与 build_dict.shard_of 一致：小写、空格转下划线；值不带斜杠
（App 侧统一用 / / 包裹显示）。

## 用法

    python -m scripts.build_ipa_cache            # 下载并构建
    python -m scripts.build_ipa_cache --no-download  # 复用本地已下好的 txt
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import httpx

REPO = pathlib.Path(__file__).resolve().parent.parent
CACHE_DIR = REPO / "data" / "dict"
INDEX = CACHE_DIR / "ecdict-subset.json"
CACHE = CACHE_DIR / "ipa.json"
US_URL = "https://raw.githubusercontent.com/open-dict-data/ipa-dict/master/data/en_US.txt"
UK_URL = "https://raw.githubusercontent.com/open-dict-data/ipa-dict/master/data/en_UK.txt"

# 渲染兼容性归一：ɫ（软颚化 l）在很多字体里没有字形，归一成普通 l。
# 其余 IPA 字符（含 ɹ、ɑ、ɔ 等）保留，App 字体通常都支持。
_NORMALIZE = str.maketrans({"ɫ": "l"})


def _key(word: str) -> str:
    return word.lower().replace(" ", "_")


def _first_variant(ipa: str) -> str:
    # 先剥掉包裹音标的首尾斜杠（ipa-dict 形如 /x/），再按 / 切分多音标变体取第一个。
    ipa = ipa.strip().strip("/").strip()
    return ipa.split("/")[0].strip()


def load_txt(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        w = parts[0].strip().lower()
        ipa = _first_variant(parts[1])
        if not w or not ipa:
            continue
        out[_key(w)] = ipa.translate(_NORMALIZE)
    return out


def download(url: str, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=httpx.Timeout(120.0, connect=30.0)
    ) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
        tmp.replace(dest)


def main() -> int:
    ap = argparse.ArgumentParser(description="从 ipa-dict 构建 IPA 缓存")
    ap.add_argument("--no-download", action="store_true", help="复用本地已下好的 txt")
    args = ap.parse_args()

    us_path = CACHE_DIR / "ipa-dict-en_US.txt"
    uk_path = CACHE_DIR / "ipa-dict-en_UK.txt"
    if not args.no_download:
        print(f"下载 {US_URL}")
        download(US_URL, us_path)
        print(f"下载 {UK_URL}")
        download(UK_URL, uk_path)

    us = load_txt(us_path)
    uk = load_txt(uk_path)
    print(f"ipa-dict: en_US {len(us)} 条, en_UK {len(uk)} 条")

    # 合并：en_US 优先，缺的用 en_UK 补。
    merged = dict(uk)
    merged.update(us)

    # 与已有缓存（可能来自 dictionaryapi.dev 的手工核对）取并集；手工核对过的不覆盖。
    existing = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.is_file() else {}
    merged.update(existing)

    # 只保留本词库实际需要的词，控制缓存体积。
    idx = json.loads(INDEX.read_text(encoding="utf-8"))
    needed = {_key(w) for w in idx}
    cache = {k: merged[k] for k in needed if k in merged}

    CACHE.write_text(
        json.dumps(cache, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    miss = len(needed) - len(cache)
    print(f"写出缓存 {len(cache)} 条（覆盖 {len(cache) / len(needed):.1%}），缺失 {miss} 条将由 to_ipa() 兜底 → {CACHE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
