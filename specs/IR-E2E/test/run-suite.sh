#!/usr/bin/env bash
# 故意极差 spec 的占位 run-suite.sh（W5-C2 E2E fixture）。
# 执行占位测试；恒退出 0（套件不充分 = 测试从不真正验证 AC）。
set -euo pipefail
cd "$(dirname "$0")"
python3 -m pytest suite/ -q --tb=short
echo "[e2e-fixture] run-suite exit 0 (placeholder suite — judge-deep 将判 insufficient)"
