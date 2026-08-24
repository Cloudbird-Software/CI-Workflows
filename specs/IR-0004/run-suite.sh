#!/usr/bin/env bash
# 套件执行器（adversary 目标目录契约）：bash run-suite.sh <impl-dir>
# impl-dir 须含 spec.md（被审"实现"= 一份 spec 文档）；exit 0 = 套件全绿。
# 审计沙箱布置：以 context/ 中的仓文件快照重建最小仓上下文（governance 等
# 非 planned blastRadius 存在性检查所需），被审 spec.md 置于 specs/IR-0004/。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
IMPL="${1:?用法: run-suite.sh <impl-dir>}"
[[ -f "$IMPL/spec.md" ]] || { echo "impl 目录缺 spec.md: $IMPL" >&2; exit 2; }
PY="${METERING_PYTHON:-}"
if [[ -z "$PY" ]]; then
  PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
fi
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/specs/IR-0004/suite" "$TMP/governance/policy" "$TMP/governance/rulesets" "$TMP/.github/ISSUE_TEMPLATE"
cp "$DIR"/suite/*.py "$TMP/specs/IR-0004/suite/"
cp "$IMPL"/spec.md "$TMP/specs/IR-0004/spec.md"
# 仓上下文快照（与本目录 run-suite.sh 同源的 context/，来自 .github@审计分支）
cp -r "$DIR"/context/governance/. "$TMP/governance/"
cp -r "$DIR"/context/issue_template/. "$TMP/.github/ISSUE_TEMPLATE/"
cd "$TMP/specs/IR-0004/suite"
if "$PY" -c "import pytest" 2>/dev/null; then
  exec "$PY" -m pytest -q test_spec_ir0004.py
else
  exec "$PY" test_spec_ir0004.py
fi
