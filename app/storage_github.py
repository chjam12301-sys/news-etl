"""GitHub 仓库 + jsDelivr CDN 作为对象存储（最后的可用方案）。

为什么用这个
------------
实测发现用户网络环境的特点是「**读取 CDN 通、上传 API 不通**」：

    pub-xxx.r2.dev（读）                ✅ 404 = 通
    xxx.r2.cloudflarestorage.com（写）  ❌ 000
    github.com                ✅ 200
    cdn.jsdelivr.net          ✅ 301
    *.supabase.co             ❌ 代理节点 TLS 失败

也就是说所有「上传 API 域名」都被阻断，而 GitHub 恰好是例外 —— 代码推送全程正常。
于是把音频与 JSON 直接提交进仓库，App 从 jsDelivr CDN 读。

免费额度
--------
- GitHub 公开仓库：无限存储，单文件 100MB 上限
- jsDelivr：每月 10GB 流量（我们每月约 1GB）

代价与对策
----------
仓库会随天数变大（每天约 30MB）。故设RETENTION_DAYS = 14，
只保留最近 14 天，稳定在 400-500MB，远低于 GitHub 1GB 的软警告线。
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import StoredObject

log = logging.getLogger("ghstorage")

# 保留天数（越大仓库越大，但历史内容越多）
RETENTION_DAYS = 14
CONTENT_DIR = "content"


@dataclass
class GitHubStorage:
    """把对象写进 Git 仓库，通过 jsDelivr 分发。"""

    name = "github"
    provider = "github"

    def __init__(
        self,
        repo: str,
        *,
        branch: str = "main",
        local_dir: str = ".",
        cdn_base: str = "",
        token: str = "",
    ) -> None:
        self.repo = repo            # owner/name
        self.branch = branch
        self.local_dir = Path(local_dir).resolve()
        self.token = token
        self.content_root = self.local_dir / CONTENT_DIR

        if not cdn_base:
            owner, name = repo.split("/", 1)
            cdn_base = f"https://cdn.jsdelivr.net/gh/{owner}/{name}@{branch}"
        self.cdn_base = cdn_base.rstrip("/")

        self.content_root.mkdir(parents=True, exist_ok=True)

    # ---- git 操作 ------------------------------------------------------- #
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = None
        if self.token:
            import os

            env = dict(os.environ)
            env["GIT_ASKPASS"] = "echo"
            env["GIT_USERNAME"] = "x-access-token"
            env["GIT_PASSWORD"] = self.token
        return subprocess.run(
            ["git", *args],
            cwd=self.local_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=check,
        )

    def _commit(self, message: str) -> bool:
        """把content/ 的变更提交并推送。返回是否有实际推送。"""
        # content/ 在 .gitignore 里（避免日常 git add 误提交），
        # 这里用 -f 强制加入 —— 内容本来就该进仓库供CDN 读取
        self._git("add", "-f", CONTENT_DIR, check=False)
        r = self._git("status", "--porcelain", "--", CONTENT_DIR, check=False)
        if not r.stdout.strip():
            log.info("[gh] content/ 无变化")
            return False
        self._git("commit", "-m", message, check=False)
        p = self._git("push", "origin", self.branch, check=False)
        if p.returncode != 0:
            # 并发推送可能失败，重试一次
            time.sleep(2)
            self._git("pull", "--rebase", "--autostash", "origin", self.branch, check=False)
            p = self._git("push", "origin", self.branch, check=False)
            if p.returncode != 0:
                raise RuntimeError(f"[gh] 推送失败: {p.stderr[:200]}")
        log.info("[gh] 已推送 %s", message)
        return True

    # ---- Storage 协议 --------------------------------------------------- #
    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> StoredObject:
        """写入文件。批量场景下先攒一批再 commit，故默认不立即推送。"""
        target = self.content_root / key.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return StoredObject(key=key, size=len(data), url=self.public_url(key))

    def commit(self, message: str = "content: 更新内容") -> bool:
        """由流水线显式调用，一次性提交本轮所有新增文件。"""
        return self._commit(message)

    def exists(self, key: str) -> bool:
        return (self.content_root / key.lstrip("/")).is_file()

    def read(self, key: str) -> bytes | None:
        p = self.content_root / key.lstrip("/")
        return p.read_bytes() if p.is_file() else None

    def delete(self, key: str) -> None:
        p = self.content_root / key.lstrip("/")
        if p.is_file():
            p.unlink()

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self.content_root / prefix.lstrip("/") if prefix else self.content_root
        if not base.exists():
            return []
        if base.is_file():
            return [prefix]
        root = len(self.content_root.parts)
        return [
            str(f.relative_to(self.content_root).as_posix())
            for f in base.rglob("*")
            if f.is_file()
        ]

    def public_url(self, key: str, version: str = "") -> str:
        """生成公开 URL。

        `version` 通常传commit SHA。jsDelivr 按「ref + 路径」做缓存，
        用 commit SHA 而非分支名，可彻底规避 @main 的缓存滞后问题
        （实测 @main 会返回旧版：timeline 为空，而 @<sha> 是最新的）。
        """
        base = self.cdn_base
        if version and "@main" in base:
            base = base.replace("@main", f"@{version}")
        return f"{base}/{CONTENT_DIR}/{key.lstrip('/')}"

    def current_ref(self) -> str:
        """当前仓库的 commit SHA（短），用作 cache-buster。"""
        r = self._git("rev-parse", "--short", "HEAD", check=False)
        return r.stdout.strip()

    def iter_prefix(self, prefix: str) -> list[StoredObject]:
        return [
            StoredObject(key=k, size=(self.content_root / k).stat().st_size)
            for k in self.list_keys(prefix)
        ]

    # ---- 容量与清理 ----------------------------------------------------- #
    def used_bytes(self) -> int:
        total = 0
        for f in self.content_root.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
        return total

    def prune(self, keep_days: int = RETENTION_DAYS) -> dict[str, Any]:
        """删除 keep_days 天前的音频与 JSON，避免仓库无限膨胀。

        按文件修改时间判定，保留每天的 index.json（最新一天要留）。
        """
        import datetime as dt

        cutoff = dt.datetime.now().timestamp() - keep_days * 86400
        removed = 0
        freed = 0

        for f in self.content_root.rglob("*"):
            if not f.is_file():
                continue
            if f.stat().st_mtime >= cutoff:
                continue
            # 当天的总索引永远保留
            if f.name == "index.json" and f.parent == self.content_root:
                continue
            try:
                freed += f.stat().st_size
                f.unlink()
                removed += 1
            except OSError as exc:
                log.warning("[gh] 删除 %s 失败: %s", f, exc)

        # 清掉空目录
        for d in sorted(self.content_root.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass

        if removed:
            self._commit(f"content: 清理 {keep_days} 天前的旧文件")
        log.info("[gh] 清理完成：删除 %d 个文件，释放 %.1f MB", removed, freed / 1024 / 1024)
        return {"deleted": removed, "bytes": freed}

    def prune_json(self, keep_days: int = RETENTION_DAYS) -> dict[str, Any]:
        """JSON 体积小，可以留久一点。"""
        return self.prune(keep_days)


def build_from_env(settings) -> GitHubStorage | None:
    """从配置构造；未配置则返回 None。"""
    repo = getattr(settings, "github_content_repo", "") or ""
    if not repo:
        return None
    return GitHubStorage(
        repo=repo,
        branch=getattr(settings, "github_branch", "main") or "main",
        local_dir=str(getattr(settings, "data_path", ".")),
        cdn_base=getattr(settings, "github_cdn_base", "") or "",
        token=getattr(settings, "github_token", "") or "",
    )