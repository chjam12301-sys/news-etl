"""配图多级降级的单元测试（全部 mock，不联网）。"""
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import images
from app.exporter import _image_meta


UNSPLASH_PAYLOAD = {
    "total": 1,
    "results": [
        {
            "id": "abc123",
            "width": 5245,
            "height": 3497,
            "alt_description": "a laptop on a desk",
            "urls": {
                "raw": "https://images.unsplash.com/photo-abc123?ixid=XYZ",
                "regular": "https://images.unsplash.com/photo-abc123?w=1080",
            },
            "links": {
                "html": "https://unsplash.com/photos/abc123",
                "download_location": "https://api.unsplash.com/photos/abc123/download",
            },
            "user": {
                "name": "Annie Spratt",
                "links": {"html": "https://unsplash.com/@anniespratt"},
            },
        }
    ],
}


def test_unsplash_parses_hotlink_attribution_and_download(tmp_path, monkeypatch):
    """Unsplash：hotlink 用 API 返回的 urls、署名带 utm、记录 download_location。"""
    monkeypatch.setattr(images.settings, "unsplash_access_key", "test-key")
    monkeypatch.setattr(images.settings, "unsplash_app_name", "daily-english-news")

    async def fake_json(url, *, params, headers, timeout):  # noqa: ANN001
        assert headers["Authorization"] == "Client-ID test-key"
        return UNSPLASH_PAYLOAD

    monkeypatch.setattr(images, "_get_json", fake_json)
    res = asyncio.run(images.search_unsplash("technology"))

    assert res is not None
    assert res.provider == "unsplash"
    # hotlink：必须是 API 返回的 url，只追加尺寸参数
    assert res.url.startswith("https://images.unsplash.com/photo-abc123?ixid=XYZ")
    assert "w=1600" in res.url
    # 署名 + utm 回链
    assert res.attribution == "Photo by Annie Spratt on Unsplash"
    assert res.creator_url == (
        "https://unsplash.com/@anniespratt"
        "?utm_source=daily-english-news&utm_medium=referral"
    )
    # 「on Unsplash」那个词指向图片详情页（不是首页），且同样带 utm
    assert res.source == "Unsplash"
    assert res.source_url == (
        "https://unsplash.com/photos/abc123"
        "?utm_source=daily-english-news&utm_medium=referral"
    )
    assert res.download_location.endswith("/download")


def test_unsplash_skipped_without_key(monkeypatch):
    monkeypatch.setattr(images.settings, "unsplash_access_key", "")
    assert asyncio.run(images.search_unsplash("tech")) is None


def test_chain_falls_back_when_first_provider_empty(monkeypatch):
    """首选源没结果 → 自动降级到下一个源。"""
    monkeypatch.setattr(images.settings, "image_providers", "unsplash,pexels,wikimedia,openverse")

    calls: list[str] = []

    async def fake_unsplash(q, *, timeout):  # noqa: ANN001
        calls.append("unsplash")
        return None

    async def fake_pexels(q, *, timeout):  # noqa: ANN001
        calls.append("pexels")
        return None

    async def fake_wikimedia(q, *, timeout):  # noqa: ANN001
        calls.append("wikimedia")
        return images.ImageResult(url="https://upload.wikimedia.org/x.jpg", provider="wikimedia")

    async def ok_reachable(url, *, timeout=8.0):  # noqa: ANN001
        return True

    monkeypatch.setattr(images, "search_unsplash", fake_unsplash)
    monkeypatch.setattr(images, "search_pexels", fake_pexels)
    monkeypatch.setattr(images, "search_wikimedia", fake_wikimedia)
    monkeypatch.setattr(images, "_reachable", ok_reachable)

    res = asyncio.run(images.find_image("tech", "Some headline about chips"))
    assert res is not None and res.provider == "wikimedia"
    assert "unsplash" in calls and "pexels" in calls


def test_dead_link_is_rejected(monkeypatch):
    """探活失败的链接不能出图。"""
    monkeypatch.setattr(images.settings, "image_providers", "wikimedia")

    async def fake_wikimedia(q, *, timeout):  # noqa: ANN001
        return images.ImageResult(url="https://dead.example/x.jpg", provider="wikimedia")

    async def dead(url, *, timeout=8.0):  # noqa: ANN001
        return False

    monkeypatch.setattr(images, "search_wikimedia", fake_wikimedia)
    monkeypatch.setattr(images, "_reachable", dead)
    assert asyncio.run(images.find_image("tech", "headline")) is None


def test_provider_exception_is_contained(monkeypatch):
    """某个图源抛异常不能打断整条链。"""
    monkeypatch.setattr(images.settings, "image_providers", "unsplash,wikimedia")

    async def boom(q, *, timeout):  # noqa: ANN001
        raise RuntimeError("api down")

    async def fake_wikimedia(q, *, timeout):  # noqa: ANN001
        return images.ImageResult(url="https://upload.wikimedia.org/y.jpg", provider="wikimedia")

    async def ok(url, *, timeout=8.0):  # noqa: ANN001
        return True

    monkeypatch.setattr(images, "search_unsplash", boom)
    monkeypatch.setattr(images, "search_wikimedia", fake_wikimedia)
    monkeypatch.setattr(images, "_reachable", ok)
    assert asyncio.run(images.find_image("tech", "headline")).provider == "wikimedia"


def test_exporter_photo_carries_gradient_fallback():
    """photo 类型必须同时给出渐变兜底，App 取不到图时不会空白。"""
    art = SimpleNamespace(
        topic="tech",
        title_original="Chips",
        image_url="https://images.unsplash.com/photo-1?w=1600",
        image_credit="Photo by Annie Spratt on Unsplash",
        image_provider="unsplash",
        image_credit_url="https://unsplash.com/@anniespratt?utm_source=x&utm_medium=referral",
        image_source_url="https://unsplash.com/photos/abc123?utm_source=x&utm_medium=referral",
        image_author="Annie Spratt",
    )
    meta = _image_meta(art)
    assert meta["type"] == "photo"
    assert meta["provider"] == "unsplash"
    assert meta["fallback"]["type"] == "gradient"
    assert {"from", "to", "label"} <= meta["fallback"].keys()


def test_exporter_rejects_unknown_provider():
    """图源不在白名单（如被换成媒体原图）→ 降级占位。"""
    art = SimpleNamespace(
        topic="tech",
        title_original="Chips",
        image_url="https://cdn.arstechnica.net/x.jpg",
        image_credit="",
        image_provider="arstechnica",
        image_credit_url="",
    )
    assert _image_meta(art)["type"] == "gradient"


def test_rest_schema_exposes_structured_image():
    """REST 出参的 image 必须与静态 JSON 完全一致（不能一处有一处无）。"""
    from app.schemas import ArticleSummaryOut

    art = SimpleNamespace(
        topic="tech",
        title_original="Chips",
        image_url="https://images.unsplash.com/photo-1?w=1600",
        image_credit="Photo by Annie Spratt on Unsplash",
        image_provider="unsplash",
        image_credit_url="https://unsplash.com/@anniespratt?utm_source=x&utm_medium=referral",
        image_source_url="https://unsplash.com/photos/abc123?utm_source=x&utm_medium=referral",
        image_author="Annie Spratt",
        source="", source_url="",
        published_date=dt.date.today(), published_at=None,
        summary_original="", body_original="", title_original2="", fetched_at=None,
    )
    ver = SimpleNamespace(
        id=1, article_id=1, level_code="A1", lang="en", level=1, level_label="A1",
        title="T", paragraphs=["p"], word_count=1, reading_minutes=1.0, lead="",
        vocab=[], audio=None, content_hash="x",
    )
    out = ArticleSummaryOut.from_row(ver, art)
    assert out.image["type"] == "photo"
    assert out.image["credit"] == "Photo by Annie Spratt on Unsplash"
    assert out.image["credit_url"].startswith("https://unsplash.com/@anniespratt")
    assert out.image["source"] == "Unsplash"          # 显示名，不是 provider 标识
    assert out.image["author"] == "Annie Spratt"
    assert out.image["source_url"].startswith("https://unsplash.com/photos/abc123")
    assert out.image["fallback"]["type"] == "gradient"
    # 旧客户端仍在读 image_url，必须保留
    assert out.image_url == art.image_url


def test_exporter_gradient_when_no_image():
    art = SimpleNamespace(
        topic="world",
        title_original="No image",
        image_url="",
        image_credit="",
        image_provider="",
        image_credit_url="",
    )
    meta = _image_meta(art)
    assert meta["type"] == "gradient"
    assert meta["label"] == "WORLD"
