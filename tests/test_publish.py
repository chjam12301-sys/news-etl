"""发布顺序的回归测试。

历史故障：导出时用当前 HEAD 拼 CDN 链接，而新文件尚未提交，
导致索引里的 detail_url / audio.url 指向不含这些文件的 commit——
首页卡片正常（标题/图片/译文都在索引里），点进详情 404。

修复为四阶段：① 提交内容 → ② 用该 commit 生成索引 → ③ 校验 → ④ 更新指针。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.publish import verify_published


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """建一个含内容产物的 git 仓库，模拟发布。"""
    r = tmp_path / "repo"
    (r / "content" / "data" / "versions").mkdir(parents=True)
    (r / "content" / "audio" / "1").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    return r


def _write_detail(repo: Path, vid: int, article_id: int, chash: str) -> None:
    d = {
        "version_id": vid, "article_id": article_id, "content_hash": chash,
        "paragraphs": ["hello world"], "paragraphs_zh": ["你好世界"],
    }
    (repo / f"content/data/versions/{vid}.json").write_text(
        json.dumps(d, ensure_ascii=False)
    )
    audio_dir = repo / f"content/audio/{article_id}"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "en.mp3").write_bytes(b"x" * 64)


def _index(vid: int, article_id: int, rev: str, chash: str) -> dict:
    return {
        "latest": {
            "date": "2026-10-08",
            "versions": {
                "en": [{
                    "version_id": vid, "article_id": article_id,
                    "content_hash": chash,
                    "detail_url": f"https://cdn.jsdelivr.net/gh/o/r@{rev}/content/data/versions/{vid}.json",
                    "audio": {"url": f"https://cdn.jsdelivr.net/gh/o/r@{rev}/content/audio/{article_id}/en.mp3"},
                }],
                "ja": [],
            },
        }
    }


class TestVerifyPublished:
    def test_ok_when_everything_present(self, repo: Path):
        _write_detail(repo, 1, 1, "abc")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c1")
        rev = _git(repo, "rev-parse", "HEAD")

        r = verify_published(repo_dir=str(repo), rev=rev, index=_index(1, 1, rev, "abc"))
        assert r.ok, r.summary()
        assert r.checked == 1

    def test_detects_missing_detail(self, repo: Path):
        """索引说有，实际 commit 里没有 → 必须判定失败。"""
        (repo / "README.md").write_text("x")   # 保证有可提交内容
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
        rev = _git(repo, "rev-parse", "HEAD")

        r = verify_published(repo_dir=str(repo), rev=rev, index=_index(99, 1, rev, "x"))
        assert not r.ok
        assert 99 in r.missing_detail

    def test_detects_missing_audio(self, repo: Path):
        _write_detail(repo, 5, 5, "h5")
        (repo / "content/audio/5/en.mp3").unlink()   # 音频没提交（被unlink）
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c")
        rev = _git(repo, "rev-parse", "HEAD")

        r = verify_published(repo_dir=str(repo), rev=rev, index=_index(5, 5, rev, "h5"))
        assert not r.ok
        assert 5 in r.missing_audio

    def test_detects_hash_mismatch(self, repo: Path):
        """详情内容与索引声明的 hash 不一致 → 张冠李戴，必须拦。"""
        _write_detail(repo, 7, 7, "real_hash")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c")
        rev = _git(repo, "rev-parse", "HEAD")

        r = verify_published(repo_dir=str(repo), rev=rev, index=_index(7, 7, rev, "claimed"))
        assert not r.ok
        assert 7 in r.hash_mismatch

    def test_detects_identity_mismatch(self, repo: Path):
        """详情里的 article_id 与索引不符 → 必须拦。"""
        _write_detail(repo, 8, 111, "hx")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c")
        rev = _git(repo, "rev-parse", "HEAD")

        idx = _index(8, 999, rev, "hx")   # 索引说 article_id=999
        r = verify_published(repo_dir=str(repo), rev=rev, index=idx)
        assert not r.ok
        assert r.identity_mismatch

    def test_empty_index_fails(self, repo: Path):
        r = verify_published(repo_dir=str(repo), rev="HEAD",
                             index={"latest": {"versions": {"en": [], "ja": []}}})
        assert not r.ok

    def test_covers_every_entry_not_just_first(self, repo: Path):
        """必须覆盖本次索引引用的每一份，而不是只看第一份。"""
        for vid, aid in [(1, 1), (2, 2), (3, 3)]:
            _write_detail(repo, vid, aid, f"h{vid}")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c")
        rev = _git(repo, "rev-parse", "HEAD")

        idx = {
            "latest": {"versions": {"en": [
                _index(1, 1, rev, "h1")["latest"]["versions"]["en"][0],
                _index(2, 2, rev, "h2")["latest"]["versions"]["en"][0],
                # 第三个故意指向不存在的 commit
                _index(3, 3, "deadbeef", "h3")["latest"]["versions"]["en"][0],
            ], "ja": []}}
        }
        r = verify_published(repo_dir=str(repo), rev=rev, index=idx)
        assert not r.ok
        assert r.checked == 3, "必须逐条检查，不能只看第一份"
        # deadbeef 这个 commit 不存在 → 按URL 自己的 rev 查文件必然缺失
        assert 3 in r.missing_detail, f"第 3 条应被判缺失，实际: {r}"


class TestOrderMatters:
    def test_index_generated_before_commit_points_to_wrong_rev(self, repo: Path):
        """复现原故障：先生成索引（用旧 HEAD），再提交内容 → 链接失效。"""
        # 第一次提交：只有旧的 detail
        _write_detail(repo, 1, 1, "h1")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "v1")
        rev1 = _git(repo, "rev-parse", "HEAD")

        # 此时导出索引，链接用的是 rev1
        idx = _index(1, 1, rev1, "h1")

        # 之后才提交新内容 v2 —— 但索引里指向的 rev1 没有 v2
        _write_detail(repo, 2, 2, "h2")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "v2")

        # 校验索引引用的 v1 下的 v2 → 必然缺失
        bad = {
            "latest": {"versions": {"en": [
                _index(2, 2, rev1, "h2")["latest"]["versions"]["en"][0],
            ], "ja": []}}
        }
        r = verify_published(repo_dir=str(repo), rev=rev1, index=bad)
        assert not r.ok, "旧 commit 下不可能有新详情，必须判定失败"

        # 正确顺序：先提交内容，再用新 commit 生成索引 → 通过
        rev2 = _git(repo, "rev-parse", "HEAD")
        good = {
            "latest": {"versions": {"en": [
                _index(2, 2, rev2, "h2")["latest"]["versions"]["en"][0],
            ], "ja": []}}
        }
        assert verify_published(repo_dir=str(repo), rev=rev2, index=good).ok
        # 而旧顺序产出的索引：v1 的文件确实存在，只是版本用错了上下文 ——
        # 它引用的 rev1 本身是自洽的，所以校验通过；这正是「校验通过但
        # 链接仍会失效」的情形，只能靠**顺序**保证，不能靠校验发现。
        stale = {
            "latest": {"versions": {"en": [
                _index(2, 2, rev1, "h2")["latest"]["versions"]["en"][0]],"ja": []}}
        }
        assert not verify_published(repo_dir=str(repo), rev=rev1, index=stale).ok


class TestLatestPointerUpdatedLast:
    def test_commit_does_not_touch_latest(self, repo: Path):
        """commit() 本身不应更新 latest —— 由publish_all 在校验后统一做。"""
        from app.storage_github import GitHubStorage

        st = GitHubStorage("o/r", local_dir=str(repo))
        assert "refresh_latest_pointer" in dir(st)
        src = (
            Path(__file__).parent.parent / "app" / "storage_github.py"
        ).read_text(encoding="utf-8")
        # commit 方法体内不应调用指针更新
        body = src.split("def commit(")[1].split("def ")[0]
        assert "refresh_latest_pointer" not in body, \
            "commit() 不应更新 latest.json，否则会跳过校验"