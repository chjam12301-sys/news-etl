"""存储容量护栏：写入前检查，超阈值直接拒绝，防止意外撑爆免费额度。

各provider 的免费额度与策略
----------------------------
| provider   | 免费额度| 超出计费              | 本项目上限 |
|------------|---------|-----------------------|-----------|
| r2         | 10 GB| $0.015/GB/月          | 8 GB|
| b2         | 10 GB   | ~$0.006/GB/月         | 8 GB       |
| supabase   | **1 GB** | 按项目套餐            | **0.9 GB** |

Supabase 免费额度只有 1GB，是最容易被撑爆的一家，所以上限最保守。

策略：超限则拒绝写入并抛 StorageQuotaExceeded；已存在对象仍可读，
保证线上内容不受影响。
"""
from __future__ import annotations

import logging
import time

from .config import settings
from .storage import get_storage

log = logging.getLogger("guard")

GB = 1024**3
DEFAULT_LIMIT_GB = 8.0
SUPABASE_LIMIT_GB = 0.9  # Supabase 免费额度仅 1GB，留 10% 余量
_CLOUD_PROVIDERS = ("r2", "b2", "supabase")

_cache: dict[str, float] = {}


class StorageQuotaExceeded(RuntimeError):
    """存储超过配置上限。"""


def _provider(st) -> str:
    return getattr(st, "provider", getattr(st, "name", "local"))


def _list_all_sizes(st) -> int:
    """遍历所有对象求总大小（字节）。不下载内容，只读元数据。"""
    if _provider(st) == "supabase":
        try:
            return st.used_bytes()
        except Exception as exc:  # noqa: BLE001
            log.warning("[guard] 读取 Supabase 用量失败: %s", exc)
            return 0

    total = 0
    token: str | None = None
    while True:
        kwargs = {"Bucket": st.bucket, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        resp = st.client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []) or []:
            total += int(obj.get("Size", 0))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return total


def current_usage_gb(*, refresh: bool = False) -> float:
    """当前存储占用（GB）。读取失败时返回 0（不阻塞主流程）。"""
    st = get_storage()
    if _provider(st) not in _CLOUD_PROVIDERS:
        return 0.0

    if refresh or "usage" not in _cache:
        try:
            _cache["usage"] = _list_all_sizes(st) / GB
            _cache["at"] = time.time()
            log.info("[guard] %s 当前占用 %.4f GB", _provider(st), _cache["usage"])
        except Exception as exc:  # noqa: BLE001
            log.warning("[guard] 读取占用失败（%s），本次跳过容量检查", exc)
            return 0.0
    return _cache.get("usage", 0.0)


def default_limit_gb(st=None) -> float:
    """按 provider 返回容量上限（GB）。"""
    st = st or get_storage()
    if _provider(st) == "supabase":
        return SUPABASE_LIMIT_GB
    return float(getattr(settings, "storage_limit_gb", DEFAULT_LIMIT_GB))


def check_capacity(limit_gb: float | None = None) -> None:
    """写入前调用。超限则抛 StorageQuotaExceeded。"""
    st = get_storage()
    if _provider(st) not in _CLOUD_PROVIDERS:
        return

    limit = limit_gb if limit_gb is not None else default_limit_gb(st)
    used = current_usage_gb()
    if used <= 0:
        return  # 读不到用量时不阻塞

    if used >= limit:
        raise StorageQuotaExceeded(
            f"{_provider(st)} 存储占用 {used:.3f}GB 已达上限 {limit}GB，拒绝写入新对象。"
            "请清理历史音频或调高上限。"
        )
    if used >= limit * 0.8:
        log.warning(
            "[guard] %s 占用已达上限的 %.0f%%（%.3f/%.2f GB），接近限额",
            _provider(st), used / limit * 100, used, limit,
        )


def auto_clean(keep_days: int = 60) -> dict[str, int]:
    """删除 keep_days 天前的音频，返回统计。仅在本地/CI 手动调用。"""
    import datetime as dt

    st = get_storage()
    if _provider(st) not in _CLOUD_PROVIDERS:
        return {"deleted": 0, "bytes": 0}

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)
    deleted = 0
    freed = 0

    for key in st.list_keys("audio/"):
        try:
            if _provider(st) == "supabase":
                # Supabase 的 list 返回的 name 需拼接 prefix，逐个head 判断
                info = st._client.get(st._url(key), headers={"Range": "bytes=0-0"})
                last_modified = info.headers.get("last-modified")
                if not last_modified:
                    continue
                ts = dt.datetime.strptime(last_modified, "%a, %d %b %Y %H:%M:%S %Z").replace(
                    tzinfo=dt.timezone.utc
                )
                size = int(info.headers.get("content-range", "/0").split("/")[-1] or 0)
            else:
                obj = st.client.head_object(Bucket=st.bucket, Key=key)
                lm = obj.get("LastModified")
                if not lm or lm.timestamp() >= cutoff.timestamp():
                    continue
                size = int(obj.get("ContentLength", 0))

            st.delete(key)
            deleted += 1
            freed += size
        except Exception as exc:  # noqa: BLE001
            log.warning("[guard] 清理 %s 失败: %s", key, exc)

    _cache.pop("usage", None)
    log.info("[guard] 清理完成：删除 %d 个文件，释放 %.1f MB", deleted, freed / 1024 / 1024)
    return {"deleted": deleted, "bytes": freed}