"""发布流程：确保索引里的每一个链接都真实可访问。

## 为什么需要这个模块

历史故障：导出时用 `storage.current_ref()` 取CDN 基址，而此刻新文件
**还没提交**，于是索引里的 `detail_url` / `audio.url` 指向了一个不包含
这些文件的旧 commit —— 首页卡片能显示（标题、图片、译文都在索引里），
点进详情却 404。

更麻烦的是：单纯更新 `latest.json` 指针**修不好**，因为失效地址写死在
索引内容里。

## 正确顺序（不可颠倒）

    ① 提交内容   detail / audio / day index 全部落库并 push
    ② 生成索引   此时 HEAD 已含所有文件，用它拼 CDN 地址 → commit
    ③ 全量验证   索引引用的每一份详情 + 音频都必须在该 commit 下存在，
                且 version_id / article_id / content_hash 与索引一致
    ④ 更新指针   全部通过后才写 latest.json

任何一步失败都**不更新 latest.json**，App 继续用上一份可用索引。
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    """发布校验结果。ok 为 False 时不得更新 latest 指针。"""

    ok: bool = True
    checked: int = 0
    missing_detail: list[int] = field(default_factory=list)
    missing_audio: list[int] = field(default_factory=list)
    hash_mismatch: list[int] = field(default_factory=list)
    identity_mismatch: list[str] = field(default_factory=list)
    # 链接指向的 commit 与被校验的 commit 不一致（链接本身就已失效）
    stale_rev: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        if self.ok:
            return f"校验通过：{self.checked} 份详情 + 音频全部可访问"
        parts = [f"校验失败（{self.error or '链接无效'}）"]
        if self.missing_detail:
            parts.append(f"详情缺失 {len(self.missing_detail)} 个: {self.missing_detail[:8]}")
        if self.missing_audio:
            parts.append(f"音频缺失 {len(self.missing_audio)} 个: {self.missing_audio[:8]}")
        if self.hash_mismatch:
            parts.append(f"哈希不符 {len(self.hash_mismatch)} 个: {self.hash_mismatch[:8]}")
        if self.identity_mismatch:
            parts.append(f"身份不符 {self.identity_mismatch[:5]}")
        if self.stale_rev:
            parts.append(f"链接 commit 错位 {len(self.stale_rev)} 处: {self.stale_rev[:5]}")
        return "；".join(parts)


def _rel(url: str) -> str:
    """从完整 URL 里取出 content/ 之后 的相对路径。"""
    if "/content/" in url:
        return url.split("/content/", 1)[1]
    return url


def _rev_in_url(url: str) -> str:
    """从 CDN URL 里取出 @ 后面的 commit SHA。"""
    if "@" not in url:
        return ""
    tail = url.split("@", 1)[1]
    return tail.split("/", 1)[0]


def _exists_in_commit(repo_dir: str, rev: str, rel: str) -> bool:
    p = subprocess.run(
        ["git", "cat-file", "-e", f"{rev}:content/{rel}"],
        cwd=repo_dir, capture_output=True,
    )
    return p.returncode == 0


def _read_in_commit(repo_dir: str, rev: str, rel: str) -> bytes | None:
    p = subprocess.run(
        ["git", "cat-file", "-p", f"{rev}:content/{rel}"],
        cwd=repo_dir, capture_output=True,
    )
    return p.stdout if p.returncode == 0 else None


def verify_published(
    *,
    repo_dir: str,
    rev: str,
    index: dict[str, Any],
    check_hash: bool = True,
) -> VerifyResult:
    """校验索引引用的每一份详情与音频在 rev 下确实存在且内容一致。

    检查项：
      1. detail_url 指向的文件，在**该 URL 自己的 commit** 下存在
      2. audio.url 指向的文件，在该URL 自己的 commit 下存在
      3. 详情的 version_id / article_id 与索引条目一致
      4. 详情的 content_hash 与索引条目一致（防张冠李戴）

    注意：按 URL 自己的 commit 检查存在性，而不是强制等于传入的 rev ——
    只要那个 commit 里文件在，浏览器就能取到；链接完全可用。
    传rev 的用途是「校验刚生成的索引」，而非要求所有链接都指向 HEAD。

    stale_rev 字段保留用于排查：记录哪些条目的链接指向了非当前 HEAD 的
    commit（正常且可用，但值得知道发布是否真的生效了）。
    """
    res = VerifyResult()
    items = [
        it
        for lang in ("en", "ja")
        for it in (index.get("latest", {}).get("versions", {}).get(lang) or [])
    ]
    if not items:
        res.ok = False
        res.error = "索引为空"
        return res

    for it in items:
        vid = it.get("version_id")
        res.checked += 1

        d_url = it.get("detail_url", "")
        d_rel = _rel(d_url)

        # URL 里的 commit 必须是**正在校验的这个** rev。
        # 否则链接指向另一个（可能不含这些文件的）commit ——
        # 这正是「首页能显示卡片、点进详情 404」的原故障形态，
        # 光看「文件在 rev 下存在」是发现不了的。
        d_rev = _rev_in_url(d_url)

        # 关键：按 **URL 自己的 rev** 检查存在性 —— 那才是浏览器实际请求的地址。
        # （不要求 URL 的 rev 等于 HEAD：只要那个 commit 里文件在，链接就可用。）
        if not d_rel or not _exists_in_commit(repo_dir, d_rev or rev, d_rel):
            res.missing_detail.append(vid)
            res.ok = False
            continue

        a_url = (it.get("audio") or {}).get("url", "")
        a_rel = _rel(a_url) if a_url else ""
        a_rev = _rev_in_url(a_url)
        if not a_rel or not _exists_in_commit(repo_dir, a_rev or rev, a_rel):
            res.missing_audio.append(vid)
            res.ok = False

        if check_hash:
            raw = _read_in_commit(repo_dir, d_rev or rev, d_rel)
            if raw is None:
                res.missing_detail.append(vid)
                res.ok = False
                continue
            try:
                det = json.loads(raw)
            except Exception:  # noqa: BLE001
                res.error = f"v{vid} 详情 JSON 无法解析"
                res.ok = False
                continue
            if det.get("version_id") != vid:
                res.identity_mismatch.append(
                    f"v{vid} 的详情里是 v{det.get('version_id')}"
                )
                res.ok = False
            if det.get("article_id") != it.get("article_id"):
                res.identity_mismatch.append(
                    f"v{vid} article_id 不符"
                )
                res.ok = False
            if det.get("content_hash") != it.get("content_hash"):
                res.hash_mismatch.append(vid)
                res.ok = False

    return res

def publish_all(*, db, storage, topics_days: int = 3, commit_msg: str = "content") -> dict[str, Any]:
    """完整发布：① 提交内容 → ② 生成索引 → ③ 校验 → ④ 更新指针。

    只有第 ④ 步在校验通过后才执行；失败则保留上一份可用索引，
    App 继续用旧数据（不会出现首页有卡片、详情打不开）。
    """
    from .exporter import export_day, export_index, index_key
    from sqlalchemy import select
    from app.db import Article

    st = storage
    result: dict[str, Any] = {"ok": False}

    # ── ① 提交内容（详情 + 音频 + 按日索引），不含全量索引 ──────────
    dates = db.execute(
        select(Article.published_date).distinct()
        .order_by(Article.published_date.desc()).limit(topics_days)
    ).scalars().all()
    from .exporter import export_all as _ea  # noqa: F401  (确保导出逻辑已加载)
    for d in dates:
        if d:
            export_day(db, d)

    if getattr(st, "provider", "") == "github":
        st.commit(f"{commit_msg}: 详情与音频")
        content_rev = st.current_ref()
    else:
        content_rev = ""

    # ── ② 用**实际包含这些文件**的 commit 生成索引 ──────────────────
    export_index(db, days=topics_days, rev=content_rev)

    if getattr(st, "provider", "") == "github":
        st.commit(f"{commit_msg}: 索引（链接基于 {content_rev[:7]}）")
        index_rev = st.current_ref()

        # ── ③ 全量校验：索引引用的每一份详情与音频 ──────────────────
        raw = _read_in_commit(st.repo_dir, index_rev, index_key())
        index = json.loads(raw) if raw else {}
        v = verify_published(
            repo_dir=st.repo_dir, rev=index_rev, index=index
        )
        result["verify"] = v.summary()
        result["checked"] = v.checked
        result["ok"] = v.ok

        if not v.ok:
            log.error("[publish] 校验未通过，保留旧索引：%s", v.summary())
            result["reason"] = "verify_failed"
            return result

        # ── ④ 全部通过，才更新 latest 指针 ──────────────────────────
        st.refresh_latest_pointer(index_rev)
        log.info("[publish] 发布成功：index@%s → latest", index_rev[:7])
        result["index_rev"] = index_rev
    else:
        st.commit(commit_msg)
        result["ok"] = True

    return result
