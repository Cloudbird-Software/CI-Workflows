#!/usr/bin/env bash
# suppression-budget-selftest.sh —— 抑制标记预算门自测（.github #87 预先指定的测试方法）
#
# T1 阈值边界：+3 绿 / +4 红（错误信息指明 4>3）/ 净减绿
# T2 总量维度：单 PR 达标但总量超基线 → 红；红因与 T1 显式区分
# T3 标记全集：每种已声明标记 ≥1 例 + 相似干扰项——计数与预标注完全一致，无漏报无误报
# T4 逃生门：引用 scope 覆盖的 ADR → 绿且入账；scope 不覆盖/幽灵 ADR → 不豁免
# T5 基线消费：基线 ±1 重跑同一 PR，判定翻转（判定消费 policy 而非硬编码）
# T6 fail-closed：输入缺失/未知仓/畸形 policy → exit 2
#
# 本文件是构造性 fixture 载体（含标记字面样本），已列入 policy count_exclude
# [CI-Workflows]——不计入本仓总量基线。
set -euo pipefail

GATE="$(cd "$(dirname "$0")" && pwd)/suppression-budget.sh"
POLICY_REAL="$(cd "$(dirname "$0")/.." && pwd)/policy/suppressions.yaml"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
OUT="$W/out"; ERR="$W/err"

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL: $1"; cat "$OUT" "$ERR" 2>/dev/null | sed 's/^/    | /' >&2; FAIL=$((FAIL + 1)); }

run() {  # set +e 跑门，捕获 rc/stdout/stderr
  set +e
  bash "$GATE" "$@" >"$OUT" 2>"$ERR"
  RC=$?
  set -e
}
expect_exit() {  # $1=期望 rc, $2=描述
  [ "$RC" -eq "$1" ] && ok "$2（exit=$RC）" || bad "$2：期望 exit=$1 实得 $RC"
}
expect_has() {  # $1=子串（stdout+stderr 合查）, $2=描述
  grep -qF -- "$1" "$OUT" "$ERR" 2>/dev/null && ok "$2" || bad "$2：输出未含 '$1'"
}
expect_lacks() {  # $1=子串, $2=描述
  if grep -qF -- "$1" "$OUT" "$ERR" 2>/dev/null; then bad "$2：输出不应含 '$1'"; else ok "$2"; fi
}

# ========== fixture：场景 policy（T1/T2/T4/T5；阈值 3、基线 5） ==========
cat >"$W/policy.yaml" <<'YAML'
version: 1
per_pr_max_net_add: 3
escape_hatch: {adr_repo: Cloudbird-Software/archive, adr_dir: adr, scope_marker: selftest-scope}
markers:
  - {id: noqa, kind: regex, pattern: '#[[:space:]]*noqa\b'}
  - {id: eslint-disable, kind: regex, pattern: '\beslint-disable\b'}
  - {id: gitleaksignore-lines, kind: file, file: '.gitleaksignore'}
baselines:
  selftest-repo: 5
count_exclude: {}
YAML
sed 's/selftest-repo: 5/selftest-repo: 6/' "$W/policy.yaml" >"$W/policy-b6.yaml"   # T5 基线+1
sed 's/selftest-repo: 5/selftest-repo: 4/' "$W/policy.yaml" >"$W/policy-b4.yaml"   # T5 基线-1

# ========== fixture：树 ==========
mk_tree() {  # $1=目录 $2=noqa 数
  mkdir -p "$1/src" "$1/docs"
  i=1
  while [ "$i" -le "$2" ]; do
    printf 'v%d = %d  # noqa\n' "$i" "$i" >"$1/src/f$i.py"
    i=$((i + 1))
  done
  printf 'README with prose mentioning noqa only in prose (no hash-marker form).\n' >"$1/docs/README.md"
}
mk_tree "$W/tree5" 5    # 总量 5 = 基线（持平绿）
mk_tree "$W/tree6" 6    # 总量 6 > 基线（T2 红 / T5 +1 后绿）
mk_tree "$W/tree3" 3    # 净减后树（T1 P-C）

# ========== fixture：diff ==========
diff_hunk() {  # $1=文件路径 $2..=内容行（自带 +/- 前缀）
  printf 'diff --git a/%s b/%s\nindex 1111111..2222222 100644\n--- a/%s\n+++ b/%s\n@@ -1,1 +1,%d @@\n' \
    "$1" "$1" "$1" "$1" "$(( $# - 1 ))"
  shift
  printf '%s\n' "$@"
}
{ diff_hunk src/a.py '+x1 = 1  # noqa' '+x2 = 2  # noqa'
  diff_hunk src/b.py '+y1 = 3  # noqa'
} >"$W/diff-a.diff"                                            # P-A：+3
{ diff_hunk src/a.py '+x1 = 1  # noqa' '+x2 = 2  # noqa' '+x3 = 3  # noqa' '+x4 = 4  # noqa'
} >"$W/diff-b.diff"                                            # P-B：+4
{ diff_hunk src/f1.py '-v1 = 1  # noqa'
  diff_hunk src/f2.py '-v2 = 2  # noqa'
} >"$W/diff-c.diff"                                            # P-C：-2（净减）
{ diff_hunk src/a.py '+x1 = 1  # noqa' '+x2 = 2  # noqa'
} >"$W/diff-d.diff"                                            # P-D：+2（单 PR 达标）

# ========== fixture：逃生门 ADR ==========
mkdir -p "$W/adrs"
printf -- '- status: accepted（2026-01-01）\nscope 覆盖：selftest-scope 门测试用 ADR。\n' >"$W/adrs/ADR-0099-selftest-scope.md"
printf -- '- status: accepted（2026-01-01）\n无关 scope 的 ADR。\n' >"$W/adrs/ADR-0088-other-scope.md"

echo "== T1 阈值边界（参数化阈值=3）=="
run --repo selftest-repo --diff "$W/diff-a.diff" --tree "$W/tree5" --policy "$W/policy.yaml"
expect_exit 0 "P-A：新增 3 个 noqa → 绿"
expect_has "SUPPRESSION-OK" "P-A 输出含绿判定行"
run --repo selftest-repo --diff "$W/diff-b.diff" --tree "$W/tree5" --policy "$W/policy.yaml"
expect_exit 1 "P-B：新增 4 个 noqa → 红"
expect_has "净增 4 > 阈值 3" "P-B 错误信息指明超阈值 4>3"
expect_has "SUPPRESSION-BUDGET-PR" "P-B 红因标记为单 PR 维度"
expect_lacks "SUPPRESSION-BUDGET-TOTAL" "P-B 不涉总量红因"
run --repo selftest-repo --diff "$W/diff-c.diff" --tree "$W/tree3" --policy "$W/policy.yaml"
expect_exit 0 "P-C：删除 2 个现有抑制标记 → 绿"
expect_has "net=-2" "P-C 机器行净增为 -2"

echo "== T2 总量维度 =="
run --repo selftest-repo --diff "$W/diff-d.diff" --tree "$W/tree6" --policy "$W/policy.yaml"
expect_exit 1 "P-D：+2（单 PR 达标）但总量 6 超基线 5 → 红"
expect_has "SUPPRESSION-BUDGET-TOTAL" "P-D 红因标记为总量维度"
expect_has "总量 6 > 基线 5" "P-D 错误信息指明总量与基线值"
expect_lacks "SUPPRESSION-BUDGET-PR" "P-D 无单 PR 维度红因（两种红因可区分）"

echo "== T3 标记全集覆盖（真实 policy；每标记 1 例 + 干扰项）=="
{
  diff_hunk src/py.py '+a = 1  # noqa' '+# 讨论 noqa 用法（干扰项：# 后有字样前缀文字）'
  diff_hunk src/js.js '+// eslint-disable-next-line no-undef' '+// eslint-disabled legacy note（干扰项）'
  diff_hunk src/ty.py '+foo(a, b)  # type: ignore' '+the type: ignore count（干扰项：无 # 前缀）'
  diff_hunk src/pr.py '+y = 2  # pyright: ignore' '+pyright: ignore noted in prose（干扰项）'
  diff_hunk src/ns.py '+shell_cmd()  # nosec' '+nosec B101 removed（干扰项：无 # 前缀）'
  diff_hunk src/go.go '+if debug:  # pragma: no cover' '+pragma: no coverage left（干扰项）'
  diff_hunk src/cv.py '+def _repr():  # coverage: ignore' '+# coverage: ignored-lines note（干扰项）'
  diff_hunk src/ist.js '+/* istanbul ignore next */' '+istanbul coverage ignore policy（干扰项）'
  diff_hunk src/gl.txt '+api_key = "fake" # gitleaks:allow' '+gitleaks allows inline prose（干扰项）'
  diff_hunk src/dep.js '+// depcruise:ignore' '+depcruise ignores documentation（干扰项：复数形式）'
  diff_hunk .gitleaksignore '+33f3c3f3c3f3c3f3c3f3c3f3c3f3c3f3c3f3c3f3c3f3c3f3c3f3c3f3' '+# 指纹豁免清单（注释行）' '+' '+44e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4e4'
} >"$W/diff-t3.diff"
mkdir -p "$W/tree-t3"
printf 'context\n' >"$W/tree-t3/ctx.txt"
run --repo no-such-repo --diff "$W/diff-t3.diff" --tree "$W/tree-t3" --policy "$POLICY_REAL"
expect_exit 2 "T3：未知仓无基线 → fail-closed（exit 2）——计数先落盘可审计"
expect_has "net=12" "T3：新增计数 = 10 正则标记 + 2 文件行 = 12（干扰项零误报）"
expect_has "SUPPRESSION_MARKER noqa diff+1 " "T3：noqa 恰命中 1（干扰项未命中）"
expect_has "SUPPRESSION_MARKER eslint-disable diff+1 " "T3：eslint 禁用注释恰命中 1"
expect_has "SUPPRESSION_MARKER type-ignore diff+1 " "T3：mypy 忽略恰命中 1"
expect_has "SUPPRESSION_MARKER pyright-ignore diff+1 " "T3：pyright 忽略恰命中 1"
expect_has "SUPPRESSION_MARKER nosec diff+1 " "T3：bandit 豁免恰命中 1"
expect_has "SUPPRESSION_MARKER pragma-no-cover diff+1 " "T3：覆盖率排除恰命中 1"
expect_has "SUPPRESSION_MARKER coverage-ignore diff+1 " "T3：coverage 忽略恰命中 1"
expect_has "SUPPRESSION_MARKER istanbul-ignore diff+1 " "T3：istanbul 忽略恰命中 1"
expect_has "SUPPRESSION_MARKER gitleaks-allow diff+1 " "T3：gitleaks 行内豁免恰命中 1"
expect_has "SUPPRESSION_MARKER depcruise-ignore diff+1 " "T3：arch-lint 行内豁免恰命中 1"
expect_has "SUPPRESSION_MARKER gitleaksignore-lines diff+2 " "T3：指纹豁免文件 2 行计入、注释与空行不计"
# 标记集双向核对：policy 声明的每个标记都被本 fixture 覆盖，反之亦然（无漏报）
grep -oE '^SUPPRESSION_MARKER [a-z0-9-]+' "$OUT" | cut -d' ' -f2 | sort >"$W/got.ids"
printf '%s\n' noqa eslint-disable type-ignore pyright-ignore nosec pragma-no-cover coverage-ignore \
  istanbul-ignore gitleaks-allow depcruise-ignore gitleaksignore-lines | sort >"$W/want.ids"
if diff -u "$W/want.ids" "$W/got.ids" >"$W/ids.diff"; then
  ok "T3：真实 policy 标记全集与 fixture 覆盖一一对应（无漏报无多余）"
else
  bad "T3：标记集不匹配（policy 新增标记须同步补 fixture）：$(cat "$W/ids.diff" | tr '\n' ' ')"
fi

echo "== T4 逃生门 =="
run --repo selftest-repo --diff "$W/diff-b.diff" --tree "$W/tree5" --policy "$W/policy.yaml" \
    --pr-title "fix: 批量豁免（ADR-0099）" --adr-dir "$W/adrs"
expect_exit 0 "T4 正向：复刻 P-B + 引用 scope 覆盖的 ADR → 绿"
expect_has "SUPPRESSION-EXEMPT ADR-0099" "T4：豁免显式入账（ADR 编号）"
expect_has "净增 4" "T4：入账记录净增量"
expect_has "红因：SUPPRESSION-BUDGET-PR" "T4：入账记录被豁免的红因"
expect_lacks "棘轮同步义务" "T4：仅单 PR 维度红（总量未越线）→ 无基线上调义务"
run --repo selftest-repo --diff "$W/diff-b.diff" --tree "$W/tree6" --policy "$W/policy.yaml" \
    --pr-title "fix: 批量豁免（ADR-0099）" --adr-dir "$W/adrs"
expect_exit 0 "T4 正向：两维度皆红（净增 4>3 且总量 6>5）+ scope 覆盖 ADR → 绿"
expect_has "棘轮同步义务" "T4：总量越线的豁免含基线上调义务声明"
expect_has "上调至 6" "T4：义务指明目标基线值"
run --repo selftest-repo --diff "$W/diff-b.diff" --tree "$W/tree5" --policy "$W/policy.yaml" \
    --pr-title "fix: 批量豁免（ADR-0088）" --adr-dir "$W/adrs"
expect_exit 1 "T4 负向：引用 scope 不覆盖的 ADR → 不豁免（仍红）"
expect_has "不构成豁免" "T4 负向：输出说明 scope 不覆盖"
run --repo selftest-repo --diff "$W/diff-b.diff" --tree "$W/tree5" --policy "$W/policy.yaml" \
    --pr-title "fix: 批量豁免（ADR-0077）" --adr-dir "$W/adrs"
expect_exit 1 "T4 负向：幽灵 ADR（文件不存在）→ 不豁免"

echo "== T5 基线消费（±1 翻转）=="
run --repo selftest-repo --diff "$W/diff-d.diff" --tree "$W/tree6" --policy "$W/policy-b6.yaml"
expect_exit 0 "T5：基线 5→6（+1）同一 PR 红→绿（判定消费 policy 基线）"
run --repo selftest-repo --diff "$W/diff-a.diff" --tree "$W/tree5" --policy "$W/policy-b4.yaml"
expect_exit 1 "T5：基线 5→4（-1）同一 PR 绿→红（非硬编码）"
expect_has "总量 5 > 基线 4" "T5：翻转红因指向基线差"

echo "== T6 fail-closed =="
run --repo selftest-repo --diff "$W/nonexistent.diff" --tree "$W/tree5" --policy "$W/policy.yaml"
expect_exit 2 "T6：diff 文件缺失 → exit 2"
printf 'version: 2\n' >"$W/policy-bad.yaml"
run --repo selftest-repo --diff "$W/diff-a.diff" --tree "$W/tree5" --policy "$W/policy-bad.yaml"
expect_exit 2 "T6：畸形 policy（version 错）→ exit 2"
expect_has "policy 无效" "T6：畸形 policy 错误信息可辨"
printf 'version: 1\nper_pr_max_net_add: 3\nmarkers:\n  - {id: noqa, kind: regex, pattern: x}\n' >"$W/policy-nobl.yaml"
run --repo selftest-repo --diff "$W/diff-a.diff" --tree "$W/tree5" --policy "$W/policy-nobl.yaml"
expect_exit 2 "T6：policy 缺 baselines 段 → exit 2"

echo
echo "======================================"
echo "SELFTEST RESULT: PASS=$PASS FAIL=$FAIL"
echo "======================================"
[ "$FAIL" -eq 0 ]
