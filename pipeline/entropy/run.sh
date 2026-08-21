#!/usr/bin/env bash
# run.sh —— 语义熵分歧度量 e2e 入口（W4-C1 .github#220，ADR-0066；spec-author/
# metering-wrapper 同款薄壳：参数与退出码在 bash，逻辑在 run.py）。
#
# 红队工具：workflow_dispatch 按需触发（.github/workflows/spec-entropy.yml），
# 非 PR 门。LLM 调用唯走 pipeline/metering/metering-wrapper.sh（INV-06/BEH-09，
# 账本 GATE_METERING_DIR，默认 ./.metering）。
#
# 用法:
#   bash pipeline/entropy/run.sh --spec <spec.md> --out-dir <dir> \
#        [--replay-dir <dir>] [--entailment-engine heuristic|deberta-mnli]
# 模式:
#   --replay-dir 给定 → 回放模式（零凭据零网络：CI/本地自测）
#   未给         → live 模式（须 LLM_API_KEY + 5 族路由齐备，见 policy.py）
# 出: <out-dir>/report.json（schema entropy-report/v1）+ 中间产物
# 退出码: 0=度量完成（含"不归因"结论——归因与否是结论不是错误） | 2=环境/参数错误
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
# python 解释器解析（同 metering-wrapper.sh：CI python3 直用；Windows MSYS 商店
# stub 探测失败回落 python；METERING_PYTHON 可强制）
PY="${METERING_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
fi
exec "$PY" "$DIR/run.py" "$@"
