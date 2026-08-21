#!/usr/bin/env bash
# 翻转制造器：奇数次运行 fail、偶数次 pass。计数器 .flap-state 放本场景目录
# （自测 setup/清理负责归零；仓库树不提交该文件）——每次 reproduce 的两次采样
# 恰好一红一绿 → 翻转异常 → inconclusive（ADR-0064 决策 4）。
set -euo pipefail
STATE="$(dirname "$0")/.flap-state"
n=0; [ -f "$STATE" ] && n=$(cat "$STATE")
n=$((n+1)); echo "$n" > "$STATE"
echo "flap run #$n"
if [ $((n % 2)) -eq 1 ]; then echo "REPRO_OUTCOME: fail"; exit 1
else echo "REPRO_OUTCOME: pass"; exit 0; fi
