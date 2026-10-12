"""Submit optional voices after publication; errors never roll back published content."""
from __future__ import annotations

import asyncio
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import ArticleVersion
from .levels import EN_LEVELS

WORKER_URL = "https://day-voice-api.innertalkdiary.workers.dev"
VOICES = ("ava", "emma", "sonia", "guy")


async def _request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    for attempt in range(3):
        try:
            response = await client.request(method, path, **kwargs)
            if response.status_code in (404, 409, 429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            response.raise_for_status()
            return response.json()
        except httpx.TransportError:
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** (attempt + 1))
    raise RuntimeError("request retries exhausted")


async def warmup_article(db: Session, article_id: int, *, transport=None) -> dict:
    result = {"article_id": article_id, "accepted": 0, "failed": 0, "items": []}
    if not settings.voice_warmup_token:
        return {**result, "status": "skipped", "reason": "missing_server_token"}
    if settings.voice_worker_url.rstrip("/") != WORKER_URL:
        raise ValueError("untrusted voice worker URL")
    versions = {v.level_code: v for v in db.scalars(select(ArticleVersion).where(
        ArticleVersion.article_id == article_id, ArticleVersion.lang == "en"))}
    async with httpx.AsyncClient(base_url=WORKER_URL, transport=transport, timeout=20,
                                follow_redirects=False,
                                headers={"Authorization": f"Bearer {settings.voice_warmup_token}"}) as client:
        for level in EN_LEVELS:
            version = versions.get(level.code)
            base = f"/v1/articles/{article_id}/levels/{level.code}/versions/{version.id}" if version else ""
            try:
                if version is None or not version.content_hash:
                    raise ValueError("missing published version")
                manifest = await _request(client, "GET", base + "/voice-manifest",
                                          params={"content_hash": version.content_hash})
                if (str(manifest.get("article_id")) != str(article_id)
                        or str(manifest.get("content_version")) != str(version.id)
                        or manifest.get("level_code") != level.code
                        or manifest.get("legacy_content_hash") != version.content_hash
                        or not re.fullmatch(r"[a-f0-9]{64}", manifest.get("text_identity", ""))):
                    raise ValueError("manifest identity mismatch")
            except Exception as exc:
                for voice in VOICES:
                    result["items"].append({"level": level.code, "voice": voice,
                                            "status": "failed", "error": type(exc).__name__})
                result["failed"] += 4
                continue
            for voice in VOICES:
                item = {"level": level.code, "voice": voice}
                try:
                    job = await _request(client, "POST", base + f"/voices/{voice}:ensure", json={
                        "legacy_content_hash": version.content_hash, "text_identity": manifest["text_identity"]})
                    if job.get("status") not in ("ready", "pending", "processing"):
                        raise ValueError("generation not accepted")
                    item.update(status=job["status"], job_id=job.get("job_id"))
                    result["accepted"] += 1
                except Exception as exc:
                    item.update(status="failed", error=type(exc).__name__)
                    result["failed"] += 1
                result["items"].append(item)
    result["status"] = "accepted" if result["failed"] == 0 else "partial"
    return result
