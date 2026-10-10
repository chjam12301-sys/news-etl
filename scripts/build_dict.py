"""构建 ECDICT 高频子集，并上线到对象存储（R2）。

## 为什么在 CI 里跑

全量 `ecdict.csv` 是 **62.9 MB**，本机实测拉 `raw.githubusercontent.com`
1 MB 分片 60 秒都拿不到（超时退出）。GitHub Actions runner 网络正常，
所以这一步放 CI。

上传也**不需要 rclone** —— 本仓 `app/storage.R2Storage` 已经是一条验证过的
通道（boto3 主用、aws cli 兜底），音频上传走的就是它，密钥与 `daily.yml`
共用同一组 secrets。

## 产物

| 路径 | 去向 | 用途 |
|---|---|---|
| `dist/dict/shards/<前两字母>.json` | 上传 R2 `dict/en/<前两字母>.json` | 运行时查询接口（Worker 读它） |
| `data/dict/ecdict-subset.json` | 提交进仓库 | ECDICT 子集快照，R2 分片的内容来源（App 运行时查词用，不在生成端烘焙） |

**为什么按前两个字母分片，而不是一词一对象**：两万词 = 两万次 PUT（+
HEAD 预检就是四万次）。实测在 GitHub runner 上跑 30 分钟才传到字母 a，
CI 的 45 分钟上限根本跑不完。分片后对象数降到几百个，时间从小时级到分钟级。
Worker 一次 GET 拿到一个 shard 再本地取词，多读的只有几十 KB。

## 两段式执行

`--build-only` 与 `--upload-only` 分开，workflow 里**先构建并提交索引**
（快、必成），**再上传 R2**（慢、可重试）。这样上传即使超时，索引也已经
落库，而 App 侧运行时直读 R2 分片，与索引是否已提交无关。

## 用法

    python -m scripts.build_dict --build-only        # 下载 + 解析 + 落索引与分片
    python -m scripts.build_dict --upload-only       # 只传分片到 R2
    python -m scripts.build_dict                     # 两步一起（本机调试用）
    python -m scripts.build_dict --top 20000
    python -m scripts.build_dict --csv /tmp/ecdict.mini.csv --build-only --index /tmp/i.json

## 与 App 仓库的关系

App 仓库（daydaynews）里有一份等价的 Node 实现 `scripts/ecdict/build-subset.mjs`，
用于本机试跑与规则说明。**权威产物由本脚本产出**——因为只有流水线
（news-etl）需要把分片上传到 R2，也只有流水线有 R2 密钥。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
log = logging.getLogger("build_dict")

ECDICT_URL = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"
# 全量文件按官方 README，约 62.9 MB。低于此值说明是中断的残文件。
ECDICT_MIN_BYTES = 50 * 1024 * 1024

POS_LABEL = {
    "n": "n.", "v": "v.", "adj": "adj.", "adv": "adv.", "prep": "prep.",
    "conj": "conj.", "pron": "pron.", "num": "num.", "art": "art.", "int": "int.",
    "aux": "aux.", "abbr": "abbr.",
}

# ECDICT 的 translation / definition 会把词性写成前缀（`n. 贸易` / `n. the exchange of...`），
# 而它自己的 `pos` 列**实测 20000 条全是空的**（不是稀疏，就是没数据）。
# 所以词性只能从释义前缀里取；下表把 ECDICT 的各种写法归一到 POS_LABEL 那套。
_TAG_ALIAS = {
    "n": "n", "v": "v", "vt": "v", "vi": "v", "aux": "aux",
    "a": "adj", "adj": "adj", "j": "adj", "s": "adj",
    "ad": "adv", "adv": "adv", "r": "adv",
    "prep": "prep", "conj": "conj", "pron": "pron", "num": "num",
    "art": "art", "int": "int", "abbr": "abbr", "pl": "n", "u": "n",
}

# 释义开头的词性标记，可有多级（如 "v. i. See Thee."）。
# 刻意只认小写：ECDICT 的标记一律小写，写成大写的是正文里的缩写或人名首字母
# （`A. Lincoln was...`），不能当成形容词吃掉。
_TAG_RE = re.compile(r"^([a-z]{1,6})\.\s*")

# 英文释义长度上限。实测 19756 条的分布：P50=48、P75=69、P90=96、P95=118、
# P99=166、最长 382 字符。词汇卡按「两行最好、顶多三行」设计，一行约 45–50 字符，
# 所以 120 字符对应三行以内，只截掉 4.5% 的长尾。
EN_MAX_CHARS = 120

# 与 App 端词名规则保持一致：小写、空格转下划线
_HEADWORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9'’\- .]*$")


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def parse_csv(text: str):
    """RFC4180 解析：支持字段内的逗号、换行与双写引号转义。"""
    field: list[str] = []
    row: list[str] = []
    in_quotes = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_quotes:
            if c == '"':
                if i + 1 < n and text[i + 1] == '"':
                    field.append('"')
                    i += 2
                    continue
                in_quotes = False
                i += 1
                continue
            field.append(c)
            i += 1
            continue
        if c == '"':
            in_quotes = True
            i += 1
            continue
        if c == ",":
            row.append("".join(field))
            field = []
            i += 1
            continue
        if c == "\r":
            i += 1
            continue
        if c == "\n":
            row.append("".join(field))
            yield row
            row = []
            field = []
            i += 1
            continue
        field.append(c)
        i += 1
    if field or row:
        row.append("".join(field))
        yield row


def normalize_pos(raw: str) -> str:
    """ECDICT 的 `pos` 列。

    两种可能形状：
    - 语料占比 `n:46/v:54` —— 取占比最高者
    - 单品词性 `n` / `vt` —— 直接归一
    注意：实测 ecdict.csv 里这一列**20000 条全为空**，真正的词性在释义前缀里，
    见 split_tag()。这里保留是为了将来上游补齐后能直接用。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "/" in raw or ":" in raw:
        best, best_pct = "", -1.0
        for part in raw.split("/"):
            part = part.strip()
            if not part:
                continue
            tag, _, pct = part.partition(":")
            try:
                value = float(pct)
            except ValueError:
                value = 0.0
            if value > best_pct:
                best_pct = value
                best = tag
        key = best.strip().lower()
    else:
        key = raw.rstrip(".").strip().lower()
    return POS_LABEL.get(_TAG_ALIAS.get(key, ""), "")


def split_tag(text: str) -> tuple[str, str]:
    """剥掉释义开头的词性标记，返回 (标准词性, 剩余文本)。

    最多剥两级：ECDICT 里有 `v. i. See Thee.` 这种双标记，再往后就不是词性了。
    标记不认识时立即停手，避免把 `A. Lincoln` 这类正文当成词性吃掉。
    """
    s = (text or "").strip()
    tag = ""
    for _ in range(2):
        m = _TAG_RE.match(s)
        if not m:
            break
        key = m.group(1).lower()
        if key not in _TAG_ALIAS:
            break
        if not tag:
            tag = POS_LABEL.get(_TAG_ALIAS[key], "")
        s = s[m.end():].strip()
    return tag, s


def clip(text: str, limit: int = EN_MAX_CHARS) -> str:
    """按词边界截断长文本。

    只截长尾（>120 字符的英文释义占 4.5%）。故意不切在单词中间，
    并在末尾加省略号说明「这里被截过」，不让读者以为释义本来就到这儿。
    """
    s = (text or "").strip()
    if len(s) <= limit:
        return s
    cut = s[: limit - 1]
    sp = cut.rfind(" ")
    if sp > 0:
        cut = cut[:sp]
    return cut.rstrip(" ,;:.") + "…"


def first_sense(text: str) -> str:
    """释义字段一条一义、以换行（或字面 \\n）分隔，只取第一条。"""
    if not text:
        return ""
    for chunk in re.split(r"\r?\n|\\n", str(text)):
        chunk = chunk.strip()
        if chunk:
            return chunk
    return ""


# ECDICT 的 phonetic 列是「IPA + 少量 ASCII / 西里尔替代」的混合记音：西里尔 schwa
# 「ә」代替「ə」、ASCII 冒号「:」代替长音号「ː」、ei/ai/au/ou/əu 代替双元音
# eɪ/aɪ/aʊ/əʊ。这里做确定性的有序替换，把能无损还原的先还原；裸 i/u 分别当作
# /iː/ /uː/（ECDICT 用 ɪ/ʊ 字符表示 KIT/FOOT，所以裸 i/u 是 FLEECE/GOOSE）。
# 这是 dictionaryapi.dev 拉不到时的兜底；真 IPA 优先走 ipa_cache（见 fetch_ipa.py）。
def to_ipa(p: str) -> str:
    if not p:
        return ""
    s = (p.replace("ә", "ə")
           .replace("ei", "eɪ").replace("ai", "aɪ").replace("au", "aʊ")
           .replace("ou", "əʊ").replace("əu", "əʊ").replace(":", "ː"))
    s = s.replace("i", "iː").replace("u", "uː")
    # 避免 iːː / uːː 这类重复长音号
    s = s.replace("iːː", "iː").replace("uːː", "uː")
    return s


def build_from_index(index_path: Path, top_n: int = 20000) -> list[dict]:
    """从已提交的 ecdict-subset.json 重建 picked（避免重新下载 62.9MB CSV）。

    用于「只重排 / 加 IPA 字段」这类不需要重解析 CSV 的重建。读取时取 phonetic_raw
    （若存在）作为 ECDICT 原记音，保证 IPA 字段可重复叠加而不污染源。
    """
    data = json.loads(Path(index_path).read_text(encoding="utf-8"))
    out: list[dict] = []
    for w, v in data.items():
        raw = v.get("phonetic_raw") or v.get("phonetic") or ""
        out.append({
            "word": v.get("word") or w,
            "phonetic": raw,
            "pos": v.get("pos") or "",
            "en": v.get("en") or "",
            "zh": v.get("zh") or "",
            "rank": 0,
        })
    out.sort(key=lambda e: e["word"])
    return out[:top_n]


def download_csv(dest: Path) -> Path:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    log.info("下载 ECDICT 全量 CSV（约 62.9 MB）→ %s", dest)
    t0 = time.time()
    with httpx.stream(
        "GET", ECDICT_URL, follow_redirects=True, timeout=httpx.Timeout(180.0, connect=30.0)
    ) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and done % (10 << 20) < (1 << 20):
                    log.info("  ...%.0f/%.0f MB", done / 1048576, total / 1048576)
    size = tmp.stat().st_size
    if size < ECDICT_MIN_BYTES:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"下载不完整：只拿到 {size / 1048576:.1f} MB（预期 >50 MB）。上游或网络异常。"
        )
    tmp.replace(dest)
    log.info("下载完成 %.1f MB，用时 %.1fs", size / 1048576, time.time() - t0)
    return dest


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #
def build(csv_path: Path, top_n: int) -> tuple[list[dict], list[str]]:
    text = csv_path.read_text(encoding="utf-8", errors="replace")
    rows = parse_csv(text)
    header = [h.strip() for h in (next(rows, None) or [])]
    try:
        idx = {name: header.index(name) for name in
               ("word", "phonetic", "definition", "translation", "pos", "bnc", "frq")}
    except ValueError as exc:
        raise RuntimeError(f"ecdict.csv 缺少必需列（{exc}）：{','.join(header)}") from exc

    entries: list[dict] = []
    scanned = 0
    for row in rows:
        scanned += 1
        if len(row) <= idx["word"]:
            continue
        word = row[idx["word"]].strip()
        # 挡掉数字、纯标点、超长短语等噪声词头
        if not word or not _HEADWORD_RE.match(word) or len(word) > 32:
            continue

        def cell(name: str) -> str:
            i = idx[name]
            return row[i].strip() if i < len(row) else ""

        phonetic = cell("phonetic")
        zh_raw = first_sense(cell("translation"))
        en_raw = first_sense(cell("definition"))

        # 词性：上游 pos 列实测全空，实际藏在释义前缀里（`n. 贸易` / `n. the exchange of...`）。
        # 剥掉前缀后，中文行与英文行都不再带 "n."，词性单独占 pos 字段。
        zh_tag, zh_body = split_tag(zh_raw)
        en_tag, en_body = split_tag(en_raw)
        zh = zh_body or zh_raw
        en = clip(en_body or en_raw)
        pos = normalize_pos(cell("pos")) or zh_tag or en_tag

        if not (phonetic or en or zh):
            continue

        # 词频：当代语料库 frq 优先，缺失时回退 BNC（推到 1e6 之后，排在 frq 命中者之后）
        try:
            frq = float(cell("frq"))
        except ValueError:
            frq = 0.0
        try:
            bnc = float(cell("bnc"))
        except ValueError:
            bnc = 0.0
        rank = frq if frq > 0 else (bnc + 1e6 if bnc > 0 else float("inf"))

        entries.append({
            "word": word, "phonetic": phonetic, "pos": pos,
            "en": en, "zh": zh, "rank": rank,
        })

    entries.sort(key=lambda e: e["rank"])
    picked = entries[:top_n]
    log.info("扫描 %d 行，可用 %d 条，取前 %d 条", scanned, len(entries), len(picked))
    return picked, header


def shard_of(word: str) -> str:
    """词条分片键 = 后两字符归一化后的前两个字符。

    **为什么分片而不是一词一对象**：一词一对象意味着两万次 PUT（外加一次 HEAD
    预检就是四万次），实测跑 30 分钟才传到字母 a，CI 的 45 分钟上限根本跑不完。
    把同一前缀的词打进一个 shard，对象数降到几百个，上传时间从小时级降到分钟级。
    Worker 侧一次 GET 拿到 shard 再本地取词，代价是多读几 KB。

    归一化规则必须与 Worker（workers/dict-api/src/index.mjs）完全一致：
    非 [a-z0-9] 的字符（`'`、`-`、空格等）一律映射成 `_`，不足两位补 `_`。
    """
    w = (word or "").lower()

    def ch(i: int) -> str:
        c = w[i] if i < len(w) else "_"
        return c if ("a" <= c <= "z" or "0" <= c <= "9") else "_"

    return ch(0) + ch(1)


def write_outputs(picked: list[dict], out_dir: Path, index_path: Path, ipa_cache: dict | None = None) -> tuple[Path, Path]:
    """产出两样东西：分片文件（上传 R2）与单文件索引（提交进仓库）。

    `phonetic` 优先用 dictionaryapi.dev 的真 IPA（来自 ipa_cache，见 fetch_ipa.py）；
    拉不到的词回退到 ECDICT 记音经 `to_ipa()` 转换。`phonetic_raw` 始终保留 ECDICT
    原值，便于追查与回滚。
    """
    shard_dir = out_dir / "shards"
    if shard_dir.exists():
        for old in shard_dir.glob("*.json"):
            old.unlink()
    shard_dir.mkdir(parents=True, exist_ok=True)

    index: dict[str, dict] = {}
    shards: dict[str, dict] = {}
    for e in picked:
        key = e["word"].lower().replace(" ", "_")
        raw = e.get("phonetic") or ""
        ipa = (ipa_cache or {}).get(key) or to_ipa(raw)
        payload = {
            "word": e["word"],
            "phonetic": ipa,
            "phonetic_raw": raw,
            "pos": e.get("pos") or "",
            "en": e.get("en") or "",
            "zh": e.get("zh") or "",
        }
        # 索引与分片用同一个查找键：小写、空格转下划线
        index[key] = payload
        shards.setdefault(shard_of(key), {})[key] = payload

    for name, bucket in shards.items():
        (shard_dir / f"{name}.json").write_bytes(
            json.dumps(bucket, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

    index_path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    index_path.write_bytes(blob)
    biggest = max((len(v) for v in shards.values()), default=0)
    log.info(
        "写出 %d 词 → %d 个分片（%s）；索引 %s（%.2f MB）；最大分片 %d 词",
        len(index), len(shards), shard_dir, index_path, len(blob) / 1048576, biggest,
    )
    return shard_dir, index_path


# --------------------------------------------------------------------------- #
# 上传 R2
# --------------------------------------------------------------------------- #
def upload(shard_dir: Path, workers: int) -> int:
    from app.dict import get_dict_storage

    store = get_dict_storage()
    if store is None:
        log.error("未配置 R2（R2_BUCKET / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT）")
        return 0

    files = sorted(shard_dir.glob("*.json"))
    if not files:
        log.error("没有分片可传：%s", shard_dir)
        return 0
    total_mb = sum(f.stat().st_size for f in files) / 1048576
    log.info("上传 %d 个分片（共 %.1f MB）到 %s，并发 %d",
             len(files), total_mb, store.name, workers)
    t0 = time.time()
    done = ok = 0
    failures: list[str] = []

    def one(p: Path) -> tuple[bool, str]:
        """无条件覆盖写。

        **不用「已存在就跳过」**：分片名恰好两个字符，会和早期「一词一对象」
        布局里的两字母词（in / on / at / to …）撞名。撞上时 exists() 返回真，
        真正的分片就永远写不进去，线上读到的是那个单词的旧对象 —— 实测踩过：
        45 个两字母分片全被跳过，dict/en/in.json 里躺着单词 in 的旧对象。
        PUT 本身是幂等的，277 个分片全量重写只要 32 秒，不值得为省这点时间冒风险。
        """
        key = f"dict/en/{p.name}"
        try:
            store.put(key, p.read_bytes(), content_type="application/json")
            if not store.exists(key):
                return False, f"{p.name}: 上传后探测不到"
            return True, p.name
        except Exception as exc:  # noqa: BLE001
            return False, f"{p.name}: {exc}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for good, detail in pool.map(one, files):
            done += 1
            if good:
                ok += 1
            else:
                failures.append(detail)
            if done % 100 == 0:
                log.info("  ...%d/%d（%.0fs）", done, len(files), time.time() - t0)

    log.info("上传完成：成功 %d，失败 %d，用时 %.0fs", ok, len(failures), time.time() - t0)
    for line in failures[:10]:
        log.warning("  ✗ %s", line)
    return ok


def prune_legacy(store) -> int:
    """清掉早期「一词一对象」布局残留的 key。

    第一版是一词一个对象（`dict/en/trade.json`），在 runner 上实测约 0.4s/次往返，
    两万词连 HEAD 预检要半小时以上，跑不完，已改为分片（`dict/en/tr.json`）。
    旧对象没人再读，但会干扰排查（"这个 trade.json 为什么还在"），顺手清掉。

    判定规则很精确：shard 的文件名**恰好两个字符**，其余都是旧布局。
    """
    keys = [k for k in store.list_keys("dict/en/") if k.endswith(".json")]
    legacy = [
        k for k in keys
        if len(k.rsplit("/", 1)[-1][: -len(".json")]) != 2
    ]
    if not legacy:
        log.info("没有需要清理的旧布局对象")
        return 0
    log.info("清理 %d 个旧布局对象（示例：%s）", len(legacy), ", ".join(legacy[:3]))
    t0 = time.time()

    # 必须走批量接口：逐个 delete_object 在 runner 上约 0.4s 一次，
    # 几千个就是几十分钟 —— 实测把一次 workflow 卡到超时。
    delete_many = getattr(store, "delete_many", None)
    if callable(delete_many):
        removed = delete_many(legacy)
    else:
        removed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            for ok in pool.map(lambda k: _try_delete(store, k), legacy):
                removed += 1 if ok else 0

    log.info("已清理 %d 个，用时 %.0fs", removed, time.time() - t0)
    return removed


def _try_delete(store, key: str) -> bool:
    try:
        store.delete(key)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("  ✗ 删除 %s 失败: %s", key, exc)
        return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="构建 ECDICT 高频子集并上线 R2")
    ap.add_argument("--csv", default=None,
                    help="全量 ecdict.csv 路径。不传则用 dist/ecdict.csv，"
                         "缺失或不足 50MB 时自动下载（显式传入的路径按原样使用，不校验完整性）")
    ap.add_argument("--out", default=str(REPO / "dist" / "dict"), help="分片输出目录")
    ap.add_argument("--index", default=str(REPO / "data" / "dict" / "ecdict-subset.json"),
                    help="单文件索引输出路径（会提交进仓库）")
    ap.add_argument("--top", type=int, default=20000, help="按词频取前 N 个词")
    ap.add_argument("--from-index", action="store_true",
                    help="从已提交的 ecdict-subset.json 重建（不重新下载 62.9MB CSV），用于加 IPA 字段")
    ap.add_argument("--ipa-cache", default=str(REPO / "data" / "dict" / "ipa.json"),
                    help="dictionaryapi.dev 真 IPA 缓存（见 scripts/fetch_ipa.py）")
    ap.add_argument("--build-only", action="store_true",
                    help="只构建，不传 R2（先落索引、后传 R2 的两段式流程用这个）")
    ap.add_argument("--upload-only", action="store_true",
                    help="跳过下载与解析，只把 --out 里已有的分片传上去")
    ap.add_argument("--prune-legacy", action="store_true",
                    help="清掉早期「一词一对象」布局残留的 key（保留两字符的 shard）")
    ap.add_argument("--workers", type=int, default=8, help="上传并发数")
    args = ap.parse_args()

    if args.build_only and args.upload_only:
        log.error("--build-only 与 --upload-only 不能同时用")
        return 2

    out_dir = Path(args.out)
    index_path = Path(args.index)

    # 真 IPA 缓存（dictionaryapi.dev，见 scripts/fetch_ipa.py）；没有就全回退到
    # ECDICT 记音经 to_ipa() 转换。缓存里查不到的词也同样回退。
    ipa_cache: dict = {}
    if Path(args.ipa_cache).is_file():
        try:
            ipa_cache = json.loads(Path(args.ipa_cache).read_text(encoding="utf-8"))
            log.info("载入 IPA 缓存 %d 条", len(ipa_cache))
        except Exception as exc:  # noqa: BLE001
            log.warning("[dict] IPA 缓存读取失败 %s：%s", args.ipa_cache, exc)
    else:
        log.info("无 IPA 缓存（%s），音标将用 ECDICT 记音经 to_ipa() 转换", args.ipa_cache)

    # 两段式：先 build 落索引（快、必成），再 upload（慢、可重试）。
    # 这样即使上传超时，索引也已经提交，App 侧的富化不受影响。
    if args.upload_only:
        from app.dict import get_dict_storage

        store = get_dict_storage()
        if store is None:
            log.error("未配置 R2（R2_BUCKET / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT）")
            return 1
        if args.prune_legacy:
            prune_legacy(store)
        log.info("--upload-only：跳过构建，直接上传 %s", out_dir / "shards")
        files = sorted((out_dir / "shards").glob("*.json"))
        if not files:
            log.error("没有分片可传：%s", out_dir / "shards")
            return 1
        return 0 if upload(out_dir / "shards", args.workers) else 1

    if args.from_index:
        # 从已提交的索引重建：不重新下载 62.9MB CSV，仅叠加 IPA 字段用。
        if not index_path.is_file():
            log.error("--from-index 但索引不存在：%s", index_path)
            return 1
        log.info("从索引重建（--from-index）：%s", index_path)
        picked = build_from_index(index_path, args.top)
    elif args.csv:
        csv_path = Path(args.csv)
        if not csv_path.is_file():
            log.error("指定的 CSV 不存在：%s", csv_path)
            return 1
        log.info("使用指定 CSV：%s（%.1f MB）", csv_path, csv_path.stat().st_size / 1048576)
        picked, header = build(csv_path, args.top)
        if not picked:
            log.error("没解析出任何词条，检查 CSV 列名：%s", ",".join(header))
            return 1
    else:
        csv_path = REPO / "dist" / "ecdict.csv"
        if not csv_path.is_file() or csv_path.stat().st_size < ECDICT_MIN_BYTES:
            csv_path = download_csv(csv_path)
        picked, header = build(csv_path, args.top)
        if not picked:
            log.error("没解析出任何词条，检查 CSV 列名：%s", ",".join(header))
            return 1

    shard_dir, index_path = write_outputs(picked, out_dir, index_path, ipa_cache)

    # 记账：索引指纹，便于线上核对「App 查不到的词」是不是索引太旧
    digest = hashlib.sha256(index_path.read_bytes()).hexdigest()[:12]
    log.info("索引指纹 %s，共 %d 词", digest, len(picked))

    if args.build_only:
        log.info("--build-only：跳过 R2 上传")
        return 0

    return 0 if upload(shard_dir, args.workers) else 1


if __name__ == "__main__":
    sys.exit(main())
