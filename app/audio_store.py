"""音频托管：音频**不进 Git**，只进对象存储。

## 为什么要拆出来

jsDelivr 对单个 GitHub 仓库有 **50MB 硬上限**。超限后，已被缓存的旧路径还能
取，但**任何新路径**都会返回 403：

    Package size exceeded the configured limit of 50 MB

实测本仓库：content/ 231MB，其中音频 214MB，JSON 只有 17MB。音频是唯一超标项，
也是唯一必须搬走的东西 —— JSON 留在 Git 才能继续用 jsDelivr 的 `@<commit>`
不可变版本能力（App 的版本发现与 content_hash 比对都依赖它）。

## 设计要点

- 与 JSON 存储（`get_storage()`，走 GitHub）彻底解耦：音频**永不**写入 Git。
- 对外 URL 由配置给出，**不含 commit SHA**，永久稳定。内容更新靠
  `content_hash` 驱动，而不是换 URL —— App 的缓存逻辑无需改动。
- 未配置对象存储时回退本地目录，但其 URL 不是 http；发布校验会据此判失败
  并保留上一份可用索引，**绝不静默产出坏链接**。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .config import settings
from .storage import LocalStorage, R2Storage, StoredObject, audio_key

log = logging.getLogger(__name__)

# 合法的音频对象 key：audio/<article_id>/<level>.<ext>
_AUDIO_KEY_RE = re.compile(r"^audio/\d+/[A-Za-z0-9_-]+\.(mp3|wav|m4a)$")


class _LocalAudioStorage(LocalStorage):
    """本地兜底（仅开发用）。配了 public base 就拼绝对地址，否则给相对路径。"""

    name = "local-audio"

    def __init__(self, root: Any, public_base: str = "") -> None:
        super().__init__(root)
        self.public_base = (public_base or "").rstrip("/")

    def public_url(self, key: str) -> str:
        return f"{self.public_base}/{key}" if self.public_base else f"/files/{key}"


def audio_base_url() -> str:
    """音频对外基址。

    优先级：AUDIO_PUBLIC_BASE（显式）> R2 > B2 > Supabase。
    全部未配置时返回 ""，调用方必须据此判断「不能发布」。
    """
    for v in (
        getattr(settings, "audio_public_base", "") or "",
        getattr(settings, "r2_public_base", "") or "",
        getattr(settings, "b2_public_base", "") or "",
    ):
        v = (v or "").strip().rstrip("/")
        if v:
            return v
    # Supabase 的公开地址是固定格式，可推导
    url = (getattr(settings, "supabase_project_url", "") or "").strip().rstrip("/")
    key = (getattr(settings, "supabase_service_key", "") or "").strip()
    bucket = (getattr(settings, "supabase_bucket", "") or "").strip()
    if url and key and bucket:
        return f"{url}/storage/v1/object/public/{bucket}"
    return ""


def audio_object_key(file_path: str = "", article_id: int = 0, level_code: str = "") -> str:
    """取对象 key。历史 file_path 合规就直接用（保留扩展名与层级）。"""
    fp = (file_path or "").strip().lstrip("/")
    if _AUDIO_KEY_RE.match(fp):
        return fp
    return audio_key(article_id, level_code)


def public_audio_url(
    *, file_path: str = "", article_id: int = 0, level_code: str = ""
) -> str:
    """拼对外可访问的音频 URL；未配置对象存储时返回 ""。"""
    base = audio_base_url()
    if not base:
        return ""
    return f"{base}/{audio_object_key(file_path, article_id, level_code)}"


_audio_storage: Any = None


def _try_r2() -> Any | None:
    bucket = getattr(settings, "r2_bucket", "")
    key_id = getattr(settings, "r2_access_key_id", "")
    secret = getattr(settings, "r2_secret_access_key", "")
    endpoint = getattr(settings, "r2_endpoint", "")
    if not (bucket and key_id and secret and endpoint):
        return None
    return R2Storage(
        bucket=bucket,
        access_key=key_id,
        secret_key=secret,
        endpoint=endpoint,
        public_base=audio_base_url(),
        provider="r2",
    )


def _try_b2() -> Any | None:
    bucket = getattr(settings, "b2_bucket", "")
    key_id = getattr(settings, "b2_access_key_id", "")
    secret = getattr(settings, "b2_secret_access_key", "")
    endpoint = getattr(settings, "b2_endpoint", "")
    if not (bucket and key_id and secret and endpoint):
        return None
    return R2Storage(
        bucket=bucket,
        access_key=key_id,
        secret_key=secret,
        endpoint=endpoint,
        public_base=audio_base_url(),
        provider="b2",
    )


def _try_supabase() -> Any | None:
    url = getattr(settings, "supabase_project_url", "")
    key = getattr(settings, "supabase_service_key", "")
    bucket = getattr(settings, "supabase_bucket", "")
    if not (url and key and bucket):
        return None
    from .storage_supabase import SupabaseStorage

    return SupabaseStorage(bucket=bucket, project_url=url, service_key=key)


def get_audio_storage() -> Any:
    """音频专用存储。**候选里不含 GitHub** —— 这是本模块存在的全部意义。

    顺序：R2 → B2 → Supabase → 本地（仅开发）。
    """
    global _audio_storage
    if _audio_storage is not None:
        return _audio_storage

    for factory in (_try_r2, _try_b2, _try_supabase):
        try:
            st = factory()
        except Exception as exc:  # noqa: BLE001
            log.warning("[audio] %s 初始化失败: %s", factory.__name__, exc)
            continue
        if st is not None:
            _audio_storage = st
            log.info("[audio] 音频托管：%s（base=%s）", st.name, audio_base_url() or "-")
            return _audio_storage

    st = _LocalAudioStorage(settings.audio_dir, audio_base_url())
    _audio_storage = st
    log.warning(
        "[audio] 未配置对象存储，音频落到本地 %s —— 其 URL 非 http，发布会被拦下",
        settings.audio_dir,
    )
    return _audio_storage


def reset_audio_storage() -> None:
    """仅供测试用。"""
    global _audio_storage
    _audio_storage = None


def upload_audio(data: bytes, key: str, *, content_type: str = "audio/mpeg") -> StoredObject:
    """上传音频并返回对象信息（含对外 URL）。"""
    return get_audio_storage().put(key, data, content_type=content_type)
