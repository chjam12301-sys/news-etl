#!/usr/bin/env bash
# 重新生成内容（含中文译文 + CC0 配图）
# 密钥从 GitHub Secrets 读取，不经过对话、不落盘。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "请粘贴 DeepSeek API Key（不回显；直接回车则跳过翻译，走离线模式）"
read -rsp "  DEEPSEEK_API_KEY: " DS_KEY
echo
if [ -n "$DS_KEY" ]; then
  export DEEPSEEK_API_KEY="$DS_KEY"
  export LLM_PROVIDER=deepseek
  export DEEPSEEK_BASE_URL="https://api.deepseek.com/v1"
  export DEEPSEEK_MODEL="deepseek-chat"
  echo "✓ 使用 DeepSeek（会消耗额度：10 个等级 ≈ 10 次调用）"
else
  echo "⚠ 无 key，走离线模式（无中文译文）"
fi

echo
echo "开始生成…（10 个等级 + 20 段音频，约 4-8 分钟）"
echo
.venv/bin/python scripts/run_full.py "$@"
