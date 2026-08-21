#!/usr/bin/env bash
# bugflow fixture 自测入口（W3-C1 .github#218，ADR-0064）——三值判定/哨兵
# fail-closed/指纹去重/周抽样 全部离线断言，零真实 LLM、零网络、零写库。
# 双消费者：① ci.yml bugflow-selftest job（PR 回归面）② reproduce 流水线的
# 基线自证（bug-reproduce.yml 的 baseline 证据——流水线执法前先证明自己没坏，
# 宪法 §4E"验证器之验证"）。
set -euo pipefail
cd "$(dirname "$0")"
# 本地 Git Bash 的 python3 可能是 Windows 商店存根（rc=9009）——实测可用性后回退 python；
# CI runner 两者皆可，python3 优先。
if python3 -c "pass" 2>/dev/null; then PY=python3; else PY=python; fi
# -W ignore::ResourceWarning：断言里的短命 open() 触发噪音告警（非泄漏——随
# TemporaryDirectory 一并回收），压制之以保 CI 日志可读
"$PY" -W ignore::ResourceWarning -m unittest discover -s . -p 'test_*.py' -v
