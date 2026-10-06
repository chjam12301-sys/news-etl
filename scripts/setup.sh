#!/usr/bin/env bash
# 一键准备本地环境
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
echo "==> 创建虚拟环境 (.venv)"
[ -d .venv ] || "$PY" -m venv .venv

echo "==> 安装依赖"
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
./.venv/bin/pip install -q pytest pytest-asyncio

[ -f .env ] || { cp .env.example .env; echo "==> 已生成 .env（GEMINI_API_KEY 留空则走离线降级模式）"; }

echo "==> 自检"
./.venv/bin/python -c "from app.api import app; print(f'    OK · {len(app.routes)} 条路由')"

cat <<'EOF'

准备完成。下一步：
  跑一次流水线   .venv/bin/python -m app.cli daily --per-topic 1
  启动 API      .venv/bin/python -m app.cli serve
  打开文档      http://127.0.0.1:8000/docs
  跑测试        .venv/bin/python -m pytest tests/ -q

上线步骤见 docs/DEPLOY.md
EOF