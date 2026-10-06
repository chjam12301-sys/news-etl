"""诊断存储配置：凭据是否有效、bucket 在哪个区域、端点是否匹配。

用法： .venv/bin/python scripts/diagnose_storage.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_ENV = ROOT / ".env"
if _ENV.is_file():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ[_k.strip()] = _v.strip()


def diag_supabase() -> int:
    """Supabase 走 REST API，认证简单得多。"""
    import httpx

    url = os.environ.get("SUPABASE_PROJECT_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    bucket = os.environ.get("SUPABASE_BUCKET", "")

    print("=" * 58)
    print(" 存储诊断 · Supabase")
    print("=" * 58)
    print(f" Project URL : {url}")
    print(f" bucket      : {bucket}")
    print(f" service key : {key[:12]}...  (长度 {len(key)})")
    print()

    if not (url and key and bucket):
        print(" ✗ 配置不完整。先跑 scripts/save_supabase_keys.sh")
        return 1

    if not key.startswith("sb_secret_") and "eyJ" not in key:
        print(" ⚠ 警告：这不像 service_role key（应以 sb_secret_ 或 eyJ 开头）")
        print("   anon key 只能读，不能写。务必用 Settings → API 里的 service_role。")
        print()

    h = {"Authorization": f"Bearer {key}", "apikey": key}
    try:
        with httpx.Client(timeout=20, headers=h) as c:
            r = c.post(f"{url}/storage/v1/object/list/{bucket}", json={"limit": 1})
            if r.status_code == 200:
                print(f" ✓ bucket '{bucket}' 可访问")
            elif r.status_code in (401, 403):
                print(f" ✗ 认证失败 (HTTP {r.status_code})")
                print("   → service_role key 不对，或 bucket 名写错")
                return 2
            elif r.status_code == 404:
                print(f" ✗ bucket '{bucket}' 不存在")
                print("   → 先去 Storage → New bucket 建一个同名 bucket（记得勾 Public）")
                return 2
            else:
                print(f" ✗ HTTP {r.status_code}: {r.text[:200]}")
                return 2

            # 试写
            probe = {"cacheControl": "3600"}
            files = {"file": ("_probe.txt", b"probe")}
            r2 = c.post(f"{url}/storage/v1/object/{bucket}/_probe.txt", files=files, data=probe)
            if r2.status_code in (200, 201):
                print(" ✓ 写入测试通过")
                pub = f"{url}/storage/v1/object/public/{bucket}/_probe.txt"
                g = c.get(pub)
                if g.status_code == 200:
                    print(" ✓ 公开读取正常 → bucket 是 Public")
                else:
                    print(f" ⚠ 公开读取失败 (HTTP {g.status_code})")
                    print("   → bucket 需设为 Public：Storage → 点bucket → Public bucket 打勾")
                c.delete(f"{url}/storage/v1/object/{bucket}/_probe.txt")
                print(" ✓ 清理完成")
    except Exception as exc:  # noqa: BLE001
        print(f" ✗ 网络/连接失败: {type(exc).__name__}: {str(exc)[:150]}")
        return 1

    print("\n" + "=" * 58)
    print(" ✓ 一切正常，可以跑 backfill_audio.py")
    print("=" * 58)
    return 0


def main() -> int:
    if os.environ.get("STORAGE_BACKEND", "").lower() == "supabase":
        return diag_supabase()

    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError, NoCredentialsError

    backend = os.environ.get("STORAGE_BACKEND", "").lower()

    if backend == "b2":
        bucket = os.environ.get("B2_BUCKET", "")
        key_id = os.environ.get("B2_ACCESS_KEY_ID", "")
        secret = os.environ.get("B2_SECRET_ACCESS_KEY", "")
        endpoint = os.environ.get("B2_ENDPOINT", "")
        prefix = "B2"
    else:
        bucket = os.environ.get("R2_BUCKET", "")
        key_id = os.environ.get("R2_ACCESS_KEY_ID", "")
        secret = os.environ.get("R2_SECRET_ACCESS_KEY", "")
        endpoint = os.environ.get("R2_ENDPOINT", "")
        prefix = "R2"

    print("=" * 58)
    print(f" 存储诊断 · {prefix}")
    print("=" * 58)
    print(f" 后端      : {backend or '(未指定)'}")
    print(f" bucket    : {bucket}")
    print(f" endpoint  : {endpoint}")
    print(f" Key ID: {key_id[:8] + '...' if key_id else '(空)'}  (长度 {len(key_id)})")
    print(f" App Key    : {secret[:6] + '...' if secret else '(空)'}  (长度 {len(secret)})")
    print()

    # 格式预检：B2 的 applicationKeyId 通常 25 位且以 005/006 开头
    if backend == "b2":
        issues = []
        if len(key_id) < 20:
            issues.append(f"Key ID 只有 {len(key_id)} 位，B2 的 applicationKeyId 通常是 25 位")
        if secret.startswith(("005", "006")):
            issues.append(
                f"App Key 以 '{secret[:3]}' 开头 —— 这看起来是 applicationKeyId，"
                "两者可能填反了"
            )
        if not key_id.startswith(("005", "006", "00")):
            issues.append(f"Key ID 前缀是 '{key_id[:3]}'，B2 通常是 005或 006 开头")
        if issues:
            print(" ⚠ 配置看起来有问题：")
            for i in issues:
                print(f"    - {i}")
            print()
            print("   B2 控制台 App Keys 页面会列出两行，请注意对应关系：")
            print("     applicationKeyId → 对应 Key ID栏（长，约 25 位，形如 005xxxxx）")
            print("     applicationKey   → 对应 Application Key 栏（形如 0067bd...，40+ 位）")
            print()
            print("   若两者确实填反了，把.benv 里这两行互换即可：")
            print("     B2_ACCESS_KEY_ID=<applicationKeyId>")
            print("     B2_SECRET_ACCESS_KEY=<applicationKey>")
            print()

    if not (bucket and key_id and secret and endpoint):
        print(" ✗ 配置不完整。先跑 scripts/save_b2_keys.sh 或 save_r2_keys.sh")
        return 1

    # B2 认证是 keyId:applicationKey 双段拼接
    if backend == "b2":
        import re
        region = "us-west-004"
        host = endpoint.replace("https://", "").split("/")[0]
        m = re.match(r"s3[.-]([a-z0-9-]+)\.backblazeb2\.com", host)
        if m:
            region = m.group(1)
        print(f" 推导 region: {region}")

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=(f"{key_id}:{secret}" if backend == "b2" else key_id),
        aws_secret_access_key=("b2-placeholder" if backend == "b2" else secret),
        region_name=region if backend == "b2" else "auto",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 2},
        ),
    )

    print("\n [1] 列举 bucket（验证凭据 + 区域）")
    try:
        resp = client.list_buckets()
        names = [b["Name"] for b in resp.get("Buckets", [])]
        print(f"     ✓ 凭据有效，可见 {len(names)} 个 bucket")
        for n in names:
            mark = " ← 目标" if n == bucket else ""
            print(f"       - {n}{mark}")
        if bucket not in names:
            print(f"\n     ✗ bucket '{bucket}' 不在账号里！")
            print(f"       可用的是: {names}")
            return 2
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "?")
        print(f"     ✗ 失败: {code}")
        print(f"       {str(e)[:200]}")
        if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "401"):
            print("\n     → Key ID / Application Key 不正确，请重新核对")
            return 2
        if code in ("AccessDenied", "403"):
            print("\n     → 权限不足。App Key 需要 Read & Write Files 权限")
            return 2
        return 2
    except NoCredentialsError:
        print("     ✗ 没有凭据")
        return 1

    print("\n [2] 测试写入（验证 bucket 区域与端点匹配）")
    key = "_probe.txt"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=b"probe")
        print(f"     ✓ 写入成功 → {endpoint}/{bucket}/{key}")
        client.delete_object(Bucket=bucket, Key=key)
        print("     ✓ 清理完成")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "?")
        print(f"     ✗ 写入失败: {code}")
        print(f"       {str(e)[:200]}")
        if code in ("301", "PermanentRedirect", "BadRequest"):
            print("\n     → 区域不匹配！bucket 所在区域与 endpoint 不一致。")
            print("       B2 控制台 bucket 详情页会显示正确的 Endpoint，")
            print("       或用 list_buckets 返回的信息确认。")
        return 3

    print("\n" + "=" * 58)
    print(" ✓ 一切正常，可以跑 backfill_audio.py")
    print("=" * 58)
    return 0


if __name__ == "__main__":
    sys.exit(main())