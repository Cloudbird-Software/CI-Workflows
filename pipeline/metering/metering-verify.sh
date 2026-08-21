#!/usr/bin/env bash
# metering-verify.sh —— 计量账本验链工具（W2-C3 .github#216，ADR-0062 决策 4）
#
# 从任一片/整目录验证产物 hash 链：逐条重算 record_sha256、核对 prev 链与
# record_index、过 record.schema.json 断言、invoke_id 全账本去重。任一断点
# 即报 CHAIN 行并 exit 3（fail-closed——链条可信度破坏必须显式红）。
#
# 用法:
#   bash pipeline/metering/metering-verify.sh --dir <GATE_METERING_DIR>      # 验目录下全部周片
#   bash pipeline/metering/metering-verify.sh --file <records-XXXX-Www.jsonl> # 验单片（断点定位）
# 退出码: 0=链完整 | 2=参数/环境错误 | 3=验链失败
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
exec "$PY" "$DIR/metering.py" verify "$@"
