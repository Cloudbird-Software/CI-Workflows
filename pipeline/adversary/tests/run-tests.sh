#!/usr/bin/env bash
# run-tests.sh —— 恶意合规 adversary 自测（W4-C2 .github#221，ADR-0067 决策 2/3）
#
# 判定物有效性（宪法 §4E）：零真实 LLM 调用（--replay-file 回放模式），断言：
#   T0 语法预检（bash -n ×3 + python ast + 回放 JSON 可解析——不产 __pycache__）
#   T1 配置锁（AC-3）：字段齐全（alias=judge-deep/model/family/prompt hash/采样
#      参数）+ prompt hash 与文件实际一致 + 跨族断言 ok（≠builder≠test-author）
#      + 篡改负控制（prompt 文件漂移 → exit 2 fail-closed）
#   T2 弱套件 e2e（AC-1）：回放的退化实现在弱套件上真全绿 → exit 1（blocking）
#      + 判"套件不充分" + 钻洞归因 S1→constant-assertion + 报告含锁定配置
#   T3 强套件 e2e（AC-2）：五类攻击全试全败（真实执行套件）→ exit 0
#      + 报告 ≥1 条攻击尝试记录（防恒绿）+ blocking=false
#   T4 恒绿防御：空 attempts 白卷 → exit 3（infra），绝不让套件轻松过关
#   T5 计量约定（ADR-0062）：LLM 调用走 metering wrapper——账本恰含
#      role=adversary 记录（model/temperature/seed/exit_status 留痕）
# 用法: bash pipeline/adversary/tests/run-tests.sh（CI job 与本地同路径）
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
FIX="$DIR/fixtures"
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
run_rc() {  # run_rc <名> <期望rc> cmd... → 输出捕获进 LAST_OUT
  local name="$1" want="$2"; shift 2
  local rc=0
  LAST_OUT=$("$@" 2>&1) || rc=$?
  if [[ "$rc" == "$want" ]]; then pass "$name（rc=$rc）"; else fail "$name：rc=$rc 期望=$want（输出：${LAST_OUT:0:400}）"; fi
}
jcheck() {  # jcheck <名> <json文件> <expr>（d=已加载对象）
  local name="$1" file="$2" expr="$3"
  if "$PY" -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); sys.exit(0 if ($expr) else 1)" \
      "$file" 2>"$TMP/j.err"; then
    pass "$name"
  else
    fail "$name（expr 不成立：$expr；err: $(tail -c 200 "$TMP/j.err")）"
  fi
}

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# ---- T0 语法/可解析预检 ----
for s in run-adversary.sh fixtures/weak-suite/run-suite.sh fixtures/strong-suite/run-suite.sh; do
  bash -n "$DIR/$s" && pass "bash -n $s" || fail "bash -n $s"
done
"$PY" -c 'import ast,sys;ast.parse(open(sys.argv[1],encoding="utf-8").read())' "$DIR/adversary.py" \
  && pass "python 语法 adversary.py" || fail "python 语法 adversary.py"
"$PY" -c 'import json,sys;[json.load(open(f,encoding="utf-8")) for f in sys.argv[1:]]' \
  "$FIX/weak-suite/replay-response.json" "$FIX/strong-suite/replay-response.json" \
  "$DIR/tests/fixtures/empty-attempts.json" \
  && pass "回放 JSON 三件可解析" || fail "回放 JSON 不可解析"

# ---- T1 配置锁（AC-3） ----
"$PY" "$DIR/adversary.py" config >"$TMP/lock.json" 2>"$TMP/lock.err"
RC=$?
if [[ $RC -eq 0 ]]; then pass "T1 config 输出 rc=0"; else fail "T1 config rc=$RC（$(cat "$TMP/lock.err")）"; fi
[[ -s "$TMP/lock.json" ]] && jcheck "T1 alias=judge-deep（锁定的档）" "$TMP/lock.json" \
  "d['alias']=='judge-deep'"
jcheck "T1 model/family 与 registry judge-deep 档一致（sovereign-family）" "$TMP/lock.json" \
  "d['model']=='glm-4.6' and d['family']=='sovereign-family'"
jcheck "T1 采样参数锁定齐全（max_tokens/temperature/top_p/seed/thinking）" "$TMP/lock.json" \
  "set(d['sampling'])>={'max_tokens','temperature','top_p','seed','thinking'} and d['sampling']['temperature']==0.2 and d['sampling']['seed']==67"
if "$PY" - "$TMP/lock.json" "$DIR/prompt-v1.md" <<'EOF'
import hashlib, json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
actual = "sha256:" + hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest()
sys.exit(0 if lock["prompt_version"] == actual else 1)
EOF
then pass "T1 prompt_version 与 prompt-v1.md 实际 sha256 一致"; else fail "T1 prompt_version 与文件不一致（锁漂移）"; fi
jcheck "T1 跨族断言 ok（AR-8：≠flash-family≠flagship-family）" "$TMP/lock.json" \
  "d['cross_family']['ok'] and d['cross_family']['adversary'] not in (d['cross_family']['builder'],d['cross_family']['test_author'])"
# 篡改负控制：prompt 文件漂移 → 配置锁必须红（fail-closed，考试档案不容漂移）
TDIR="$TMP/tamper/pipeline"; mkdir -p "$TDIR"
cp -r "$DIR" "$TDIR/adversary" && cp "$DIR/../models.yaml" "$TDIR/"
printf '\n' >>"$TDIR/adversary/prompt-v1.md"
run_rc "T1 篡改 prompt-v1.md 副本 → 配置锁红（exit 2）" 2 \
  "$PY" "$TDIR/adversary/adversary.py" config

# ---- T2 弱套件 e2e（AC-1：退化实现全绿 → 套件不充分 blocking） ----
M2="$TMP/meter-t2"; mkdir -p "$M2"
run_rc "T2 弱套件：adversary 得手 → exit 1（blocking）" 1 \
  env GATE_METERING_DIR="$M2" bash "$DIR/run-adversary.sh" \
    --target "$FIX/weak-suite" --replay-file "$FIX/weak-suite/replay-response.json" \
    --report-out "$TMP/weak-report.json"
jcheck "T2 verdict=insufficient 且 blocking" "$TMP/weak-report.json" \
  "d['verdict']=='insufficient' and d['blocking'] is True"
jcheck "T2 钻洞归因：S1 → constant-assertion（策略 ID→洞映射）" "$TMP/weak-report.json" \
  "d['exploited']==['S1'] and d['holes']==[{'strategy':'S1','hole':'常量断言洞——套件期望值可枚举即被背诵','suite_gap':'constant-assertion'}]"
jcheck "T2 尝试记录 ≥1 且套件真实执行过（suite_tail 有证据）" "$TMP/weak-report.json" \
  "d['attempt_count']>=1 and d['attempts'][0]['green'] is True and 'OK' in d['attempts'][0]['suite_tail']"
jcheck "T2 报告含锁定配置（AC-3 留痕：模型/prompt hash/族标记）" "$TMP/weak-report.json" \
  "d['config']['alias']=='judge-deep' and d['config']['prompt_version'].startswith('sha256:') and d['config']['cross_family']['ok'] is True"

# ---- T3 强套件 e2e（AC-2：五类攻击全败 → 套件通过考验） ----
M3="$TMP/meter-t3"; mkdir -p "$M3"
run_rc "T3 强套件：攻击全败 → exit 0" 0 \
  env GATE_METERING_DIR="$M3" bash "$DIR/run-adversary.sh" \
    --target "$FIX/strong-suite" --replay-file "$FIX/strong-suite/replay-response.json" \
    --report-out "$TMP/strong-report.json"
jcheck "T3 verdict=survived 且非 blocking" "$TMP/strong-report.json" \
  "d['verdict']=='survived' and d['blocking'] is False"
jcheck "T3 报告含 ≥1 条攻击尝试且全败（防恒绿留痕）" "$TMP/strong-report.json" \
  "d['attempt_count']>=1 and all(not a['green'] for a in d['attempts'])"
jcheck "T3 五类策略各有真实尝试（S1-S5 全试、套件 rc 均红）" "$TMP/strong-report.json" \
  "{a['strategy'] for a in d['attempts']}=={'S1','S2','S3','S4','S5'} and all(a['suite_rc']!=0 for a in d['attempts'])"
jcheck "T3 exploited/holes 为空（无得手）" "$TMP/strong-report.json" \
  "d['exploited']==[] and d['holes']==[]"

# ---- T4 恒绿防御（白卷 = infra exit 3） ----
M4="$TMP/meter-t4"; mkdir -p "$M4"
run_rc "T4 空 attempts 白卷 → exit 3（恒绿防御）" 3 \
  env GATE_METERING_DIR="$M4" bash "$DIR/run-adversary.sh" \
    --target "$FIX/weak-suite" --replay-file "$DIR/tests/fixtures/empty-attempts.json" \
    --report-out "$TMP/empty-report.json"
jcheck "T4 报告 verdict=no-attempts（白卷留痕可诊断）" "$TMP/empty-report.json" \
  "d['verdict']=='no-attempts' and d['attempt_count']==0"

# ---- T5 计量约定（ADR-0062：调用经 metering wrapper，账本留痕） ----
LEDGERS=$(ls "$M2"/records-*.jsonl 2>/dev/null)
[[ -n "$LEDGERS" ]] && pass "T5 账本周片落盘" || { fail "T5 账本未落盘"; LEDGERS="$M2/nope.jsonl"; }
"$PY" -c "
import json, sys
recs = []
for line in open(sys.argv[1], encoding='utf-8'):
    line = line.strip()
    if line:
        recs.append(json.loads(line))
adv = [r for r in recs if r.get('role') == 'adversary']
assert adv, '无 role=adversary 记录'
r = adv[-1]
assert r['model'] == 'glm-4.6', r['model']
assert r['exit_status'] == 'ok', r['exit_status']
assert r['sampling']['temperature'] == 0.2 and r['seed'] == 67, (r['sampling'], r['seed'])
assert r['prompt_version'].startswith('sha256:'), r['prompt_version']
sys.exit(0)
" "$LEDGERS" && pass "T5 role=adversary 记录：model/temperature/seed/prompt_version 留痕" \
  || fail "T5 计量记录断言不成立（见上）"

echo "-----"
if [[ $FAILS -eq 0 ]]; then
  echo "adversary 自测全部通过（T0-T5）"
  exit 0
fi
echo "adversary 自测失败 $FAILS 项"
exit 1
