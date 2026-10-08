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

        # 推送后重写 latest.json —— 它必须指向**含本次内容**的 commit。
        # 放在提交之后写，是因为 ref 只有提交后才确定；
        # 若放提交之前，ref 会指向上一个 commit，App 按此拉取就拿不到新内容。
        try:
            self._refresh_latest_pointer(self.current_ref())
        except Exception as exc:  # noqa: BLE001
            log.warning("[gh] 更新 latest.json 失败: %s", exc)

        log.info("[gh] 已推送 %s", message)
        return True

    def _refresh_latest_pointer(self, ref: str = "") -> None:
        """重写 content/latest.json（两个路径都写），使其指向含本次内容的 commit。

        `ref` 传本次内容提交后的 SHA。因为紧接着还要为 latest.json 本身
        提交一次，若改用那之后的 HEAD 就会自我指向、永远错位一个 commit。
        """
        import datetime as _dt
        import json as _json

        ref = ref or self.current_ref()
        if not ref:
            return

        # 自检：ref 必须真的含有最新的 index.json，否则 App 按此拉取会拿到旧内容。
        # （若因提交顺序错位而指错，这里能立刻发现并纠正。）
        from .storage import index_key

        probe = self._git("show", f"{ref}:{index_key()}", check=False)
        if probe.returncode != 0:
            alt = self._git("rev-parse", "HEAD", check=False).stdout.strip()
            log.warning("[gh] latest.json 的 ref=%s 不含索引，改用 HEAD=%s",
                        ref, alt[:7])
            ref = alt or ref

        cdn = self.cdn_base.replace("@main", f"@{ref}")
        # main_url 用 @main 而非固定 ref —— 因为本文件要靠它自己刷新：
        # 若自身也钉在某个 ref 上，jsDelivr 缓存会让它永远停在旧值，
        # App 就再也拿不到新的 index_url。
        payload = {
            "ref": ref,
            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "index_url": f"{cdn}/{CONTENT_DIR}/index.json",
            "main_index_url": (
                f"{self.cdn_base}/{CONTENT_DIR}/index.json"
            ),
            "hint": (
                "取 index_url（固定 ref，无缓存问题）；"
                "若想每次都拿最新，可读 main_index_url 但需自行校验 "
                "其 version.published_at 是否够新"
            ),
        }
        raw = _json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        for p in (
            self.content_root / "latest.json",
            self.content_root / "data" / "latest.json",
        ):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)

        # 这次写本身也是一次提交，推上去才算生效
        self._git("add", "-f", CONTENT_DIR, check=False)
        r = self._git("status", "--porcelain", "--", CONTENT_DIR, check=False)
        if r.stdout.strip():
            self._git("commit", "-m", f"chore: latest.json → {ref}", check=False)
            self._git("push", "origin", self.branch, check=False)
        log.info("[gh] latest.json 已指向 @%s", ref)

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