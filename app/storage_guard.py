"""存储容量护栏：在写入前检查，超阈值直接拒绝，防止意外撑爆 R2 免费额度。

背景
----
R2 免费额度 10GB。超出后按 $0.015/GB/月 计费。虽不可能跑满，但一旦代码
被改坏（比如某个 key 生成逻辑异常导致文件重复堆积），就该在这里拦住。

策略
----
`max_total_gb` 默认 8GB（留 20% 余量），超过则拒绝写入新对象并抛
StorageQuotaExceeded；已存在对象仍可读，保证线上内容不受影响。
"""
from __future__ import annotations

import logging

from .storage import get_storage

log = logging.getLogger("guard")

GB =1024 ** 3
DEFAULT_LIMIT_GB = 8.0
# 缓存总用量，避免频繁 list（list 是B 类操作）
_cache: dict[str, float] = {}


class StorageQuotaExceeded(RuntimeError):
    """存储超过配置上限。"""


def _list_all_sizes(st) -> int:
    """遍历所有对象求总大小。用 list_objects 的 Size 字段，不下载内容。"""
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
    """当前存储占用（GB）。"""
    st = get_storage()
    if getattr(st, "provider", st.name) not in ("r2", "b2"):
        return 0.0
    if refresh or "usage" not in _cache:
        try:
            _cache["usage"] = _list_all_sizes(st) / GB
            _cache["at"] = __import__("time").time()
            log.info("[guard] R2 当前占用 %.3f GB", _cache["usage"])
        except Exception as exc:  # noqa: BLE001
            log.warning("[guard] 读取占用失败，跳过检查: %s", exc)
            return _cache.get("usage", 0.0)
    return _cache["usage"]


def check_capacity(limit_gb: float = DEFAULT_LIMIT_GB) -> None:
    """写入前调用。超限则抛 StorageQuotaExceeded。"""
    st = get_storage()
    if getattr(st, "provider", st.name) not in ("r2", "b2"):
        return
    used = current_usage_gb()
    if used >= limit_gb:
        raise StorageQuotaExceeded(
            f"R2 存储占用 {used:.2f}GB 已达上限 {limit_gb}GB，拒绝写入新对象。"
            "请清理历史音频或调高 limit_gb。"
        )
    if used >= limit_gb * 0.8:
        log.warning("[guard] R2 占用已达 %.0f%%（%.2f/%.1f GB），接近上限", used / limit_gb * 100, used, limit_gb)


def auto_clean(keep_days: int = 60) -> dict[str, int]:
    """删除 keep_days 天前的音频，返回统计。仅在本地/CI 手动调用。"""
    import datetime as dt

    st = get_storage()
    if getattr(st, "provider", st.name) not in ("r2", "b2"):
        return {"deleted": 0, "bytes": 0}

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)
    cutoff_ms = cutoff.timestamp() * 1000
    deleted = 0
    freed = 0

    for key in st.list_keys("audio/"):
        try:
            obj = st.client.head_object(Bucket=st.bucket, Key=key)
            if obj.get("LastModified") and obj["LastModified"].timestamp() * 1000 < cutoff_ms:
                size = int(obj.get("ContentLength", 0))
                st.delete(key)
                deleted += 1
                freed += size
        except Exception as exc:  # noqa: BLE001
            log.warning("[guard] 清理 %s 失败: %s", key, exc)

    _cache.pop("usage", None)
    log.info("[guard] 清理完成：删除 %d 个文件，释放 %.1f MB", deleted, freed / 1024 / 1024)
    return {"deleted": deleted, "bytes": freed}