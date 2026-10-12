import json
from subprocess import CompletedProcess
from unittest.mock import Mock

import httpx
import pytest

from app.storage_github import GitHubStorage


@pytest.fixture
def storage(tmp_path, monkeypatch):
    st = GitHubStorage("chjam12301-sys/news-etl", local_dir=str(tmp_path))
    monkeypatch.setattr(st, "_git", lambda *args, **kw: CompletedProcess(args, 0, stdout=""))
    return st


def reply(url, **kwargs):
    assert kwargs == {"timeout": 10, "follow_redirects": False}
    path = httpx.URL(url).path
    return httpx.Response(200, request=httpx.Request("GET", url), json={
        "status": "finished", "paths": {path: {"throttled": False, "providers": {"CF": True, "FY": True}}}})


def test_purge_only_two_mutable_paths_after_push(storage, monkeypatch):
    events = []
    monkeypatch.setattr(storage, "_commit", lambda message: events.append("push") or True)
    def get(url, **kwargs):
        events.append(url)
        return reply(url, **kwargs)
    monkeypatch.setattr(httpx, "get", get)
    result = storage.refresh_latest_pointer("a" * 40)
    assert events == ["push", "https://purge.jsdelivr.net/gh/chjam12301-sys/news-etl@main/content/latest.json",
                      "https://purge.jsdelivr.net/gh/chjam12301-sys/news-etl@main/content/data/latest.json"]
    assert result["cdn_visibility"]["status"] == "purged"
    for key in ("latest.json", "data/latest.json"):
        assert json.loads(storage.read(key))["ref"] == "a" * 40


@pytest.mark.parametrize("failure", ["timeout", "http", "throttled", "provider", "malformed"])
def test_purge_failure_keeps_pointer_and_reports_retry(storage, monkeypatch, caplog, failure):
    monkeypatch.setattr(storage, "_commit", lambda message: True)
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if len(calls) == 2:
            return reply(url, **kwargs)
        if failure == "timeout":
            raise httpx.ReadTimeout("SECRET-IN-EXCEPTION")
        if failure == "http":
            return httpx.Response(503, request=httpx.Request("GET", url))
        if failure == "malformed":
            return httpx.Response(200, request=httpx.Request("GET", url), json=[])
        return httpx.Response(200, request=httpx.Request("GET", url), json={"status": "finished", "paths": {
            httpx.URL(url).path: {"throttled": failure == "throttled", "providers": {"CF": failure != "provider"}}}})
    monkeypatch.setattr(httpx, "get", get)
    result = storage.refresh_latest_pointer("b" * 40)
    assert result["pointer_published"]
    assert result["cdn_visibility"]["status"] == "retry_needed"
    assert len(calls) == 2
    assert json.loads(storage.read("latest.json"))["ref"] == "b" * 40
    assert "visibility is unconfirmed" in caplog.text
    assert "SECRET-IN-EXCEPTION" not in caplog.text + json.dumps(result)


def test_push_failure_never_purges(storage, monkeypatch):
    def fail(message):
        raise RuntimeError("push failed")
    monkeypatch.setattr(storage, "_commit", fail)
    get = Mock(side_effect=AssertionError("must not purge"))
    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(RuntimeError):
        storage.refresh_latest_pointer("c" * 40)
    get.assert_not_called()


def test_unchanged_pointer_can_retry_purge(storage, monkeypatch):
    monkeypatch.setattr(storage, "_commit", lambda message: False)
    get = Mock(side_effect=reply)
    monkeypatch.setattr(httpx, "get", get)
    result = storage.refresh_latest_pointer("d" * 40)
    assert result["cdn_visibility"]["status"] == "purged"
    assert get.call_count == 2
