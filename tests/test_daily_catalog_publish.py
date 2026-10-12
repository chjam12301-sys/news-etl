import datetime as dt
import json
import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import exporter, publish
from app.db import Base, Article, ArticleVersion
from app.storage import day_index_key, index_key
from app.storage_github import GitHubStorage


@pytest.fixture
def publication(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "test@example.test")
    git("config", "user.name", "test")
    (repo / "README").write_text("fixture")
    git("add", "README")
    git("commit", "-qm", "initial")
    storage = GitHubStorage("chjam12301-sys/news-etl", local_dir=str(repo))
    def commit(message):
        git("add", "-A", "content")
        git("commit", "--allow-empty", "-qm", message)
        return True
    monkeypatch.setattr(storage, "_commit", commit)
    pointers = []
    monkeypatch.setattr(storage, "refresh_latest_pointer", pointers.append)
    monkeypatch.setattr(exporter, "get_storage", lambda: storage)
    monkeypatch.setattr(exporter.settings, "public_base_url", storage.cdn_base + "/content")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        article = Article(source="test", source_url="https://example.test", topic="world",
                          title_original="Test", published_date=dt.date(2026, 10, 9), fingerprint="a" * 40)
        db.add(article)
        db.flush()
        version = ArticleVersion(article_id=article.id, level_code="en_a1", lang="en", level=1,
                                 level_label="A1", title="Test", body="Hello world.",
                                 paragraphs=["Hello world."], content_hash="a" * 16)
        db.add(version)
        db.commit()
        exporter.export_day(db, article.published_date)
        storage.commit("old details")
        old_ref = storage.current_ref()
        article.published_date = dt.date(2026, 10, 12)
        db.commit()
        yield db, storage, pointers, old_ref, git


def read_at(storage, rev, path):
    return json.loads(publish._read_in_commit(storage.repo_dir, rev, path))


def test_one_publish_pins_daily_catalog_after_committed_details(publication):
    db, storage, pointers, old_ref, git = publication
    result = publish.publish_all(db=db, storage=storage)
    assert result["ok"]
    assert pointers == [result["index_rev"]]
    root = read_at(storage, result["index_rev"], index_key())
    catalog_ref = publish._rev_in_url(root["base_url"])
    assert catalog_ref not in (old_ref, result["index_rev"])
    daily = read_at(storage, catalog_ref, day_index_key("2026-10-12"))
    item = daily["versions"]["en"]["en_a1"][0]
    detail_ref = publish._rev_in_url(item["detail_url"])
    assert detail_ref not in (old_ref, catalog_ref, result["index_rev"])
    detail = read_at(storage, detail_ref, publish._rel(item["detail_url"]))
    assert detail["published_date"] == item["published_date"] == "2026-10-12"
    assert detail["version_id"] == item["version_id"]
    assert detail["content_hash"] == item["content_hash"]
    assert publish.verify_published(repo_dir=storage.repo_dir, rev=result["index_rev"], index=root).ok


@pytest.mark.parametrize("corruption", ["old_detail", "date", "level", "hash", "missing_day", "duplicate_day", "foreign_detail", "unversioned_detail"])
def test_verifier_follows_actual_daily_urls_and_rejects_corruption(publication, corruption):
    db, storage, pointers, old_ref, git = publication
    result = publish.publish_all(db=db, storage=storage)
    root = read_at(storage, result["index_rev"], index_key())
    path = day_index_key("2026-10-12")
    day = json.loads(storage.read(path))
    item = day["versions"]["en"]["en_a1"][0]
    if corruption == "old_detail":
        item["detail_url"] = storage.public_url("data/versions/1.json", version=old_ref)
    elif corruption == "foreign_detail":
        item["detail_url"] = item["detail_url"].replace("news-etl@", "other-repo@")
    elif corruption == "unversioned_detail":
        item["detail_url"] = storage.public_url("data/versions/1.json")
    elif corruption == "date":
        item["published_date"] = "2026-10-09"
    elif corruption == "level":
        item["level_code"] = "en_b1"
    elif corruption == "hash":
        item["content_hash"] = "b" * 16
    elif corruption == "duplicate_day":
        day["versions"]["en"]["en_a1"].append(dict(item))
        day["version_count"] += 1
    if corruption == "missing_day":
        storage.delete(path)
    else:
        storage.put(path, json.dumps(day).encode())
    storage.commit("corrupt reachable catalog")
    root["base_url"] = storage.public_url("", version=storage.current_ref()).rstrip("/")
    checked = publish.verify_published(repo_dir=storage.repo_dir, rev=storage.current_ref(), index=root)
    assert not checked.ok
    assert checked.identity_mismatch or checked.hash_mismatch


def test_bad_daily_locator_blocks_pointer_update(publication, monkeypatch):
    db, storage, pointers, old_ref, git = publication
    export = exporter.export_day
    def corrupt(db, date, **kwargs):
        key = export(db, date, **kwargs)
        if kwargs.get("export_details") is False:
            day = json.loads(storage.read(key))
            day["versions"]["en"]["en_a1"][0]["detail_url"] = storage.public_url("data/versions/1.json", version=old_ref)
            storage.put(key, json.dumps(day).encode())
        return key
    monkeypatch.setattr(exporter, "export_day", corrupt)
    result = publish.publish_all(db=db, storage=storage)
    assert not result["ok"] and result["reason"] == "verify_failed"
    assert pointers == []
