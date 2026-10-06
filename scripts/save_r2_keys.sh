#!/usr/bin/env bash
# 把 R2 密钥存到本机 .env（已 gitignore，不会进仓库）
# 之后所有命令都自动读取，不用反复粘贴。
#
# 用法：直接回车即可 —— 会提示你去哪里复制。
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"

if [ -f "$ENV_FILE" ]; then
  echo "已存在 $ENV_FILE，将更新 R2 相关项。"
else
  : > "$ENV_FILE"
  echo ".env" >> .gitignore 2>/dev/null || true
fi

set_key() {
  local key="$1" val="$2"
  # 删掉旧行，再追加
  grep -v "^${key}=" "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || true
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  echo "${key}=${val}" >> "$ENV_FILE"
}

echo
echo "================================================================"
echo " 需要两个值，都在 Cloudflare R2 的 API Token 页面"
echo "================================================================"
echo
echo " 打开：https://dash.cloudflare.com → 左侧 R2 →"
echo "      Manage R2 API Tokens（或 Account Details → API Tokens）"
echo "      点Create API Token"
echo "        权限      : Object Read & Write"
echo "        指定 bucket: 只勾 news-etl-audio"
echo "      创建后会显示两串字符（Secret 只显示这一次）"
echo
echo "----------------------------------------------------------------"

read -rp "  R2_ACCESS_KEY_ID: " AK
echo
read -rsp "  R2_SECRET_ACCESS_KEY（不回显）: " SK
echo

if [ -z "$AK" ] || [ -z "$SK" ]; then
  echo "未输入完整，已取消。"
  exit 1
fi

set_key R2_ACCESS_KEY_ID "$AK"
set_key R2_SECRET_ACCESS_KEY "$SK"
set_key R2_BUCKET "news-etl-audio"
set_key R2_ENDPOINT "https://f3e8db4fa711d0a98d3a27def84cd87f.r2.cloudflarestorage.com"
set_key R2_PUBLIC_BASE "https://pub-aba43a6fb1db4dc08fede1dbc81f3241.r2.dev"
set_key PUBLIC_BASE_URL "https://pub-aba43a6fb1db4dc08fede1dbc81f3241.r2.dev"

chmod 600 "$ENV_FILE"

echo
echo "✓ 已保存到 .env（权限 600，已 gitignore）"
echo "  以后跑任何命令都不用再输入密钥了。"
echo
echo "  下一步执行："
echo "    .venv/bin/python scripts/backfill_audio.py"
echo