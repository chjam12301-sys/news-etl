"""存储层测试：本地实现 + R2 实现（mock boto3，不联网）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.storage import (
    JSON_PREFIX,
    LocalStorage,
    StoredObject,
    audio_key,
    day_index_key,
    get_storage,
    index_key,
    reset_storage,
    version_key,
)


# --------------------------------------------------------------------------- #
# Key 约定
# --------------------------------------------------------------------------- #
def test_key_conventions():
    assert audio_key(7, "en_a1") == "audio/7/en_a1.mp3"
    assert audio_key(7, "ja_n3", "wav") == "audio/7/ja_n3.wav"
    assert index_key() == f"{JSON_PREFIX}/index.json"
    assert day_index_key("2026-10-06") == f"{JSON_PREFIX}/2026-10-06/index.json"
    assert version_key(42) == f"{JSON_PREFIX}/versions/42.json"


# --------------------------------------------------------------------------- #
# LocalStorage
# --------------------------------------------------------------------------- #
def test_local_put_read_exists(tmp_path: Path):
    st = LocalStorage(tmp_path)
    obj = st.put("audio/1/en_a1.mp3", b"ID3fakeaudio", content_type="audio/mpeg")
    assert isinstance(obj, StoredObject)
    assert obj.size == len(b"ID3fakeaudio")
    assert st.exists("audio/1/en_a1.mp3")
    assert st.read("audio/1/en_a1.mp3") == b"ID3fakeaudio"


def test_local_read_missing_returns_none(tmp_path: Path):
    st = LocalStorage(tmp_path)
    assert st.read("audio/nope.mp3") is None
    assert not st.exists("audio/nope.mp3")


def test_local_list_keys_prefix(tmp_path: Path):
    st = LocalStorage(tmp_path)
    st.put("data/index.json", b"{}")
    st.put("data/2026-10-06/index.json", b"{}")
    st.put("data/2026-10-07/index.json", b"{}")
    st.put("audio/1/en_a1.mp3", b"x")

    assert set(st.list_keys("data")) == {
        "data/index.json",
        "data/2026-10-06/index.json",
        "data/2026-10-07/index.json",
    }
    assert st.list_keys("audio") == ["audio/1/en_a1.mp3"]
    assert len(st.list_keys("")) == 4


def test_local_overwrite(tmp_path: Path):
    st = LocalStorage(tmp_path)
    st.put("data/index.json", b"old")
    st.put("data/index.json", b"new-longer")
    assert st.read("data/index.json") == b"new-longer"


def test_local_delete(tmp_path: Path):
    st = LocalStorage(tmp_path)
    st.put("a/b.txt", b"x")
    st.delete("a/b.txt")
    assert not st.exists("a/b.txt")
    st.delete("a/b.txt")  # 重复删除不应报错


def test_local_blocks_path_traversal(tmp_path: Path):
    """防止 key 里带 ../ 逃出存储根目录。"""
    root = tmp_path / "root"
    st = LocalStorage(root)
    st.put("../escape.txt", b"nope")
    st.put("a/../../escape2.txt", b"nope")

    # 关键：不能写到 root 之外
    assert not (tmp_path / "escape.txt").exists()
    assert not (root.parent / "escape.txt").exists()
    assert not (tmp_path.parent / "escape.txt").exists()

    # 所有写入都应落在 root 内（去掉 ../ 后被规范化为根内路径）
    written = [p for p in root.rglob("*") if p.is_file()]
    assert written, "应有文件写入 root 内"


def test_local_json_roundtrip(tmp_path: Path):
    st = LocalStorage(tmp_path)
    payload = {"title": "测试标题", "timeline": [{"i": 0, "w": "Hi", "s": 0.0, "e": 0.3}]}
    st.put("data/versions/1.json", json.dumps(payload, ensure_ascii=False).encode())
    got = json.loads(st.read("data/versions/1.json").decode())
    assert got["title"] == "测试标题"
    assert got["timeline"][0]["w"] == "Hi"


# --------------------------------------------------------------------------- #
# R2（mock）
# --------------------------------------------------------------------------- #
class _FakeClient:
    """最小 boto3 client 替身。"""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple] = []

    def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803
        self.calls.append(("put", Key, kw.get("ContentType"), kw.get("CacheControl")))
        self.objects[Key] = Body

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):  # noqa: N803
        import io

        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000, ContinuationToken=None):  # noqa: N803
        keys = [k for k in self.objects if k.startswith(Prefix)]
        keys.sort()
        if not keys:
            return {"Contents": [], "IsTruncated": False}
        return {
            "Contents": [{"Key": k, "Size": len(self.objects[k])} for k in keys[:MaxKeys]],
            "IsTruncated": False,
        }

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        self.calls.append(("presign", Params["Key"], ExpiresIn))
        return f"https://signed.example/{Params['Key']}?sig=x"


@pytest.fixture()
def r2(monkeypatch):
    """构造一个注入 fake client 的 R2Storage。"""
    pytest.importorskip("botocore", reason="R2 测试需要 boto3/botocore")
    from app import storage as storage_mod

    st = storage_mod.R2Storage.__new__(storage_mod.R2Storage)
    st.bucket = "test-bucket"
    st.public_base = ""
    st.client = _FakeClient()
    return st


def test_r2_put_read(r2):
    obj = r2.put("audio/1/en_a1.mp3", b"ID3data", content_type="audio/mpeg")
    assert obj.size == len(b"ID3data")
    assert r2.exists("audio/1/en_a1.mp3")
    assert r2.read("audio/1/en_a1.mp3") == b"ID3data"


def test_r2_missing_returns_none(r2):
    assert r2.read("audio/nope.mp3") is None
    assert not r2.exists("audio/nope.mp3")


def test_r2_sets_cache_and_content_type(r2):
    r2.put("data/index.json", b"{}", content_type="application/json")
    kind, key, ctype, cache = r2.client.calls[0]
    assert kind == "put" and key == "data/index.json"
    assert ctype == "application/json"
    assert "immutable" in cache


def test_r2_list_keys_prefix(r2):
    r2.put("data/index.json", b"{}")
    r2.put("data/2026-10-06/index.json", b"{}")
    r2.put("audio/1/en_a1.mp3", b"x")
    assert r2.list_keys("data") == ["data/2026-10-06/index.json", "data/index.json"] or set(
        r2.list_keys("data")
    ) == {"data/index.json", "data/2026-10-06/index.json"}


def test_r2_public_base_takes_priority(r2):
    r2.public_base = "https://cdn.example.com"
    assert r2.public_url("audio/1/en_a1.mp3") == "https://cdn.example.com/audio/1/en_a1.mp3"


def test_r2_falls_back_to_presign(r2):
    r2.public_base = ""
    url = r2.public_url("audio/1/en_a1.mp3")
    assert url.startswith("https://signed.example/")
    assert any(c[0] == "presign" for c in r2.client.calls)


# --------------------------------------------------------------------------- #
# 工厂函数
# --------------------------------------------------------------------------- #
def test_local_storage_is_default_after_import(monkeypatch, tmp_path):
    """所有云存储凭证都为空时，必须回退本地，绝不能因漏配而崩。"""
    from app import config as config_mod
    from app.storage import LocalStorage, reset_storage

    reset_storage()
    s = config_mod.get_settings()
    for k in ("r2_bucket", "r2_access_key_id", "r2_secret_access_key", "r2_endpoint",
              "b2_bucket", "b2_access_key_id", "b2_secret_access_key", "b2_endpoint",
              "supabase_project_url", "supabase_service_key", "supabase_bucket",
              "github_content_repo", "github_branch", "github_cdn_base"):
        monkeypatch.setattr(s, k, "", raising=False)

    st = get_storage()
    assert isinstance(st, LocalStorage), f"未配凭证时回退失败，得到 {type(st).__name__}"
    reset_storage()


def test_local_storage_is_default_after_import(monkeypatch, tmp_path):
    """所有云存储凭证都为空时，必须回退本地，绝不能因漏配而崩。"""
    from app import config as config_mod
    from app.storage import LocalStorage, reset_storage

    reset_storage()
    s = config_mod.get_settings()
    for k in ("r2_bucket", "r2_access_key_id", "r2_secret_access_key", "r2_endpoint",
              "b2_bucket", "b2_access_key_id", "b2_secret_access_key", "b2_endpoint",
              "supabase_project_url", "supabase_service_key", "supabase_bucket",
              "github_content_repo", "github_branch", "github_cdn_base"):
        monkeypatch.setattr(s, k, "", raising=False)

    st = get_storage()
    assert isinstance(st, LocalStorage), f"未配凭证时回退失败，得到 {type(st).__name__}"
    reset_storage()

