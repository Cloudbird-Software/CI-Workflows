#!/usr/bin/env bash
# fixture repro 入口：$1 = 状态目录（base/fix 之一）的隔离拷贝（workdir）。
# python3 探测：Windows 商店存根 rc=9009 不可用 → 回退 python（CI 两者皆可）。
set -euo pipefail
if python3 -c "pass" 2>/dev/null; then PY=python3; else PY=python; fi
exec "$PY" "$1/case_bug.py"
