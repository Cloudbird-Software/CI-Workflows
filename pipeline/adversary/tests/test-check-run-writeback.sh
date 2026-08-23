#!/usr/bin/env bash
# test-check-run-writeback.sh —— check_run_writeback.py 自测（W4-C2 .github#283，INV-02/AC-4）
#
# 零网络、零凭据。分两层：
#   1) 纯逻辑（schema 校验、verdict→conclusion 映射、output 截断、check name）
#      → 由 tests/test_check_run_writeback_logic.py 承担（自算 fixture 路径）
#   2) CLI 行为（schema 非法 exit 4、无定位 exit 2、环境缺失 exit 2）
#      → 本脚本在 pipeline/adversary 目录下用相对路径驱动，避 MSYS 路径转换
# API 写回路径由 workflow 端到端实测覆盖，不在本自测范围。
#
# 断言：
#   W0 语法预检（双 python ast + fixture JSON 可解析）
#   W1 纯逻辑（映射 + 截断 + name）
#   W2 CLI：非法 schema → exit 4
#   W3 CLI：无 --spec-pr 且无 target → exit 2
#   W4 CLI：环境缺失（无令牌无 CB_APP_ID）→ exit 2
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="python"
FAILS=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; FAILS=$((FAILS+1)); }
run_rc() {  # run_rc <名> <期望rc> cmd...
  local name="$1" want="$2"; shift 2
  local rc=0
  LAST_OUT=$("$@" 2>&1) || rc=$?
  if [[ "$rc" == "$want" ]]; then pass "$name（rc=$rc）"; else fail "$name：rc=$rc 期望=$want（输出：${LAST_OUT:0:400}）"; fi
}

# 进入 pipeline/adversary 目录，所有路径用相对形式（MSYS 安全）
cd "$DIR" || exit 1
FIX="tests/fixtures/check_run_writeback"
# 用仓内临时目录（MSYS 安全），避免 mktemp 返回 /c/... 路径 Python 不识
TMP=".test-tmp-writeback"; rm -rf "$TMP"; mkdir -p "$TMP"; trap 'rm -rf "$TMP"' EXIT

# ---- W0 语法预检 ----
"$PY" -c "import ast;ast.parse(open('check_run_writeback.py',encoding='utf-8').read())" \
  && pass "W0 python 语法 check_run_writeback.py" || fail "W0 语法错误"
"$PY" -c "import ast;ast.parse(open('tests/test_check_run_writeback_logic.py',encoding='utf-8').read())" \
  && pass "W0 python 语法 test_check_run_writeback_logic.py" || fail "W0 测试助手语法错误"
for f in "$FIX"/report-*.json; do
  "$PY" -c "import json;json.load(open('$f',encoding='utf-8'))" \
    && pass "W0 fixture 可解析 $(basename $f)" || fail "W0 fixture 不可解析 $f"
done

# ---- W1 纯逻辑层（test_check_run_writeback_logic.py 自算 fixture 路径）----
run_rc "W1 纯逻辑自测（映射+截断+name）" 0 \
  "$PY" "tests/test_check_run_writeback_logic.py"

# ---- W2 CLI：非法 schema → exit 4 ----
BAD="$TMP/bad.json"
"$PY" -c "import json;d=json.load(open('$FIX/report-survived.json',encoding='utf-8'));d['schema']='wrong';json.dump(d,open('$BAD','w',encoding='utf-8'))"
run_rc "W2 非法 schema → exit 4" 4 \
  env APP_TOKEN="ghs_dummy" "$PY" "check_run_writeback.py" --report "$BAD" --spec-pr 1

# ---- W3 CLI：无 --spec-pr 且无 target → exit 2 ----
NOTARGET="$TMP/no-target.json"
"$PY" -c "import json;d=json.load(open('$FIX/report-survived.json',encoding='utf-8'));del d['target'];json.dump(d,open('$NOTARGET','w',encoding='utf-8'))"
run_rc "W3 无 --spec-pr 且无 target → exit 2" 2 \
  env APP_TOKEN="ghs_dummy" "$PY" "check_run_writeback.py" --report "$NOTARGET"

# ---- W4 CLI：环境缺失铸造路径 ----
run_rc "W4 无令牌无 CB_APP_ID → exit 2" 2 \
  env -u APP_TOKEN -u CB_APP_ID "$PY" "check_run_writeback.py" --report "$FIX/report-survived.json" --spec-pr 1

echo "-----"
if [[ $FAILS -eq 0 ]]; then
  echo "check_run_writeback 自测全部通过（W0-W4）"
  exit 0
fi
echo "check_run_writeback 自测失败 $FAILS 项"
exit 1
