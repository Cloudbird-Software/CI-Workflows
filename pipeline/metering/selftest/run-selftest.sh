#!/usr/bin/env bash
# run-selftest.sh —— 计量 wrapper 自测（W2-C3 .github#216，ADR-0062 决策 6）
#
# 判定物有效性（宪法 §4E）：零真实 LLM 调用（--replay-file 离线回放），断言：
#   T1 流式聚合（AC-1）：多分片一次 invoke → 账本恰一条聚合记录；同 invoke_id
#      二次落账被拒（spike T8 分片重复计数教训的机器执法）
#   T2 BEH-09 字段齐全（AC-2）：model/prompt 版本/seed/采样参数/用量/耗时/
#      exit 状态/产物 hash/链字段逐项断言
#   T3 验链+篡改负控制（AC-2）：改中间记录字段/链哈希 → metering-verify 红；
#      schema 负控制（多余字段）→ 红
#   T4 扫描双形态（AC-3）：fixture 含直连 → 命中 4 类模式 exit 1；合规 → 绿；
#      行内豁免 → 放行留痕
#   T5 按角色档归账（AC-4 引擎）：role 聚合 token 正确；窗口过滤；坏账本拒归账
#   T6 llm-usage/v1 兼容件（spec-pr.py 等既有下游零改动）
#   T7 ledger-sync 干跑（gh 桩）：relink 续接 + 合并片验链 + 写回预演
# 用法: bash pipeline/metering/selftest/run-selftest.sh（CI job 与本地同路径）
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
FIX="$DIR/selftest/fixtures"
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
  LAST_OUT=$(GATE_METERING_DIR="$METER" "$@" 2>&1) || rc=$?
  if [[ "$rc" == "$want" ]]; then pass "$name（rc=$rc）"; else fail "$name：rc=$rc 期望=$want（输出：${LAST_OUT:0:400}）"; fi
}

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
METER="$TMP/metering"; mkdir -p "$METER"

# ---- 0) 语法预检（bash -n + python ast——不产 __pycache__） ----
for s in metering-wrapper.sh metering-verify.sh scan-direct-sdk.sh ledger-sync.sh; do
  bash -n "$DIR/$s" && pass "bash -n $s" || fail "bash -n $s"
done
"$PY" -c 'import ast,sys;ast.parse(open(sys.argv[1],encoding="utf-8").read())' "$DIR/metering.py" \
  && pass "python 语法 metering.py" || fail "python 语法 metering.py"

# ---- T1 流式聚合：5 分片一次 invoke → 恰一条记录（AC-1） ----
LAST_OUT=$(GATE_METERING_DIR="$METER" bash "$DIR/metering-wrapper.sh" \
  --model glm-4.5-air --prompt-file "$FIX/prompt.txt" --role probe --tag selftest-stream \
  --max-tokens 64 --temperature 0 --seed 7 --stream \
  --replay-file "$FIX/stream-response.sse" --invoke-id selftest-invoke-001 2>"$TMP/t1.err")
RC1=$?
STDOUT_CONTENT="$LAST_OUT"
if [[ $RC1 -eq 0 && "$STDOUT_CONTENT" == "$(cat "$FIX/stream-expected.txt")" ]]; then
  pass "T1 流式回放成功且聚合正文正确（'${STDOUT_CONTENT}'）"
else
  fail "T1 流式回放：rc=$RC1 stdout='$STDOUT_CONTENT'（期望 '$(cat "$FIX/stream-expected.txt")'）err=$(head -c 300 "$TMP/t1.err")"
fi
LEDGER=$(ls "$METER"/records-*.jsonl 2>/dev/null | head -1)
[[ -n "$LEDGER" ]] || { fail "T1 账本未落盘"; exit 1; }
N=$(wc -l <"$LEDGER" | tr -d ' ')
[[ "$N" == "1" ]] && pass "T1 5 分片聚合成恰 1 条 invoke 记录（spike T8 教训不复发）" \
  || fail "T1 记录数=$N 期望 1（分片被重复计数）"

# ---- T1b 非流式 + 兼容件目录（同账本续链） ----
run_rc "T1b 非流式回放" 0 bash "$DIR/metering-wrapper.sh" \
  --model glm-4.5-air --prompt-file "$FIX/prompt.txt" --role spec-author --tag selftest-plain \
  --max-tokens 128 --temperature 0.2 --thinking disabled \
  --replay-file "$FIX/plain-response.json" --invoke-id selftest-invoke-002 \
  --usage-compat-dir "$TMP/usage"
N=$(wc -l <"$LEDGER" | tr -d ' ')
[[ "$N" == "2" ]] && pass "T1b 两次 invoke → 2 条记录（各自恰一条）" || fail "T1b 记录数=$N 期望 2"

# ---- T1c invoke_id 去重执法（聚合键=invoke_id，ADR-0062 决策 2） ----
run_rc "T1c 同 invoke_id 二次落账被拒（exit 3）" 3 bash "$DIR/metering-wrapper.sh" \
  --model glm-4.5-air --prompt-file "$FIX/prompt.txt" --role probe --max-tokens 16 \
  --stream --replay-file "$FIX/stream-response.sse" --invoke-id selftest-invoke-001
N=$(wc -l <"$LEDGER" | tr -d ' ')
[[ "$N" == "2" ]] && pass "T1c 拒绝后账本仍 2 条（无重复计数）" || fail "T1c 账本=$N 期望 2"

# ---- T2 BEH-09 字段齐全断言（AC-2） ----
"$PY" - "$LEDGER" <<'PYEOF'
import hashlib, json, sys
recs = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
r0, r1 = recs[0], recs[1]
need = ["model", "prompt_version", "seed", "sampling", "usage", "latency_ms",
        "exit_status", "artifacts", "stream", "chunks", "invoke_id", "role",
        "ts_start", "ts_end", "record_sha256", "prev_record_sha256"]
miss = [k for k in need if k not in r0]
assert not miss, f"BEH-09 字段缺失: {miss}"
assert r0["model"] == "glm-4.5-air" and r0["role"] == "probe" and r0["seed"] == 7
assert r0["prompt_version"].startswith("sha256:") and len(r0["prompt_version"]) == 71
assert r0["sampling"]["temperature"] == 0 and r0["sampling"]["max_tokens"] == 64
assert r0["sampling"]["top_p"] is None and r0["sampling"]["thinking"] is None
assert r0["usage"] == {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}, r0["usage"]
assert r0["exit_status"] == "ok" and r0["stream"] is True and r0["chunks"] == 5, (r0["exit_status"], r0["stream"], r0["chunks"])
assert {a["name"] for a in r0["artifacts"]} == {"request", "response"}
assert all(a["sha256"].startswith("sha256:") and len(a["sha256"]) == 71 and a["bytes"] > 0 for a in r0["artifacts"])
assert r0["latency_ms"] >= 0 and r0["record_index"] == 0 and r0["prev_record_sha256"] is None
assert r1["record_index"] == 1 and r1["prev_record_sha256"] == r0["record_sha256"], "链前驱断裂"
assert r1["role"] == "spec-author" and r1["stream"] is False and r1["chunks"] == 1
assert r1["sampling"]["thinking"] == "disabled" and r1["sampling"]["max_tokens"] == 128
assert r1["usage"]["total_tokens"] == 42
for r in recs:  # 自哈希重算（canonical：除 record_sha256 外排序键紧凑序列化）
    body = {k: v for k, v in r.items() if k != "record_sha256"}
    h = "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                              separators=(",", ":")).encode("utf-8")).hexdigest()
    assert h == r["record_sha256"], f"自哈希不符 idx={r['record_index']}"
print("T2 OK")
PYEOF
[[ $? -eq 0 ]] && pass "T2 BEH-09 字段齐全 + 自哈希重算一致 + 链前驱正确" || fail "T2 字段断言（见上）"

# ---- T3 验链 + 篡改负控制（AC-2：改一条中间字段 → 验链必红） ----
run_rc "T3 未篡改验链绿" 0 bash "$DIR/metering-verify.sh" --dir "$METER"
cp "$LEDGER" "$TMP/ledger.bak"
"$PY" - "$LEDGER" <<'PYEOF'
import json, sys
p = sys.argv[1]
lines = open(p, encoding="utf-8").read().splitlines()
r = json.loads(lines[0]); r["usage"]["completion_tokens"] = 999  # 篡改中间字段
lines[0] = json.dumps(r, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
open(p, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
PYEOF
run_rc "T3 篡改中间字段 → 验链红（负控制）" 3 bash "$DIR/metering-verify.sh" --dir "$METER"
echo "$LAST_OUT" | grep -q "record_sha256" && pass "T3 篡改被定位到哈希断言" || fail "T3 报告缺哈希断言细节"
run_rc "T3 坏账本拒绝归账（不可信不入账）" 3 "$PY" "$DIR/metering.py" aggregate --dir "$METER" --since 2026-01-01 --json
cp "$TMP/ledger.bak" "$LEDGER"
run_rc "T3 复原后验链复绿" 0 bash "$DIR/metering-verify.sh" --file "$LEDGER"
# schema 负控制：多余字段（additionalProperties:false 必须真拦）
"$PY" - "$LEDGER" "$METER" <<'PYEOF'
import json, os, sys
src, meter_dir = sys.argv[1], sys.argv[2]
r = json.loads(open(src, encoding="utf-8").read().splitlines()[0])
r["bogus_field"] = "schema-negcontrol"
open(os.path.join(meter_dir, "records-2099-W01.jsonl"), "w", encoding="utf-8", newline="\n").write(
    json.dumps(r, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
PYEOF
run_rc "T3 schema 负控制（多余字段）→ 验链红" 3 bash "$DIR/metering-verify.sh" --file "$METER/records-2099-W01.jsonl"
rm -f "$METER/records-2099-W01.jsonl"

# ---- T4 扫描双形态（AC-3）：fixture 拷出豁免区再扫（豁免只对仓根默认扫描生效） ----
SCAN="$TMP/scan"; mkdir -p "$SCAN"; cp "$FIX"/bad-direct.py "$FIX"/ok-via-wrapper.sh "$FIX"/bad-with-allow.py "$SCAN/"
run_rc "T4 含直连 fixture → 命中红" 1 bash "$DIR/scan-direct-sdk.sh" "$SCAN/bad-direct.py"
for pid in openai-sdk-import openai-client-init curl-direct-llm http-lib-llm-endpoint; do
  echo "$LAST_OUT" | grep -q "$pid" && pass "T4 检出 $pid" || fail "T4 未检出 $pid（输出：${LAST_OUT:0:300}）"
done
run_rc "T4 合规 fixture（经 wrapper）→ 绿" 0 bash "$DIR/scan-direct-sdk.sh" "$SCAN/ok-via-wrapper.sh"
run_rc "T4 行内豁免 → 绿且留痕" 0 bash "$DIR/scan-direct-sdk.sh" "$SCAN/bad-with-allow.py"
echo "$LAST_OUT" | grep -q "ALLOW" && pass "T4 豁免留痕可见（ALLOW 行）" || fail "T4 豁免无留痕"
run_rc "T4 模式表不可读 → fail-closed（exit 2）" 2 bash "$DIR/scan-direct-sdk.sh" --patterns "$TMP/nope.yaml" "$SCAN"

# ---- T5 按角色档归账（AC-4 引擎） ----
AGG=$("$PY" "$DIR/metering.py" aggregate --dir "$METER" --since 2026-01-01 --json) \
  && pass "T5 归账运行" || fail "T5 归账运行失败"
"$PY" - "$AGG" <<'PYEOF'
import json, sys
d = json.loads(sys.argv[1])
assert d["roles"]["probe"] == {"invokes": 1, "prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}, d["roles"]
assert d["roles"]["spec-author"]["total_tokens"] == 42, d["roles"]
assert d["totals"]["total_tokens"] == 207 and d["totals"]["invokes"] == 2, d["totals"]
assert d["records"] == 2
print("T5 OK")
PYEOF
[[ $? -eq 0 ]] && pass "T5 角色档归账正确（probe=165 / spec-author=42 / 合计=207）" || fail "T5 归账数值断言"
FUTURE=$("$PY" -c "import datetime;print((datetime.date.today()+datetime.timedelta(days=2)).isoformat())")
AGG2=$("$PY" "$DIR/metering.py" aggregate --dir "$METER" --since "$FUTURE" --json)
"$PY" - "$AGG2" <<'PYEOF'
import json, sys
d = json.loads(sys.argv[1])
assert d["records"] == 0 and d["totals"]["total_tokens"] == 0, d
print("OK")
PYEOF
[[ $? -eq 0 ]] && pass "T5 窗口过滤生效（--since 未来 → 0 条）" || fail "T5 窗口过滤"

# ---- T6 llm-usage/v1 兼容件（既有下游 spec-pr.py / run 摘要零改动） ----
U=$(ls "$TMP/usage"/usage-*.json 2>/dev/null | head -1)
if [[ -n "$U" ]] && "$PY" -c 'import json,sys;d=json.load(open(sys.argv[1],encoding="utf-8"));assert d["schema"]=="llm-usage/v1" and d["usage"]["total_tokens"]==42 and d["model"]=="glm-4.5-air" and d["response_sha256"].startswith("sha256:")' "$U"; then
  pass "T6 兼容件字段可被既有下游消费（model/tokens/response_sha256）"
else
  fail "T6 兼容件缺失或字段不符（$U）"
fi

# ---- T7 ledger-sync 干跑（gh 桩：分支未建→建、contents 404→创世续接） ----
BIN="$TMP/bin"; mkdir -p "$BIN"
cat >"$BIN/gh" <<'STUB'
#!/usr/bin/env bash
# selftest gh 桩：main ref 返回定值；其余（ledger 分支/contents）模拟 404
if [[ "$*" == *"git/ref/heads/main"* ]]; then
  echo '{"object":{"sha":"deadbeefcafe0000000000000000000000000000"}}'; exit 0
fi
exit 1
STUB
chmod +x "$BIN/gh"
OUT7=$(PATH="$BIN:$PATH" GH_TOKEN=selftest-dummy METERING_PYTHON="$PY" \
  bash "$DIR/ledger-sync.sh" --dir "$METER" --repo demo/edge --branch metering-ledger --dry-run 2>&1)
RC7=$?
# 断言干跑全程（建分支/创世续接/合并片验链）通过且只报不写（写操作经 mutate 桩为 DRY，
# 其输出重定向到 /dev/null——以"链验通过+同步完成"与退出码判定路径完整性）
if [[ $RC7 -eq 0 ]] && echo "$OUT7" | grep -q "同步完成：1 片更新" && echo "$OUT7" | grep -q "链验通过"; then
  pass "T7 ledger-sync 干跑：建分支/创世续接/合并片验链/写回预演通过"
else
  fail "T7 ledger-sync 干跑 rc=$RC7（输出：${OUT7:0:400}）"
fi

echo "----------------------------------------"
if [[ $FAILS -eq 0 ]]; then echo "SELFTEST PASS：全部断言绿（零真实 LLM 调用）"; exit 0; fi
echo "SELFTEST FAIL：$FAILS 处断言失败"; exit 1
