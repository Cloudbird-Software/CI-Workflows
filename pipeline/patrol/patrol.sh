#!/usr/bin/env bash
# patrol.sh —— patrol 巡逻服务 bash 入口（W3-C2 .github#219，ADR-0065）
#
# 职责：拉取政策（.github 仓 governance/policy/patrol.yaml——阈值唯一真源，
# C1 版本化）→ 委托 patrol.py run。fail-closed：政策拉不下来/解析不过 =
# 拒绝巡逻（宁可被 dead-man 记缺席，不可无政策盲跑——宪法 §6 缺席纪律）。
#
# 用法（参数缺省全走政策/环境，workflow 内固定形态调用）：
#   bash pipeline/patrol/patrol.sh --repo <owner/repo> --run-id <id> --seed <n> \
#        --state <dir> --out <dir> [--target-base <dir>] [--llm-replay <f>] [--clock <iso>]
# env:
#   PATROL_POLICY_URL  政策 raw 地址（默认 .github 仓 main 的 governance/policy/patrol.yaml）
#   PATROL_POLICY      本地政策文件路径（离线/自测——设置后跳过拉取）
#   LLM_API_KEY        可选——缺失时 LLM 前沿探索半源诚实降级（skipped 计数）
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

# python 解释器解析：CI（ubuntu）python3 直用；本地 Windows（MSYS）python3 是
# 商店 stub → 探测失败回落 python（metering-wrapper.sh 同款约定）
PY="${METERING_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
fi

TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT
if [[ -n "${PATROL_POLICY:-}" ]]; then
  POLICY_FILE="$PATROL_POLICY"   # 离线形态（自测/演习）：本地政策直用
else
  URL="${PATROL_POLICY_URL:-https://raw.githubusercontent.com/Cloudbird-Software/.github/main/governance/policy/patrol.yaml}"
  # -f：HTTP 错误码即失败（不落 404 HTML 假政策）；--max-time 防挂死烧 runner 分钟
  if ! curl -fsSL --max-time 30 "$URL" -o "$TMPD/patrol.yaml"; then
    echo "FATAL: 政策拉取失败（$URL）——fail-closed 拒绝巡逻（ADR-0065 决策 3：阈值集中政策文件）" >&2
    exit 2
  fi
  POLICY_FILE="$TMPD/patrol.yaml"
fi

exec "$PY" "$DIR/patrol.py" run --policy "$POLICY_FILE" "$@"
