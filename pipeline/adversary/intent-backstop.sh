#!/usr/bin/env bash
# intent-backstop.sh —— 意图层探索道闸 S6-S8 CLI 入口（ISSUE-263 AC-16 / ADR-0067 / ADR-0079）
#
# 对每张卡的 spec.md 做确定性探索，只报人不阻断。产物 schemas：
#   intent-backstop.<card>.hit.json      schema=intent-backstop/hit/v1
#   intent-backstop.<card>.no-hit.json   schema=intent-backstop/no-hit/v1
#   intent-backstop.<card>.skipped.json  schema=intent-backstop/skipped/v1
#
# 用法:
#   bash pipeline/adversary/intent-backstop.sh --spec <spec.md> --repo-root <dir> [--out-dir <dir>] [--card-id <id>]
# env:
#   METERING_PYTHON  可强制指定 python 解释器（同 metering 约定）
# 退出码: 0=完成 | 2=参数/解析失败或 S8 判定作废（void）
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

exec "$PY" "$DIR/intent-backstop.py" "$@"
