#!/usr/bin/env bash
# run-verify.sh —— 证据引用机械核对 CLI 入口（W3-C5 .github#281，ADR-0067）
#
# 对 redteam/verifier 报告中的 citations 做字符串级机械核对：
#   1) 动态获取当前已检出仓库 HEAD SHA 作为基准版本
#   2) 逐条读取真实工件并匹配 exact_string
#   3) 作废引用强制 verdict=insufficient
#   4) 快照归档工件 + TTL manifest
#
# 用法:
#   bash pipeline/adversary/run-verify.sh --report-in <report.json> \
#       [--repo-dir <dir>] [--snapshot-dir <dir>] [--report-out <verified.json>]
# env:
#   METERING_PYTHON  可强制指定 python 解释器（与 metering 约定一致）
# 出:
#   人类可读核对摘要；核对后报告 JSON → --report-out
# 退出码: 0=全部引用有效 | 1=存在作废引用（verdict insufficient）| 2=infra/配置错误
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

PY="${METERING_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
fi

ARGS=("$PY" "$DIR/verify-evidence.py")
REPORT_OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-in|--repo-dir|--snapshot-dir|--run-id|--ttl-days)
      ARGS+=("$1" "${2:?}"); shift 2 ;;
    --report-out)
      REPORT_OUT="${2:?}"; ARGS+=("$1" "$REPORT_OUT"); shift 2 ;;
    *) echo "未知参数 $1（用法见文件头）" >&2; exit 2 ;;
  esac
done
"${ARGS[@]}"
