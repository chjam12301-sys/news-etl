"""把 content/audio 下的存量音频上传到对象存储，并把它们移出 Git。

## 为什么

jsDelivr 对单个 GitHub 仓库有 50MB 硬上限，超限后**任何新路径**都返回 403
（"Package size exceeded the configured limit of 50 MB"）。音频 214MB 是唯一
超标项，JSON 只有 17MB。把音频搬去对象存储后，仓库即可长期留在上限以内。

## 用法

在 GitHub Actions 里跑（那里才有 R2 密钥）：

    python -m scripts.upload_audio              # 上传 + 逐个校验
    python -m scripts.upload_audio --drop-git   # 上传后再从 Git 移除并提交

本地没有密钥时可以先 dry-run 看会做什么：

    python -m scripts.upload_audio --dry
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AUDIO_ROOT = REPO / "content" / "audio"


def _git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=check
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只列出将上传的文件，不真的传")
    ap.add_argument("--drop-git", action="store_true", help="上传后从 Git 移除 content/audio")
    ap.add_argument("--force", action="store_true", help="即使已存在也重新上传")
    args = ap.parse_args()

    from app.audio_store import audio_base_url, get_audio_storage

    base = audio_base_url()
    if not base:
        print("✗ 未配置音频对外基址：需要 AUDIO_PUBLIC_BASE 或 R2_PUBLIC_BASE", file=sys.stderr)
        return 2
    print(f"音频对外基址：{base}")

    if not AUDIO_ROOT.is_dir():
        print(f"✗ 没有 {AUDIO_ROOT}", file=sys.stderr)
        return 2

    files = sorted(AUDIO_ROOT.rglob("*.mp3")) + sorted(AUDIO_ROOT.rglob("*.wav"))
    if not files:
        print("没有待上传的音频")
        return 0

    total_mb = sum(f.stat().st_size for f in files) / 1024 / 1024
    print(f"待处理 {len(files)} 个文件，共 {total_mb:.1f}MB")
    if args.dry:
        for f in files[:5]:
            print(f"  audio/{f.relative_to(AUDIO_ROOT).as_posix()}")
        print("  ...")
        return 0

    import shutil

    aws = shutil.which("aws")
    print(f"aws cli：{aws or '未安装（TLS 兜底通道不可用）'}")
    if aws:
        print("  " + subprocess.run([aws, "--version"], capture_output=True,
                                    text=True).stdout.strip().splitlines()[0])

    store = get_audio_storage()
    print(f"存储后端：{getattr(store, 'name', store)}")

    ok = fail = skipped = 0
    failures: list[str] = []
    for i, f in enumerate(files, 1):
        if i % 50 == 0:
            print(f"  ...{i}/{len(files)}")
        key = f"audio/{f.relative_to(AUDIO_ROOT).as_posix()}"
        data = f.read_bytes()
        try:
            if not args.force and store.exists(key):
                skipped += 1
            else:
                store.put(
                    key,
                    data,
                    content_type="audio/mpeg" if f.suffix == ".mp3" else "audio/wav",
                )
            if store.exists(key):
                ok += 1
            else:
                fail += 1
                failures.append(f"{key}: 上传后探测不到")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            failures.append(f"{key}: {exc}")

    print(f"\n上传完成：成功 {ok}，跳过（已存在）{skipped}，失败 {fail}")
    for line in failures[:10]:
        print(f"  ✗ {line}")
    if fail:
        return 1

    if not args.drop_git:
        print("\n未指定 --drop-git，content/audio 仍在 Git 里")
        return 0

    # ---- 移出 Git ------------------------------------------------------
    print("\n从 Git 移除 content/audio ...")
    _git("rm", "-r", "--cached", "--quiet", "content/audio")
    gi = REPO / ".gitignore"
    marker = "# 音频不入库：jsDelivr 单仓库 50MB 上限，音频由对象存储提供"
    text = gi.read_text() if gi.exists() else ""
    if "content/audio/" not in text:
        gi.write_text(text.rstrip() + f"\n\n{marker}\ncontent/audio/\n")
    _git("add", ".gitignore")
    _git("commit", "-m", "content: 音频迁出 Git（改由对象存储提供）")
    p = _git("push", "origin", "main")
    if p.returncode != 0:
        print(f"✗ 推送失败: {p.stderr[:300]}", file=sys.stderr)
        return 1
    print("✓ 已推送")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
