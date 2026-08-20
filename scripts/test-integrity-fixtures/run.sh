#!/usr/bin/env bash
# test-integrity-fixtures/run.sh —— T8 单元级自检（ADR-0035 / .github #86 卡内指定）
#
# 卡内测试方法 T8：对检测脚本喂构造的 diff fixture（本地 `git diff` 输出形态），
# 断言断言计数、skip 计数、文件删除判定三项输出与预标注（expected.txt）完全一致。
# fixture 覆盖：四类篡改（删除/断言净降/抑制标记/期望值改写）+ 正常类
# （新增测试演进/纯重构/rename/仅文档/文件间迁移）+ 边界（fail-closed、幽灵 ADR、
# 逃生门豁免入账、移除 skip 不触发 R3）。
#
# 用法：bash scripts/test-integrity-fixtures/run.sh   （cwd 任意）
set -euo pipefail
cd "$(dirname "$0")"
SCRIPT="$(cd .. && pwd)/test-integrity.sh"

PASS=0; FAIL=0; FAILED=""
for d in cases/*/; do
  name="${d%/}"; name="${name#cases/}"
  exp_exit="$(sed -n 's/^exit=//p' "$d/expected.txt" | head -1)"
  exp_rules="$(sed -n 's/^rules=//p' "$d/expected.txt" | head -1)"

  rc=0
  out="$(
    if [ -f "$d/env.sh" ]; then set -a; . "$d/env.sh"; set +a; fi
    if [ -f "$d/diff.txt" ]; then export DIFF_FILE="$d/diff.txt"; fi
    bash "$SCRIPT"
  )" || rc=$?

  got_rules="$(printf '%s\n' "$out" | { grep -oE '\[TI-[A-Z0-9-]+\]' || true; } | tr -d '[]' | sort -u | paste -sd, -)"

  ok=1
  [ "$rc" = "$exp_exit" ] || ok=0
  [ "$got_rules" = "$exp_rules" ] || ok=0
  while IFS= read -r pat; do
    [ -n "$pat" ] || continue
    printf '%s\n' "$out" | grep -qE "$pat" || ok=0
  done < <(sed -n 's/^contains=//p' "$d/expected.txt")

  if [ "$ok" = 1 ]; then
    PASS=$((PASS + 1)); echo "PASS $name (exit=$rc rules=$got_rules)"
  else
    FAIL=$((FAIL + 1)); FAILED="$FAILED $name"
    echo "FAIL $name: want exit=$exp_exit rules=$exp_rules; got exit=$rc rules=$got_rules"
    printf '%s\n' "$out" | sed 's/^/    | /' | head -24
  fi
done

echo "T8 fixtures: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ] || { echo "FAILED:${FAILED}"; exit 1; }
