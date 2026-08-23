#!/usr/bin/env bash
# test-intent-backstop.sh —— 意图道闸 S6-S8 离线自测（ISSUE-263 AC-16 / ADR-0067 / ADR-0079）
#
# 断言：
#   I0 语法预检（bash -n + python ast + dogfood JSON 可解析）
#   I1 重复能力卡（ISSUE-DUP）：S6 命中并带 file:line 证据，产出 hit 工件
#   I2 干净卡（ISSUE-CLEAN）：S6-S8 均无命中，产出 no-hit 工件，无 hit 工件
#   I3 #263 dogfood 三条命中 fixture 符合 intent-backstop/hit/v1 schema
# 用法: bash pipeline/adversary/tests/test-intent-backstop.sh
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
FIX="$DIR/fixtures/intent-backstop"
PY="${METERING_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
fi

FAILS=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; FAILS=$((FAILS+1)); }
jcheck() {
  local name="$1" file="$2" expr="$3"
  if "$PY" -c "import json,sys,os; d=json.load(open(sys.argv[1],encoding='utf-8')); sys.exit(0 if ($expr) else 1)" \
      "$file" 2>"$TMP/j.err"; then
    pass "$name"
  else
    fail "$name（expr 不成立：$expr；err: $(tail -c 200 "$TMP/j.err")）"
  fi
}

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# ---- I0 语法/可解析预检 ----
bash -n "$DIR/intent-backstop.sh" && pass "bash -n intent-backstop.sh" || fail "bash -n intent-backstop.sh"
"$PY" -c 'import ast,sys;ast.parse(open(sys.argv[1],encoding="utf-8").read())' "$DIR/intent-backstop.py" \
  && pass "python 语法 intent-backstop.py" || fail "python 语法 intent-backstop.py"
"$PY" -c 'import json,sys;json.load(open(sys.argv[1],encoding="utf-8"))' "$FIX/issue-263-dogfood-hits.json" \
  && pass "dogfood fixture JSON 可解析" || fail "dogfood fixture JSON 不可解析"

# ---- I1 重复能力卡：S6 命中并带 file:line ----
IBOUT="$TMP/intent-backstop"
RC=0
bash "$DIR/intent-backstop.sh" --spec "$FIX/duplicate-spec.md" \
  --repo-root "$(cd "$DIR/../.." && pwd)" --out-dir "$IBOUT" --card-id ISSUE-DUP >/dev/null 2>&1 || RC=$?
if [[ $RC -eq 0 ]]; then pass "I1 重复能力卡 rc=0"; else fail "I1 重复能力卡 rc=$RC"; fi
DUP_HIT="$IBOUT/intent-backstop.ISSUE-DUP.hit.json"
DUP_NOHIT="$IBOUT/intent-backstop.ISSUE-DUP.no-hit.json"
[[ -f "$DUP_HIT" ]] && pass "I1 hit 工件落盘" || fail "I1 未产出 hit 工件"
[[ ! -f "$DUP_NOHIT" ]] && pass "I1 命中时无 no-hit 工件" || fail "I1 命中时不应产出 no-hit 工件"
jcheck "I1 schema=intent-backstop/hit/v1 且含 S6" "$DUP_HIT" \
  "d['schema']=='intent-backstop/hit/v1' and any(h['strategy']=='S6' for h in d['hits'])"
jcheck "I1 S6 证据含 file:line 且指向 spec 中的 flaky-retry" "$DUP_HIT" \
  "any(h['strategy']=='S6' and any('flaky-retry' in e['snippet'] and e['file'].endswith('duplicate-spec.md') and e['line']>0 for e in h['evidence']) for h in d['hits'])"

# ---- I2 干净卡：产出 no-hit 记录 ----
RC=0
bash "$DIR/intent-backstop.sh" --spec "$FIX/clean-spec.md" \
  --repo-root "$(cd "$DIR/../.." && pwd)" --out-dir "$IBOUT" --card-id ISSUE-CLEAN >/dev/null 2>&1 || RC=$?
if [[ $RC -eq 0 ]]; then pass "I2 干净卡 rc=0"; else fail "I2 干净卡 rc=$RC"; fi
CLN_HIT="$IBOUT/intent-backstop.ISSUE-CLEAN.hit.json"
CLN_NOHIT="$IBOUT/intent-backstop.ISSUE-CLEAN.no-hit.json"
[[ -f "$CLN_NOHIT" ]] && pass "I2 no-hit 工件落盘" || fail "I2 未产出 no-hit 工件"
[[ ! -f "$CLN_HIT" ]] && pass "I2 无 hit 工件" || fail "I2 不应产出 hit 工件"
jcheck "I2 schema=intent-backstop/no-hit/v1 且 S6-S8 均跑过" "$CLN_NOHIT" \
  "d['schema']=='intent-backstop/no-hit/v1' and set(d['strategies_run'])=={'S6','S7','S8'}"

# ---- I3 #263 dogfood fixture schema校验 ----
jcheck "I3 dogfood fixture为 hit schema 且含 3 条" "$FIX/issue-263-dogfood-hits.json" \
  "d['schema']=='intent-backstop/hit/v1' and d['hit_count']==3 and set(h['strategy'] for h in d['hits'])=={'S6','S7','S8'}"
for strat in S6 S7 S8; do
  jcheck "I3 dogfood $strat 证据含 file:line" "$FIX/issue-263-dogfood-hits.json" \
    "any(h['strategy']=='$strat' and all('file' in e and 'line' in e for e in h['evidence']) for h in d['hits'])"
done

echo "-----"
if [[ $FAILS -eq 0 ]]; then
  echo "intent-backstop 自测全部通过（I0-I3）"
  exit 0
fi
echo "intent-backstop 自测失败 $FAILS 项"
exit 1
