"""存储抽象：本地磁盘 / Cloudflare R2（S3 兼容）双实现。

设计要点
--------
- 本地开发：默认走LocalStorage，零依赖、零配置
- 云端：设置 R2_ENDPOINT + R2_BUCKET + R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY
  即自动切换到 R2Storage。API 与流水线共用同一套接口代码。
- 未配置 R2 时自动回退本地，避免线上因漏配密钥而全盘失败。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import settings

log = logging.getLogger(__name__)


@dataclass(slots=True)
class StoredObject:
    key: str
    size: int = 0
    url: str | None = None


class Storage(Protocol):
    """最小存储协议：只需 put / exists / read / public_url / list_keys。"""

    name: str

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject: ...

    def exists(self, key: str) -> bool: ...

    def read(self, key: str) -> bytes | None: ...

    def delete(self, key: str) -> None: ...

    def list_keys(self, prefix: str = "") -> list[str]: ...

    def public_url(self, key: str) -> str: ...

    def iter_prefix(self, prefix: str) -> list[StoredObject]: ...


class LocalStorage:
    """本地文件系统实现。"""

    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        """把 key 映射到 root 内的安全路径。

        单纯str.replace('..', '') 不够：key 以 '/' 开头时 `self.root / safe`
        会变成绝对路径而逃出 root。这里逐段校验，任何越界都直接夹回root。
        """
        parts: list[str] = []
        for seg in key.replace("\\", "/").split("/"):
            if not seg or seg == ".":
                continue
            if seg == "..":
                # 尝试回退一级；已在根则丢弃
                if parts:
                    parts.pop()
                continue
            parts.append(seg)
        return self.root.joinpath(*parts) if parts else self.root

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return StoredObject(key=key, size=len(data), url=f"/files/{key}")

    def exists(self, key: str) -> bool:
        return self._p(key).is_file()

    def read(self, key: str) -> bytes | None:
        p = self._p(key)
        return p.read_bytes() if p.is_file() else None

    def delete(self, key: str) -> None:
        p = self._p(key)
        if p.is_file():
            p.unlink()

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self._p(prefix)
        if not base.exists():
            return []
        if base.is_file():
            return [prefix]
        root_len = len(self.root.parts)
        return [
            str(f.relative_to(self.root).as_posix())
            for f in base.rglob("*")
            if f.is_file()
        ]

    def public_url(self, key: str) -> str:
        return f"/files/{key}"

    def iter_prefix(self, prefix: str) -> list[StoredObject]:
        return [
            StoredObject(key=k, size=self._p(k).stat().st_size)
            for k in self.list_keys(prefix)
        ]


class R2Storage:
    """Cloudflare R2（S3 兼容）实现，使用 boto3。

    R2 免费额度：10GB 存储/月 + 100万次 ClassA + 1000万次 ClassB + 出网免费。
    """

    name = "r2"

    def __init__(
        self,
        bucket: str,
        access_key: str,
        secret_key: str,
        endpoint: str,
        *,
        public_base: str = "",
        region: str = "auto",
    ) -> None:
        import boto3  # 延迟导入：本地开发无需安装

        self.bucket = bucket
        self.public_base = public_base.rstrip("/")
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        extra: dict = {"CacheControl": "public, max-age=31536000, immutable"}
        if content_type:
            extra["ContentType"] = content_type
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return StoredObject(key=key, size=len(data), url=self.public_url(key))

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def read(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
            return obj["Body"].read()
        except ClientError:
            return None

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def list_keys(self, prefix: str = "") -> list[str]:
        out: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []) or []:
                out.append(obj["Key"])
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return out

    def public_url(self, key: str) -> str:
        """R2 有两种对外方式：
        1) 配了公开域名（public_base）→ 直接拼 URL，最快
        2) 未配置 → 用 Presign 签名 URL，有效期 7 天
        """
        if self.public_base:
            return f"{self.public_base}/{key}"
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=604800,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[r2] 签名失败: %s", exc)
            return key

    def iter_prefix(self, prefix: str) -> list[StoredObject]:
        return [StoredObject(key=k) for k in self.list_keys(prefix)]


_storage: Storage | None = None


def get_storage() -> Storage:
    """按配置返回存储实现；未配 R2 则用本地。"""
    global _storage
    if _storage is not None:
        return _storage

    bucket = getattr(settings, "r2_bucket", "")
    key_id = getattr(settings, "r2_access_key_id", "")
    secret = getattr(settings, "r2_secret_access_key", "")
    endpoint = getattr(settings, "r2_endpoint", "")

    if bucket and key_id and secret and endpoint:
        try:
            _storage = R2Storage(
                bucket=bucket,
                access_key=key_id,
                secret_key=secret,
                endpoint=endpoint,
                public_base=getattr(settings, "r2_public_base", "") or "",
            )
            log.info("[storage] 使用 R2, bucket=%s", bucket)
            return _storage
        except Exception as exc:  # noqa: BLE001
            log.error("[storage] R2 初始化失败(%s)，回退本地", exc)

    _storage = LocalStorage(settings.data_path / "storage")
    log.info("[storage] 使用本地目录 %s", _storage.root)
    return _storage


def reset_storage() -> None:
    """仅供测试用。"""
    global _storage
    _storage = None


# ---- 常用key 约定 -------------------------------------------------------- #
AUDIO_PREFIX = "audio"
JSON_PREFIX = "data"

def audio_key(article_id: int, level_code: str, ext: str = "mp3") -> str:
    return f"{AUDIO_PREFIX}/{article_id}/{level_code}.{ext}"


def index_key() -> str:
    return f"{JSON_PREFIX}/index.json"


def day_index_key(date_str: str) -> str:
    return f"{JSON_PREFIX}/{date_str}/index.json"


def version_key(version_id: int) -> str:
    return f"{JSON_PREFIX}/versions/{version_id}.json"