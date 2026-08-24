#!/usr/bin/env bash
# 强套件执行入口（survived 腿）：bash run-suite.sh <impl-dir>。
# 套件与 adversary 产物拷进一次性目录真实执行；exit 0 = 全绿。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
IMPL="${1:?用法: run-suite.sh <impl-dir>}"
[[ -d "$IMPL" ]] || { echo "impl 目录不存在: $IMPL" >&2; exit 2; }
PY="${METERING_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
fi
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cp "$DIR"/suite/*.py "$TMP"/
cp "$IMPL"/*.py "$TMP"/
cd "$TMP"
exec "$PY" -m unittest -v test_tax
