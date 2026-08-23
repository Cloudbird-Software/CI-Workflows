#!/usr/bin/env bash
# llm-verify.sh —— LLM-as-a-Verifier CLI 入口（W3-C3 .github#279，ADR-0072）
#
# 封装 llm_verifier.py：endpoint 三探测 + K 次重复评估 + token 交叉核对。
# LLM 调用唯一入口 = pipeline/metering/metering-wrapper.sh（ADR-0062）。
#
# 用法:
#   bash scripts/llm-verify.sh verify --card-id <id> --target-file <file> \
#        [--criteria <file>] [--report-out <report.json>] [--repeat-k <n>]
#   bash scripts/llm-verify.sh probe --model <name>
#   bash scripts/llm-verify.sh cross-check --invoke-id <id> --ledger-dir <dir>
#
# env:
#   LLM_API_KEY      必填（org secret，直连 provider key，ADR-0048）
#   LLM_BASE_URL     可选——默认 https://open.bigmodel.cn/api/paas/v4
# 出:
#   verify: 报告 JSON → --report-out（缺省 stdout）
# 退出码: 0=survived | 1=insufficient | 2=配置/探测失败 | 3=token 偏差作废 | 4=provider 失败
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ADVERSARY_DIR="$(cd "$DIR/../pipeline/adversary" && pwd)"

PY="${METERING_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
fi

CMD="${1:-}"
shift || true

ARGS=("$PY" "$ADVERSARY_DIR/llm_verifier.py" "$CMD")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --card-id|--criteria|--target-file|--target-text|--role|--repeat-k|--base-url|--api-key|--token-deviation-pct|--ledger-dir|--report-out|--model|--invoke-id|--usage-json)
      ARGS+=("$1" "${2:?}"); shift 2 ;;
    *) echo "未知参数 $1（用法见文件头）" >&2; exit 2 ;;
  esac
done

"${ARGS[@]}"
