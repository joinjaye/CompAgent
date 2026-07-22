#!/usr/bin/env bash
set -euo pipefail

# 完整每日链路：当天采集/分析/看板 -> 飞书三张业务表 -> 文本与 Overview 合并卡片到 EN 群。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-.venv/bin/python}"
DB_PATH="${DB_PATH:-data/competitor_intel.db}"
DASHBOARD_OUT="${DASHBOARD_OUT:-docs/data/dashboard.json}"
BATCH_DATE="${BATCH_DATE:-$(date -u +%F)}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"
SCREENSHOT_DIR="${SCREENSHOT_DIR:-data/daily_screenshots/${BATCH_DATE}}"

PYTHON="$PYTHON" DB_PATH="$DB_PATH" DASHBOARD_OUT="$DASHBOARD_OUT" BATCH_DATE="$BATCH_DATE" \
  scripts/run_daily_pre_lark.sh

"$PYTHON" -m src.sinks.feishu_business_tables \
  --db-path "$DB_PATH" \
  --date "$BATCH_DATE" \
  --table all

SERVER_LOG="$(mktemp -t compagent-dashboard.XXXXXX.log)"
"$PYTHON" -m http.server "$DASHBOARD_PORT" --directory docs >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

DASHBOARD_URL="http://127.0.0.1:${DASHBOARD_PORT}/index.html"
for _ in 1 2 3 4 5; do
  if "$PYTHON" -c "import urllib.request; urllib.request.urlopen('${DASHBOARD_URL}', timeout=2).read(1)"; then
    break
  fi
  sleep 1
done

"$PYTHON" -m src.sinks.feishu_bot \
  --dashboard-url "$DASHBOARD_URL" \
  --db-path "$DB_PATH" \
  --batch-date "$BATCH_DATE" \
  --screenshot-dir "$SCREENSHOT_DIR" \
  --execute

echo "完成：当天数据已采集分析、同步飞书三表，并将文本与 Overview 合并日报卡片推送到 EN 群。"
