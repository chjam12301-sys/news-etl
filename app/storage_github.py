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
        # 仓库根目录 —— 供发布校验用 git cat-file 查某个 commit 下文件是否存在
        self.repo_dir = str(self.local_dir)
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
        # runner 上默认没有 git 身份，`git commit` 会直接失败；此时 push 无内容
        # 可推、返回 0，于是「已推送」的日志是假的（曾导致 latest 指向旧索引）。
        if not self._git("config", "user.email", check=False).stdout.strip():
            self._git("config", "user.email",
                      "github-actions[bot]@users.noreply.github.com", check=False)
            self._git("config", "user.name", "github-actions[bot]", check=False)
            log.info("[gh] 已设置 bot 提交身份")
        before = self.current_ref()
        # content/ 在 .gitignore 里（避免日常 git add 误提交），
        # 这里用 -f 强制加入 —— 内容本来就该进仓库供CDN 读取
        self._git("add", "-f", CONTENT_DIR, check=False)
        # 音频例外：jsDelivr 单仓库 50MB 上限，音频一律由对象存储提供。
        # -f 会无视 .gitignore，所以这里显式摘掉，防止历史残留文件被重新入库。
        self._git("rm", "-r", "--cached", "--quiet", "--ignore-unmatch",
                  f"{CONTENT_DIR}/audio", check=False)
        r = self._git("status", "--porcelain", "--", CONTENT_DIR, check=False)
        if not r.stdout.strip():
            log.info("[gh] content/ 无变化")
            return False
        c = self._git("commit", "-m", message, check=False)
        if self.current_ref() == before:
            # 没有新 commit就往下走的话，push 会「成功」但远端根本没变，
            # latest 于是指向一个不含新内容的索引 —— 必须硬失败。
            raise RuntimeError(
                f"[gh] 提交未生效: {c.stderr.strip()[:200] or c.stdout.strip()[:200]}"
            )
        p = self._git("push", "origin", self.branch, check=False)
        if p.returncode != 0:
            # 并发推送可能失败。rebase 冲突时 git 会把冲突标记写进工作区文件，
            # 一旦提交上去，CDN 上的 JSON 就废了（曾导致 content/index.json
            # 带<<<<<<< 而无法解析）。因此：先 stash 保护产物，
            # rebase 失败就放弃本地提交、保住远端版本。
            log.warning("[gh] 推送冲突，重试（rebase）")
            time.sleep(2)
            self._git("stash", "push", "--include-untracked",
                      "--", CONTENT_DIR, check=False)
            self._git("fetch", "origin", self.branch, check=False)
            self._git("reset", "--hard", f"origin/{self.branch}", check=False)
            self._git("stash", "pop", check=False)
            # 复查产物是否被冲突标记污染
            if self._content_has_conflict_markers():
                log.error("[gh] 产物含冲突标记，放弃本次提交（保住远端旧版）")
                self._git("checkout", "--", ".", check=False)
                return False
            p = self._git("push", "origin", self.branch, check=False)
            if p.returncode != 0:
                raise RuntimeError(f"[gh] 推送失败: {p.stderr[:200]}")

        # 自检：本地这个 commit 必须真的在远端，否则「已推送」依然是假的
        head = self.current_ref()
        self._git("fetch", "origin", self.branch, check=False)
        if self._git("merge-base", "--is-ancestor", head,
                     f"origin/{self.branch}", check=False).returncode != 0:
            raise RuntimeError(f"[gh] 推送后远端仍无 {head}，本次发布不可信")

        # 这里**不**更新 latest.json。
        # 索引里的链接必须在内容提交之后生成、再经校验，才可以更新指针，
        # 顺序由 app/publish.publish_all 统一编排。
        log.info("[gh] 已推送 %s → %s", message, head)
        return True

    def _content_has_conflict_markers(self) -> bool:
        """检查产物里是否混入了 git 冲突标记。"""
        for p in self.content_root.rglob("*.json"):
            try:
                head = p.read_text(encoding="utf-8", errors="replace")[:200]
            except OSError:
                continue
            if head.startswith("<<<<<<<") or "\n<<<<<<<" in head or head.startswith(">>>>>>>"):
                log.error("[gh] 发现冲突标记: %s", p)
                return True
        return False

    def refresh_latest_pointer(self, ref: str = "") -> None:
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

        # 这次写本身也是一次提交，推上去才算生效。
        # 走 _commit 统一路径：那里会补 git 身份、并在推送后核对远端确有该commit
        # （此前这里自己写的一套「commit + push」不带身份，失败时静默假成功）。
        if self._commit(f"chore: latest.json → {ref}"):
            log.info("[gh] latest.json 已指向 @%s", ref)
        else:
            log.info("[gh] latest.json 无变化，仍指向 @%s", ref)

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

    def prune(self, keep_days: int = RETENTION_DAYS,
             max_mb: float = 0.0) -> dict[str, Any]:
        """删除过期内容，避免仓库无限膨胀。

        两个维度：
          1. keep_days —— 按音频目录名（文章 id）与版本 JSON 里的
             published_date 判断，与 mtime 无关。CI 里 checkout 会把所有
             文件的 mtime 重置成checkout 时间，按 mtime 判会导致永远清不掉。
          2. max_mb —— 总量上限。jsDelivr 单仓库超过 50MB 会全站返回
             403（Package size exceeded），必须留足余量。
        """
        import datetime as dt
        import json as _json

        cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
        removed = 0
        freed = 0

        def _pub_date(p: Path) -> str:
            """尽力取出该文件对应的发布日期。"""
            if p.suffix == ".json":
                try:
                    d = _json.loads(p.read_text(encoding="utf-8"))
                    return str(d.get("published_date") or d.get("date") or "")
                except Exception:  # noqa: BLE001
                    return ""
            # audio/<article_id>/<level>.mp3 -> 查该article 的最新 JSON
            if p.parent.parent.name == "audio":
                aid = p.parent.name
                for vp in (self.content_root / "data" / "versions").glob("*.json"):
                    try:
                        d = _json.loads(vp.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001
                        continue
                    if str(d.get("article_id")) == aid:
                        return str(d.get("published_date") or "")
                return ""
            return ""

        for f in sorted(self.content_root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if not f.is_file():
                continue
            if f.name == "index.json" and f.parent == self.content_root:
                continue

            # 先按体积上限删最旧的，直到降到上限内
            over = False
            if max_mb > 0:
                total = self.used_bytes()
                if total <= max_mb * 1024 * 1024:
                    over = False
                else:
                    over = True

            d = _pub_date(f)
            expired = bool(d) and d < cutoff
            if not (expired or over):
                continue
            try:
                freed += f.stat().st_size
                f.unlink()
                removed += 1
            except OSError:
                pass

        log.info("[gh] 清理 %d 个文件，释放 %.1f MB（保留 %d 天，上限 %s MB）",
                 removed, freed / 1024 / 1024, keep_days,
                 f"{max_mb:.0f}" if max_mb else "无")
        return {"deleted": removed, "bytes": freed,
                "size_mb": round(self.used_bytes() / 1024 / 1024, 1)}

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