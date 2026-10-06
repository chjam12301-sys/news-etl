"""Supabase Storage 实现（REST API，无需 SDK）。

为什么换 Supabase
------------------
- Cloudflare R2 的 S3 API 域名在部分网络不可达；
- Backblaze B2 的 applicationKeyId 格式校验严格、易踩坑；
- Supabase 用 **Bearer token** 认证，最简单；且与 Neon 同一家，
  Storage + Database 一次搞定，新加坡节点国内访问尚可。

免费额度：1 GB 存储 + 5 GB 出网/ 带宽，超出收费。故必须配好容量护栏。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import StoredObject

log = logging.getLogger("supabase")


@dataclass(slots=True)
class SupabaseStorage:
    """Supabase Storage（REST API + service_role key）。"""

    name = "supabase"
    provider = "supabase"

    def __init__(self, bucket: str, project_url: str, service_key: str, *, public: bool = True) -> None:
        self.bucket = bucket
        self.project_url = project_url.rstrip("/")
        self.key = service_key
        self.public = public

        import httpx

        self._httpx = httpx
        self._client = httpx.Client(
            timeout=60,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {service_key}",
                "apikey": service_key,
                "Content-Type": "application/json",
            },
        )
        # 公开访问地址（bucket 为 public 时可用）
        self.public_base = f"{self.project_url}/storage/v1/object/public/{bucket}"

    # ---- 内部 ---------------------------------------------------------- #
    def _url(self, key: str) -> str:
        return f"{self.project_url}/storage/v1/object/{self.bucket}/{key}"

    def _req(self, method: str, url: str, **kw):
        last = None
        for attempt in range(3):
            try:
                r = self._client.request(method, url, **kw)
                if r.status_code in (429, 500, 502, 503):
                    last = f"HTTP {r.status_code}"
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return r
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"[supabase] {method} 失败: {last}")

    # ---- Storage 协议 --------------------------------------------------- #
    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        files = {"file": (key.rsplit("/", 1)[-1], data, content_type or "application/octet-stream")}
        r = self._req("POST", self._url(key), files=files)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"[supabase] 上传 {key} 失败: HTTP {r.status_code} {r.text[:200]}")
        log.info("[supabase] 上传成功 %s (%d 字节)", key, len(data))
        return StoredObject(key=key, size=len(data), url=self.public_url(key))

    def exists(self, key: str) -> bool:
        r = self._client.head(self._url(key))
        return r.status_code == 200

    def read(self, key: str) -> bytes | None:
        r = self._req("GET", self._url(key))
        return r.content if r.status_code == 200 else None

    def delete(self, key: str) -> None:
        self._client.delete(self._url(key))

    def list_keys(self, prefix: str = "") -> list[str]:
        """列举对象。Supabase 用 POST + body 传prefix。"""
        out: list[str] = []
        folder = prefix.rstrip("/")
        payload: dict[str, Any] = {"limit": 1000}
        if folder:
            payload["prefix"] = folder

        # Storage list API 在 body 里传 prefix
        r = self._client.post(
            f"{self.project_url}/storage/v1/object/list/{self.bucket}",
            json={**payload, "prefix": folder} if folder else payload,
        )
        if r.status_code != 200:
            # 回退：逐目录列举
            return self._list_by_folder(folder) if folder else []

        for item in r.json() or []:
            name = item.get("name") if isinstance(item, dict) else item
            if name:
                out.append(f"{folder}/{name}" if folder else name)
        return out

    def _list_by_folder(self, folder: str) -> list[str]:
        """Storage API 返回的是「目录下一层」条目，递归展开。"""
        r = self._client.post(
            f"{self.project_url}/storage/v1/object/list/{self.bucket}",
            json={"prefix": folder, "limit": 1000},
        )
        if r.status_code != 200:
            return []
        out: list[str] = []
        for item in r.json() or []:
            name = item.get("name") if isinstance(item, dict) else item
            if not name:
                continue
            full = f"{folder}/{name}" if folder else name
            out.append(full)
        return out

    def public_url(self, key: str) -> str:
        return f"{self.public_base}/{key}"

    def iter_prefix(self, prefix: str) -> list[StoredObject]:
        return [StoredObject(key=k) for k in self.list_keys(prefix)]

    # ---- 容量统计（护栏用） --------------------------------------------- #
    def used_bytes(self) -> int:
        """Supabase 不直接返回总用量，遍历 bucket 累加。"""
        r = self._client.post(
            f"{self.project_url}/storage/v1/object/list/{self.bucket}",
            json={"limit": 1000, "prefix": ""},
        )
        if r.status_code != 200:
            return 0
        items = r.json() or []
        total = 0
        for it in items:
            if isinstance(it, dict):
                meta = it.get("metadata") or {}
                total += int(meta.get("size") or it.get("size") or 0)
        return total