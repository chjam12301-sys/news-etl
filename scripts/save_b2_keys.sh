#!/usr/bin/env bash
# 把 Backblaze B2 密钥存到本机 .env（已 gitignore，不会进仓库）
# 之后所有命令都自动读取，不用反复粘贴。
#
# 用法：bash scripts/save_b2_keys.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"

if [ -f "$ENV_FILE" ]; then
  echo "已存在 .env，将更新 B2 相关项。"
else
  : > "$ENV_FILE"
  grep -qx '.env' .gitignore 2>/dev/null || echo ".env" >> .gitignore
fi

set_key() {
  local key="$1" val="$2"
  grep -v "^${key}=" "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || true
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  echo "${key}=${val}" >> "$ENV_FILE"
}

cat <<'BANNER'

================================================================
 Backblaze B2 免费 10GB，S3 兼容，无需绑卡
================================================================

 1) 注册并登录 https://www.backblazeb2.com
 2) 左侧 Buckets → Create Bucket
      Name      : news-etl-audio
      Files     : Public（这样 App 能直接读音频）
      Encryption: SSE-B2 即可
 3) 左侧 App Keys → Add Key
      Name           : news-etl
      Capabilities   : Read & Write Files
      有效期         : 建议留空（永久）
 4) 记下 Key ID 与 Application Key

  ⚠️ 记下 Endpoint，形如：
      https://s3.us-west-004.backblazeb2.com
      （若建 bucket 时选了 Location，用对应的那个）

----------------------------------------------------------------

BANNER

read -rp "  Bucket 名（回车=news-etl-audio）: " BUCKET
BUCKET="${BUCKET:-news-etl-audio}"
echo
read -rp "  Endpoint（回车=s3.us-west-004）: " EP_HOST
EP_HOST="${EP_HOST:-s3.us-west-004}"
echo
read -rp "  Key ID: " AK
echo
read -rsp "  Application Key（不回显）: " SK
echo

if [ -z "$AK" ] || [ -z "$SK" ]; then
  echo "未输入完整，已取消。"
  exit 1
fi

set_key B2_BUCKET "$BUCKET"
set_key B2_ENDPOINT "https://${EP_HOST}"
set_key B2_ACCESS_KEY_ID "$AK"
set_key B2_SECRET_ACCESS_KEY "$SK"
# B2 的公开下载域名（bucket 名 + endpoint）
set_key B2_PUBLIC_BASE "https://f000.backblazeb2.com/file/${BUCKET}"
set_key STORAGE_BACKEND b2
set_key PUBLIC_BASE_URL "https://f000.backblazeb2.com/file/${BUCKET}"
# 容量上限留余量
set_key STORAGE_LIMIT_GB "8"

chmod 600 "$ENV_FILE"

echo
echo "✓ 已保存到 .env（权限 600，已 gitignore）"
echo
echo "  下一步执行："
echo "    .venv/bin/python scripts/backfill_audio.py"
echo
echo "  （把 .env 里的 B2_ENDPOINT 换成你实际拿到的 endpoint）"