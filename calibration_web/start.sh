#!/usr/bin/env bash
set -euo pipefail

WEB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_HOST="${EGO_WEB_HOST:-0.0.0.0}"
WEB_PORT="${EGO_WEB_PORT:-8020}"

PATH="$WEB_ROOT/.node/bin:$PATH" npm --prefix "$WEB_ROOT/frontend" run build

echo "Kalibr 标定网页: http://127.0.0.1:${WEB_PORT}"
echo "数据目录: ${EGO_WEB_DATA_ROOT:-/home/conanluo/kalibr_data}"
exec "$WEB_ROOT/backend/.venv/bin/uvicorn" \
  ego_web.main:app \
  --app-dir "$WEB_ROOT/backend" \
  --host "$WEB_HOST" \
  --port "$WEB_PORT"
