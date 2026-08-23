#!/usr/bin/env bash
# cnb-bridge.sh —— CNB dispatch CLI 入口（W3-C4 .github#280）
#
# 封装 cnb_bridge.py：canary + dispatch + fallback 链 + 凭据扫描。
#
# 用法:
#   bash scripts/cnb-bridge.sh canary [--report-out <f>]
#   bash scripts/cnb-bridge.sh dispatch [--report-out <f>]
#   bash scripts/cnb-bridge.sh fallback [--report-out <f>]
#   bash scripts/cnb-bridge.sh scan-creds
#
# env:
#   CNB_TOKEN          必填（org secret，CNB 沙箱唯一凭据）
#   OWN_API_ENDPOINT   可选（fallback 第一级自有 API）
# 退出码: 0=成功 | 1=canary/探测失败 | 2=配置/凭据违规 | 3=连续 fallback 达阈值 | 4=额度尽
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

ARGS=("$PY" "$ADVERSARY_DIR/cnb_bridge.py" "$CMD")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --report-out) ARGS+=("$1" "${2:?}"); shift 2 ;;
    *) echo "未知参数 $1（用法见文件头）" >&2; exit 2 ;;
  esac
done

"${ARGS[@]}"
