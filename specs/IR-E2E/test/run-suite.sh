#!/usr/bin/env bash
# 故意极差 spec 的占位 run-suite.sh（W5-C2 E2E fixture）。
# stdlib unittest（无 pytest 依赖）：恒绿（套件不充分 = 测试从不真正验证 AC）。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
IMPL="${1:-}"
PY="${METERING_PYTHON:-python3}"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cp "$DIR"/suite/*.py "$TMP"/
if [[ -n "$IMPL" && -d "$IMPL" ]]; then cp "$IMPL"/*.py "$TMP"/ 2>/dev/null || true; fi
cd "$TMP"
exec "$PY" -m unittest -v test_placeholder
