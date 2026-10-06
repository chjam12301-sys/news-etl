#!/usr/bin/env bash
# 把 Supabase 配置存到本机 .env（已 gitignore，不会进仓库）
# 之后所有命令都自动读取。
#
# 用法：bash scripts/save_supabase_keys.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"
[ -f "$ENV_FILE" ] || { : > "$ENV_FILE"; grep -qx '.env' .gitignore 2>/dev/null || echo ".env" >> .gitignore; }

set_key() {
  local key="$1" val="$2"
  grep -v "^${key}=" "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || true
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  echo "${key}=${val}" >> "$ENV_FILE"
}

cat <<'BANNER'

================================================================
 Supabase —— Storage + Database 一次搞定
================================================================

 1) 打开 https://supabase.com/dashboard → New project
      Name    : news-etl
      DB pass : 随意（我们用 Neon，这个密码用不上）
      Region  : 选 Southeast Asia (Singapore) ← 国内访问更快
      Plan    : Free
      等 2 分钟建好。

 2) 建Storage bucket
      左侧Storage → New bucket
      Name : news-audio
      ☑ Public bucket       ← 必须勾！否则 App 读不到音频

 3) 取密钥
      左侧 Settings → API
      复制两项：
        Project URL    （形如 https://abcdefgh.supabase.co）
        service_role  key  ← 要 service_role，不是 anon key！
                          （service_role 是灰底那行，泄露可写删，
                            但只放GitHub Secrets 不外传）

----------------------------------------------------------------

BANNER

read -rp "  Project URL: " URL
echo
read -rsp "  service_role key（不回显）: " KEY
echo

if [ -z "$URL" ] || [ -z "$KEY" ]; then
  echo "未输入完整，已取消。"
  exit 1
fi

case "$URL" in
  https://*.supabase.co) echo "✓ Project URL 格式正常" ;;
  *) echo "⚠ URL 看起来不像 Supabase 项目地址: $URL"; exit 1 ;;
esac

BUCKET="news-audio"

set_key SUPABASE_PROJECT_URL "${URL%/}"
set_key SUPABASE_SERVICE_KEY "$KEY"
set_key SUPABASE_BUCKET "$BUCKET"
set_key STORAGE_BACKEND "supabase"
set_key PUBLIC_BASE_URL "${URL%/}/storage/v1/object/public/${BUCKET}"
set_key EXPORT_JSON "true"

chmod 600 "$ENV_FILE"

echo
echo "✓ 已保存到 .env（权限 600，已 gitignore）"
echo "  存储上限将自动按 0.9 GB 执行（Supabase 免费额度 1GB）"
echo
echo "  下一步："
echo "    .venv/bin/python scripts/diagnose_storage.py"
echo "    .venv/bin/python scripts/backfill_audio.py"