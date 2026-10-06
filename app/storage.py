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
import time
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


def _put_via_awscli(key: str, path: str, content_type: str | None) -> bool:
    """用 aws cli 上传。GitHub Actions runner 的 Python TLS 栈与 R2
    握手失败（SSLV3_ALERT_HANDSHAKE_FAILURE），而 aws cli 用 OpenSSL，
    通常不受影响。作为备用通道。"""
    import os
    import subprocess
    import tempfile

    endpoint = os.environ.get("R2_ENDPOINT", "")
    bucket = os.environ.get("R2_BUCKET", "")
    if not endpoint or not bucket:
        return False

    env = dict(os.environ)
    env.update(
        AWS_ACCESS_KEY_ID=os.environ.get("R2_ACCESS_KEY_ID", ""),
        AWS_SECRET_ACCESS_KEY=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
        AWS_DEFAULT_REGION="auto",
    )
    cmd = [
        "aws", "s3", "cp", path, f"s3://{bucket}/{key}",
        "--endpoint-url", endpoint,
        "--no-progress",
        "--cache-control", "public, max-age=31536000, immutable",
    ]
    if content_type:
        cmd += ["--content-type", content_type]
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, timeout=120)
        if r.returncode == 0:
            return True
        log.warning("[r2] aws cli 上传失败: %s", r.stderr.decode()[:200])
    except FileNotFoundError:
        log.info("[r2] 未安装 aws cli")
    except Exception as exc:  # noqa: BLE001
        log.warning("[r2] aws cli 调用异常: %s", exc)
    return False


class R2Storage:
    """Cloudflare R2 / Backblaze B2（S3 兼容）实现，使用 boto3。

    两家都是 S3 协议，差异仅在：
    - R2：region="auto"，公开域名形如 https://pub-xxx.r2.dev
    - B2：region 从 endpoint 域名推导（us-west-004 等），
      公开域名形如 https://f000.backblazeb2.com/file/<bucket>

    免费额度：R2 10GB/月+出网免费；B2 10GB。
    """

    name = "r2"  # 实例化后按 provider 覆盖为 "r2" 或 "b2"

    def __init__(
        self,
        bucket: str,
        access_key: str,
        secret_key: str,
        endpoint: str,
        *,
        public_base: str = "",
        region: str = "auto",
        provider: str = "r2",
    ) -> None:
        import boto3  # 延迟导入：本地开发无需安装

        self.bucket = bucket
        self.provider = provider
        self.name = provider
        self.public_base = public_base.rstrip("/")

        # R2 与 B2 都是 S3 兼容，差异主要在：
        # - region：R2 用 "auto"，B2 用 "us-west-004" 等
        # - 公开下载地址格式不同
        config = None
        try:
            from botocore.config import Config

            config = Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},  # R2 用 path 风格最稳
                retries={"max_attempts": 4, "mode": "standard"},
            )
        except Exception:  # noqa: BLE001
            config = None

        kwargs = dict(
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        if config is not None:
            kwargs["config"] = config

        try:
            import certifi

            kwargs["verify"] = certifi.where()
        except Exception:  # noqa: BLE001
            pass

        try:
            self.client = boto3.client("s3", **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("[r2] 带config 初始化失败(%s)，用默认配置重试", exc)
            kwargs.pop("config", None)
            kwargs.pop("verify", None)
            self.client = boto3.client("s3", **kwargs)

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        """写入对象。

        主通道用 boto3；若 TLS 握手失败（GitHub Actions runner 常见），
        自动降级到 aws cli —— 它的TLS 由 OpenSSL 实现，能绕开该问题。
        """
        extra: dict = {"CacheControl": "public, max-age=31536000, immutable"}
        if content_type:
            extra["ContentType"] = content_type

        last: Exception | None = None
        tls_failed = False

        for attempt in range(3):
            try:
                self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
                return StoredObject(key=key, size=len(data), url=self.public_url(key))
            except Exception as exc:  # noqa: BLE001
                last = exc
                if "SSL" in str(exc) or "handshake" in str(exc).lower():
                    tls_failed = True
                    break  # 重试无用，直接走备用通道
                if "AccessDenied" in str(exc) or "InvalidAccessKeyId" in str(exc):
                    raise
                log.warning("[r2] put %s 失败(%s)，重试", key, exc)
                time.sleep(2 * (attempt + 1))

        # 降级：aws cli
        if tls_failed:
            import tempfile

            with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
                f.write(data)
                tmp = f.name
            try:
                if _put_via_awscli(key, tmp, content_type):
                    log.info("[r2] 经 aws cli 上传成功: %s", key)
                    return StoredObject(key=key, size=len(data), url=self.public_url(key))
            finally:
                Path(tmp).unlink(missing_ok=True)

        raise RuntimeError(f"[r2] 上传 {key} 失败: {last}")

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
    """按配置返回存储实现。

    优先级：显式 STORAGE_BACKEND > 自动探测（B2 → R2 → 本地）。
    无论配了什么，任何异常都回退本地，保证内容生成永不中断。
    """
    global _storage
    if _storage is not None:
        return _storage

    backend = (getattr(settings, "storage_backend", "") or "").lower()

    def _try_b2() -> Storage | None:
        bucket = getattr(settings, "b2_bucket", "")
        key_id = getattr(settings, "b2_access_key_id", "")
        secret = getattr(settings, "b2_secret_access_key", "")
        endpoint = getattr(settings, "b2_endpoint", "")
        if not (bucket and key_id and secret and endpoint):
            return None
        # B2 的 region 就写在 endpoint 域名里（s3.us-west-004.…）
        region = "us-west-004"
        for part in endpoint.replace("https://", "").split("."):
            if part.startswith("s3."):
                region = part[3:]
                break
        return R2Storage(
            bucket=bucket,
            access_key=key_id,
            secret_key=secret,
            endpoint=endpoint,
            public_base=getattr(settings, "b2_public_base", "") or "",
            region=region,
            provider="b2",
        )

    def _try_r2() -> Storage | None:
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
            public_base=getattr(settings, "r2_public_base", "") or "",
            provider="r2",
        )

    order = {"b2": [_try_b2, _try_r2], "r2": [_try_r2, _try_b2]}.get(backend, [_try_b2, _try_r2])
    for factory in order:
        try:
            st = factory()
        except Exception as exc:  # noqa: BLE001
            log.error("[storage] %s 初始化失败: %s", getattr(st, "provider", "?"), exc)
            continue
        if st is not None:
            _storage = st
            log.info("[storage] 使用 %s, bucket=%s", st.provider, st.bucket)
            return _storage

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