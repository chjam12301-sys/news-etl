"""音频托管与发布校验的回归测试。

背景：jsDelivr 单仓库 50MB 上限，音频 214MB 会把仓库撑爆（新路径一律 403）。
因此音频必须走对象存储，且**永不进 Git**。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.audio_store import (
    audio_base_url,
    audio_object_key,
    get_audio_storage,
    public_audio_url,
    reset_audio_storage,
)
from app.config import settings
from app.publish import verify_published


@pytest.fixture(autouse=True)
def _reset():
    reset_audio_storage()
    yield
    reset_audio_storage()


class TestPublicAudioUrl:
    def test_uses_configured_base(self, monkeypatch):
        monkeypatch.setattr(settings, "audio_public_base", "https://pub-x.r2.dev")
        assert (
            public_audio_url(file_path="audio/23/A1.mp3", article_id=23, level_code="A1")
            == "https://pub-x.r2.dev/audio/23/A1.mp3"
        )

    def test_falls_back_to_r2_public_base(self, monkeypatch):
        monkeypatch.setattr(settings, "audio_public_base", "")
        monkeypatch.setattr(settings, "r2_public_base", "https://pub-y.r2.dev")
        assert audio_base_url() == "https://pub-y.r2.dev"

    def test_legacy_file_path_replaced_by_ids(self, monkeypatch):
        """历史 file_path 可能是本地绝对路径，必须回退到 article/level 拼。"""
        monkeypatch.setattr(settings, "audio_public_base", "https://pub-x.r2.dev")
        assert (
            public_audio_url(file_path="/tmp/whatever.mp3", article_id=23, level_code="A1")
            == "https://pub-x.r2.dev/audio/23/A1.mp3"
        )

    def test_empty_when_unconfigured(self, monkeypatch):
        for k in ("audio_public_base", "r2_public_base", "b2_public_base",
                  "supabase_project_url"):
            monkeypatch.setattr(settings, k, "")
        assert public_audio_url(article_id=1, level_code="A1") == ""

    def test_key_regex_rejects_escape(self):
        assert audio_object_key("audio/23/A1.mp3") == "audio/23/A1.mp3"
        assert audio_object_key("../../etc/passwd", 5, "A1") == "audio/5/A1.mp3"


class TestNeverGitHub:
    def test_no_github_candidate(self):
        """音频存储候选里绝不能有 GitHub —— 这是整个模块存在的理由。"""
        src = (
            Path(__file__).parent.parent / "app" / "audio_store.py"
        ).read_text(encoding="utf-8")
        assert "_try_github" not in src

    def test_fallback_is_local_not_github(self, monkeypatch):
        for k in ("r2_bucket", "r2_endpoint", "b2_bucket", "b2_endpoint",
                  "supabase_project_url"):
            monkeypatch.setattr(settings, k, "")
        st = get_audio_storage()
        assert getattr(st, "name", "") != "github"
        assert st.name == "local-audio"

    def test_commit_strips_audio_from_index(self):
        """git add -f 会无视 .gitignore，提交时必须显式摘掉 content/audio。"""
        src = (
            Path(__file__).parent.parent / "app" / "storage_github.py"
        ).read_text(encoding="utf-8")
        body = src.split("def _commit")[1].split("def exists")[0]
        assert "ignore-unmatch" in body and "audio" in body


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "content" / "data" / "versions").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    return r


def _index(vid: int, article_id: int, rev: str, chash: str, audio_url: str) -> dict:
    return {
        "latest": {"versions": {"en": [{
            "version_id": vid, "article_id": article_id, "content_hash": chash,
            "detail_url": f"https://cdn.jsdelivr.net/gh/o/r@{rev}/content/data/versions/{vid}.json",
            "audio": {"url": audio_url},
        }], "ja": []}}
    }


class TestVerifyExternalAudio:
    def _commit_detail(self, repo: Path, vid: int, aid: int, chash: str) -> str:
        d = {"version_id": vid, "article_id": aid, "content_hash": chash,
             "paragraphs": ["x"], "paragraphs_zh": ["x"]}
        (repo / f"content/data/versions/{vid}.json").write_text(json.dumps(d))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c")
        return _git(repo, "rev-parse", "HEAD")

    def test_object_storage_audio_checked_by_http(self, repo: Path):
        rev = self._commit_detail(repo, 1, 1, "h1")
        url = "https://pub-x.r2.dev/audio/1/en.mp3"     # 不含 /content/ → 走 HTTP
        seen: list[str] = []

        def fake_head(u: str, **kw):
            seen.append(u)
            return True, ""

        r = verify_published(repo_dir=str(repo), rev=rev,
                             index=_index(1, 1, rev, "h1", url), head=fake_head)
        assert r.ok, r.summary()
        assert seen == [url]

    def test_missing_object_blocks_publish(self, repo: Path):
        rev = self._commit_detail(repo, 2, 2, "h2")
        r = verify_published(
            repo_dir=str(repo), rev=rev,
            index=_index(2, 2, rev, "h2", "https://pub-x.r2.dev/audio/2/en.mp3"),
            head=lambda u, **kw: (False, "HTTP 404"),
        )
        assert not r.ok
        assert 2 in r.missing_audio
        assert r.audio_errors and "HTTP 404" in r.audio_errors[0]

    def test_non_http_audio_url_blocks_publish(self, repo: Path):
        """没配对象存储时 URL 是相对路径 —— 绝不能对外发布。"""
        rev = self._commit_detail(repo, 3, 3, "h3")
        r = verify_published(
            repo_dir=str(repo), rev=rev,
            index=_index(3, 3, rev, "h3", "/files/audio/3/en.mp3"),
            head=lambda u, **kw: (True, ""),
        )
        assert not r.ok
        assert 3 in r.missing_audio
