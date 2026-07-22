#!/usr/bin/env bash
set -euo pipefail

# 每日生产链路（止于 Lark 同步/推送之前）：
# collect -> group/classify/region/dedup -> ZMX capability catalog -> competitor analysis
# -> Listing/Delisting 分类 -> Overview/Campaign/Product 三份 Summary -> dashboard -> export QA
# 默认总 LLM 预算 5,000,000 token：目录 1,000,000 + 五家竞品各 800,000。

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-.venv/bin/python}"
DB_PATH="${DB_PATH:-data/competitor_intel.db}"
DASHBOARD_OUT="${DASHBOARD_OUT:-docs/data/dashboard.json}"
BATCH_DATE="${BATCH_DATE:-$(date -u +%F)}"
LLM_PROVIDER="${LLM_PROVIDER:-cursor_agent}"
LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-5000000}"

if (( LLM_MAX_TOKENS > 5000000 )); then
  echo "拒绝执行：LLM_MAX_TOKENS=${LLM_MAX_TOKENS} 超过 5,000,000" >&2
  exit 2
fi

BASELINE_BUDGET=$((LLM_MAX_TOKENS / 5))
COMPETITOR_BUDGET=$(((LLM_MAX_TOKENS - BASELINE_BUDGET) / 5))
BASELINE_LOCALE_BUDGET=$((BASELINE_BUDGET / 5))
COMPETITOR_PROCESS_BUDGET=$((COMPETITOR_BUDGET / 3))
COMPETITORS="Bitunix,Weex,BingX,Phemex,Lbank"

mkdir -p data/backups
cp "$DB_PATH" "data/backups/competitor_intel_pre_${BATCH_DATE}.db"

"$PYTHON" -m src.collectors \
  --date "$BATCH_DATE" \
  --db-path "$DB_PATH"

"$PYTHON" -m src.pipeline --db "$DB_PATH" group-check \
  --sources "${COMPETITORS},Zoomex"
"$PYTHON" -m src.pipeline --db "$DB_PATH" classify --apply \
  --sources "${COMPETITORS},Zoomex"
"$PYTHON" -m src.pipeline --db "$DB_PATH" region \
  --sources "$COMPETITORS"
"$PYTHON" -m src.pipeline --db "$DB_PATH" dedup --apply

for locale in EN EN-Asia FR ID VN; do
  "$PYTHON" -m src.analysis.zmx_catalog extract \
    --db "$DB_PATH" \
    --locale "$locale" \
    --provider "$LLM_PROVIDER" \
    --max-calls 2 \
    --max-tokens "$BASELINE_LOCALE_BUDGET"
done

# rollup 不调用 LLM，纯 SQL 聚合，跑一次覆盖 campaign/product 两个类目即可
"$PYTHON" -m src.analysis.zmx_catalog rollup --db "$DB_PATH"

IFS=',' read -r -a SOURCES <<< "$COMPETITORS"
for source in "${SOURCES[@]}"; do
  # Cursor bridge 已验证单进程稳定窗口为 2 次调用；跑三轮可覆盖最多 6 个非派生批次，
  # 后续轮次对已完成批次走缓存，不重复计费。
  for pass in 1 2 3; do
    "$PYTHON" -m src.analysis \
      --db "$DB_PATH" \
      --date "$BATCH_DATE" \
      --source "$source" \
      --category campaign,product \
      --provider "$LLM_PROVIDER" \
      --max-calls 2 \
      --max-tokens "$COMPETITOR_PROCESS_BUDGET"
  done
  # Listing/Delisting 不进入 ZMX 对比，只做一次轻量币种赛道分类；每个
  # source×locale×category 最多一次调用，受控输出 AI/Meme/Layer2/DeFi 等标签。
  "$PYTHON" -m src.analysis \
    --db "$DB_PATH" \
    --date "$BATCH_DATE" \
    --source "$source" \
    --category listing,delisting \
    --provider "$LLM_PROVIDER" \
    --max-calls 10 \
    --max-tokens "$COMPETITOR_PROCESS_BUDGET"
done

# 汇总全部市场的已分析批次，一次生成 Overview、Campaign、Product 三份 2-4 句
# AI Insight。生产流程要求三份均成功，否则不继续导出兜底文案。
"$PYTHON" -m src.analysis.daily_digest \
  --db "$DB_PATH" \
  --date "$BATCH_DATE" \
  --provider "$LLM_PROVIDER" \
  --require-generated

"$PYTHON" -m src.dashboard \
  --db-path "$DB_PATH" \
  --out "$DASHBOARD_OUT"

"$PYTHON" -m src.dashboard.validate \
  --input "$DASHBOARD_OUT" \
  --date "$BATCH_DATE"

echo "完成：每日流程已运行到 Dashboard；未执行 Lark 表格同步或群推送。"
