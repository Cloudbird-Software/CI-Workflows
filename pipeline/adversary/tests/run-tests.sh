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
#   T6 攻击面清单（#278）：S6-S8 必须 requires_explore=true；S1'-S5' 与 S1-S5 不得标
#   T7 证据引用机械核对（AC-9 / AC-2 / INV-03）
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
jcheck() {  # jcheck <名> <json文件> <expr>（d=已加载对象；os 已导入）
  local name="$1" file="$2" expr="$3"
  if "$PY" -c "import json,sys,os; d=json.load(open(sys.argv[1],encoding='utf-8')); sys.exit(0 if ($expr) else 1)" \
      "$file" 2>"$TMP/j.err"; then
    pass "$name"
  else
    fail "$name（expr 不成立：$expr；err: $(tail -c 200 "$TMP/j.err")）"
  fi
}

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# ---- T0 语法/可解析预检 ----
for s in run-adversary.sh run-verify.sh fixtures/weak-suite/run-suite.sh fixtures/strong-suite/run-suite.sh; do
  bash -n "$DIR/$s" && pass "bash -n $s" || fail "bash -n $s"
done
for p in adversary.py verify-evidence.py; do
  "$PY" -c 'import ast,sys;ast.parse(open(sys.argv[1],encoding="utf-8").read())' "$DIR/$p" \
    && pass "python 语法 $p" || fail "python 语法 $p"
done
"$PY" -c 'import json,sys;[json.load(open(f,encoding="utf-8")) for f in sys.argv[1:]]' \
  "$FIX/weak-suite/replay-response.json" "$FIX/strong-suite/replay-response.json" \
  "$DIR/tests/fixtures/empty-attempts.json" \
  "$FIX/verify-evidence/report-valid.json" "$FIX/verify-evidence/report-fabricated.json" \
  && pass "回放/verify JSON 可解析" || fail "回放/verify JSON 不可解析"

# ---- T1 配置锁（AC-3） ----
"$PY" "$DIR/adversary.py" config >"$TMP/lock.json" 2>"$TMP/lock.err"
RC=$?
if [[ $RC -eq 0 ]]; then pass "T1 config 输出 rc=0"; else fail "T1 config rc=$RC（$(cat "$TMP/lock.err")）"; fi
[[ -s "$TMP/lock.json" ]] && jcheck "T1 alias=judge-deep（锁定的档）" "$TMP/lock.json" \
  "d['alias']=='judge-deep'"
jcheck "T1 model/family 与 registry judge-deep 档一致（sovereign-family）" "$TMP/lock.json" \
  "d['model']=='kimi-for-coding' and d['family']=='sovereign-family'"
jcheck "T1 采样参数锁定齐全（max_tokens/temperature/top_p/seed/thinking）" "$TMP/lock.json" \
  "set(d['sampling'])>={'max_tokens','temperature','top_p','seed','thinking'} and d['sampling']['temperature']==0.2 and d['sampling']['seed']==67"
PROMPT_FILE=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1],encoding='utf-8'))['prompt_file'])" "$TMP/lock.json")
if "$PY" - "$TMP/lock.json" "$DIR/$PROMPT_FILE" <<'EOF'
import hashlib, json, sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
actual = "sha256:" + hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest()
sys.exit(0 if lock["prompt_version"] == actual else 1)
EOF
then pass "T1 prompt_version 与 $PROMPT_FILE 实际 sha256 一致"; else fail "T1 prompt_version 与文件不一致（锁漂移）"; fi
jcheck "T1 跨族断言 ok（AR-8：≠flash-family≠flagship-family）" "$TMP/lock.json" \
  "d['cross_family']['ok'] and d['cross_family']['adversary'] not in (d['cross_family']['builder'],d['cross_family']['test_author'])"
# 篡改负控制：prompt 文件漂移 → 配置锁必须红（fail-closed，考试档案不容漂移）
TDIR="$TMP/tamper/pipeline"; mkdir -p "$TDIR"
cp -r "$DIR" "$TDIR/adversary" && cp "$DIR/../models.yaml" "$TDIR/"
printf '\n' >>"$TDIR/adversary/$PROMPT_FILE"
run_rc "T1 篡改 $PROMPT_FILE 副本 → 配置锁红（exit 2）" 2 \
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
assert r['model'] == 'kimi-for-coding', r['model']
assert r['exit_status'] == 'ok', r['exit_status']
assert r['sampling']['temperature'] == 0.2 and r['seed'] == 67, (r['sampling'], r['seed'])
assert r['prompt_version'].startswith('sha256:'), r['prompt_version']
sys.exit(0)
" "$LEDGERS" && pass "T5 role=adversary 记录：model/temperature/seed/prompt_version 留痕" \
  || fail "T5 计量记录断言不成立（见上）"

# ---- T6 攻击面清单 requires_explore 语义（#278 AC-16） ----
"$PY" - "$DIR/attack-strategies.yaml" <<'EOF'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
st = {s['id']: s for s in doc.get('strategies', [])}
for sid in ('S6', 'S7', 'S8'):
    assert sid in st, f"缺 {sid}"
    assert st[sid].get('requires_explore') is True, f"{sid} requires_explore 应为 true"
for sid in ("S1'", "S2'", "S3'", "S4'", "S5'", 'S1', 'S2', 'S3', 'S4', 'S5'):
    assert sid in st, f"缺 {sid}"
    assert not st[sid].get('requires_explore'), f"{sid} 不得设置 requires_explore"
print("T6 攻击面清单 requires_explore 语义正确")
EOF
if [[ $? -eq 0 ]]; then pass "T6 S6-S8 requires_explore=true 且 S1'-S5'/S1-S5 不标"; else fail "T6 requires_explore 语义错误"; fi

# ---- T7 证据引用机械核对（AC-9 / AC-2 / INV-03） ----
VFIX="$FIX/verify-evidence"
VS="$TMP/snapshots"
run_rc "T7 全有效引用 → exit 0，verdict 保持 survived" 0 \
  "$PY" "$DIR/verify-evidence.py" --report-in "$VFIX/report-valid.json" \
    --repo-dir "$VFIX/repo" --snapshot-dir "$VS/valid" \
    --report-out "$TMP/verify-valid.json" --run-id "test-valid"
jcheck "T7 valid 报告 verdict=survived 且非 blocking" "$TMP/verify-valid.json" \
  "d['verdict']=='survived' and d['blocking'] is False and d['void_count']==0"
jcheck "T7 valid 引用全部命中且含 baseline SHA/时间" "$TMP/verify-valid.json" \
  "all(c['_status']=='valid' and c['_matched'] is True for c in d['citations']) and len(d['baseline']['sha'])==40 and bool(d['baseline']['fetched_at'])"
jcheck "T7 valid 快照目录含 manifest + 引用文件" "$TMP/verify-valid.json" \
  "os.path.isfile(os.path.join(d['snapshot_dir'],'manifest.json')) and os.path.isfile(os.path.join(d['snapshot_dir'],'src/tax.py'))"
"$PY" - "$VS/valid/manifest.json" <<'EOF'
import json, sys, datetime as dt
m = json.load(open(sys.argv[1], encoding="utf-8"))
assert m["schema"] == "evidence-snapshot-manifest/v1"
assert m["ttl_days"] == 90
exp = dt.datetime.fromisoformat(m["expires_at"].replace("Z", "+00:00"))
cre = dt.datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
assert (exp - cre).days == 90
assert m["baseline_sha"] and len(m["baseline_sha"]) == 40
assert "src/tax.py" in m["citation_files"]
sys.exit(0)
EOF
[[ $? -eq 0 ]] && pass "T7 valid TTL manifest 字段齐全" || fail "T7 valid TTL manifest 字段不全"

run_rc "T7 捏造+隐藏引用 → exit 1，verdict 强制 insufficient" 1 \
  "$PY" "$DIR/verify-evidence.py" --report-in "$VFIX/report-fabricated.json" \
    --repo-dir "$VFIX/repo" --snapshot-dir "$VS/fabricated" \
    --report-out "$TMP/verify-fabricated.json" --run-id "test-fabricated"
jcheck "T7 fabricated 报告 verdict=insufficient 且 blocking" "$TMP/verify-fabricated.json" \
  "d['verdict']=='insufficient' and d['blocking'] is True and d['original_verdict']=='survived'"
jcheck "T7 fabricated 作废列表含 c-fabricated 与 c-hidden" "$TMP/verify-fabricated.json" \
  "set(d['voided'])=={'c-fabricated','c-hidden'} and d['void_count']==2 and d['valid_count']==1"
jcheck "T7 c-real 仍为 valid，c-fabricated/c-hidden 为 void" "$TMP/verify-fabricated.json" \
  "{c['id']:c['_status'] for c in d['citations']}=={'c-real':'valid','c-fabricated':'void','c-hidden':'void'}"
jcheck "T7 fabricated 快照不收录不存在文件" "$TMP/verify-fabricated.json" \
  "not os.path.exists(os.path.join(d['snapshot_dir'],'src/internal/hidden.py'))"

NOGIT="$TMP/nogit-repo"; mkdir -p "$NOGIT/src"
echo 'x = 1' > "$NOGIT/src/x.py"
run_rc "T7 无 git 基准 → fail-closed exit 2" 2 \
  "$PY" "$DIR/verify-evidence.py" --report-in "$VFIX/report-valid.json" \
    --repo-dir "$NOGIT" --report-out "$TMP/verify-nogit.json"

echo "-----"
if [[ $FAILS -eq 0 ]]; then
  echo "adversary 自测全部通过（T0-T7）"
  exit 0
fi
echo "adversary 自测失败 $FAILS 项"
exit 1
