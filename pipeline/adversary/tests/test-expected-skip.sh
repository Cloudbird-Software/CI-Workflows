#!/usr/bin/env bash
# test-expected-skip.sh —— expected_skip.py 自测（W4-C3 .github#284，AC-14/AC-19）
#
# 零网络、零凭据：直接调 judge 纯函数 + CLI judge 模式（传 --paths）。
# 断言：
#   E0 语法预检（python ast + 豁免清单 JSON 可解析）
#   E1 dev 路径（无 specs/**）→ expected_skip=True, rc=0
#   E2 specs/** 实质变更 → expected_skip=False, rc=1
#   E3 specs/** 全命中 owner 豁免清单 → expected_skip=True, rc=0
#   E4 部分命中豁免 + 剩余 specs → expected_skip=False, rc=1（fail-closed）
#   E5 未豁免 specs + dev 混合 → expected_skip=False, rc=1
#   E6 exemption_sha 随清单内容变化
#   E7 参数错：非法 --paths → rc=2
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
cd "$DIR" || exit 1
FIX="fixtures/expected_skip"
EXEMPT="$FIX/exemptions.json"

# ---- E0 语法预检 ----
"$PY" -c "import ast;ast.parse(open('expected_skip.py',encoding='utf-8').read())" \
  && pass "E0 python 语法 expected_skip.py" || fail "E0 语法错误"
"$PY" -c "import json;json.load(open('$EXEMPT',encoding='utf-8'))" \
  && pass "E0 豁免清单 JSON 可解析" || fail "E0 豁免清单不可解析"

# ---- E1 dev 路径 → skip=True, rc=0 ----
run_rc "E1 dev 路径（src/foo.py）→ rc=0 (skip)" 0 \
  "$PY" expected_skip.py judge --paths '["src/foo.py", "ciw/pipeline/x.py"]'
# 验证 JSON 输出含 expected_skip=true
OUT=$(env "$PY" expected_skip.py judge --paths '["src/foo.py"]' 2>/dev/null)
echo "$OUT" | "$PY" -c "import json,sys; d=json.load(sys.stdin); exit(0 if d['expected_skip'] is True and d['specs_paths']==[] else 1)" \
  && pass "E1 输出 expected_skip=true 且 specs_paths 为空" || fail "E1 输出断言不成立"

# ---- E2 specs/** 实质变更 → skip=False, rc=1 ----
run_rc "E2 specs 实质变更 → rc=1 (no skip)" 1 \
  "$PY" expected_skip.py judge --paths '["specs/ISSUE-263/spec.md"]'
echo "$OUT" > /dev/null
OUT1=$(env "$PY" expected_skip.py judge --paths '["specs/ISSUE-263/spec.md"]' 2>/dev/null)
echo "$OUT1" | "$PY" -c "import json,sys; d=json.load(sys.stdin); exit(0 if d['expected_skip'] is False and 'specs/ISSUE-263/spec.md' in d['specs_paths'] else 1)" \
  && pass "E2 输出 expected_skip=false 且 specs_paths 含 spec.md" || fail "E2 输出断言不成立"

# ---- E3 specs 全命中豁免清单 → skip=True（specs/**/CHANGELOG.md 在豁免清单内）----
run_rc "E3 specs 全命中豁免（specs/**/CHANGELOG.md）→ rc=0 (skip)" 0 \
  "$PY" expected_skip.py judge --paths '["specs/ISSUE-263/CHANGELOG.md", "docs/readme.md"]' \
  --exempt-list "$EXEMPT"
OUT2=$(env "$PY" expected_skip.py judge --paths '["specs/ISSUE-263/CHANGELOG.md"]' --exempt-list "$EXEMPT" 2>/dev/null)
echo "$OUT2" | "$PY" -c "import json,sys; d=json.load(sys.stdin); exit(0 if d['expected_skip'] is True and len(d['exempted_paths'])>=1 else 1)" \
  && pass "E3 输出 expected_skip=true 且 exempted_paths 非空" || fail "E3 输出断言不成立（out=$OUT2）"

# ---- E4 部分命中豁免 + 剩余 specs → skip=False（fail-closed）----
run_rc "E4 部分豁免+剩余 specs → rc=1 (no skip)" 1 \
  "$PY" expected_skip.py judge --paths '["specs/IR-1/CHANGELOG.md", "specs/IR-1/spec.md"]' \
  --exempt-list "$EXEMPT"
OUT3=$(env "$PY" expected_skip.py judge --paths '["specs/IR-1/CHANGELOG.md","specs/IR-1/spec.md"]' --exempt-list "$EXEMPT" 2>/dev/null)
echo "$OUT3" | "$PY" -c "import json,sys; d=json.load(sys.stdin); exit(0 if d['expected_skip'] is False and len(d['remaining_specs'])>=1 and len(d['exempted_paths'])>=1 else 1)" \
  && pass "E4 输出 expected_skip=false 且 remaining+exempted 均非空" || fail "E4 输出断言不成立（out=$OUT3）"

# ---- E5 未豁免 specs + dev 混合 → skip=False ----
run_rc "E5 specs+dev 混合（spec 未豁免）→ rc=1 (no skip)" 1 \
  "$PY" expected_skip.py judge --paths '["src/main.py", "specs/IR-1/suite/test_x.py"]'

# ---- E6 exemption_sha 随清单变化 ----
SHA1=$(env "$PY" expected_skip.py judge --paths '["src/x.py"]' --exempt-list "$EXEMPT" 2>/dev/null | "$PY" -c "import json,sys;print(json.load(sys.stdin)['exemption_sha'])")
SHA2=$(env "$PY" expected_skip.py judge --paths '["src/x.py"]' 2>/dev/null | "$PY" -c "import json,sys;print(json.load(sys.stdin)['exemption_sha'])")
if [[ -n "$SHA1" && -n "$SHA2" && "$SHA1" != "$SHA2" ]]; then
  pass "E6 exemption_sha 随清单文件变化（有清单≠无清单）"
else
  fail "E6 exemption_sha 未体现清单差异（sha1=$SHA1 sha2=$SHA2）"
fi

# ---- E7 参数错 → rc=2 ----
run_rc "E7 非法 --paths → rc=2" 2 \
  "$PY" expected_skip.py judge --paths 'not-a-json-array[['

echo "-----"
if [[ $FAILS -eq 0 ]]; then
  echo "expected_skip 自测全部通过（E0-E7）"
  exit 0
fi
echo "expected_skip 自测失败 $FAILS 项"
exit 1
