#!/usr/bin/env bash
# 修复后 spec 的 run-suite.sh（W5-C2 E2E fixture）。
# 执行真实 AC 语义测试；测试真实验证 criteria 溯源 / blastRadius / 反摆拍。
set -euo pipefail
cd "$(dirname "$0")"
python3 -m pytest suite/ -v --tb=short
echo "[e2e-fixture] fixed-spec run-suite exit 0 (real AC assertions — should survive)"
