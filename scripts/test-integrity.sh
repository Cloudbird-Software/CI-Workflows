#!/usr/bin/env bash
# test-integrity.sh —— P2-1 测试篡改检测（.github #86 / ADR-0035）
#
# 四条规则（regex 级、语言无关；AST 级解析为后续增强，不在 #86 范围）：
#   TI-R1 测试文件删除（真实删除或改名移出测试路径；内容不变的 rename 不算）→ 红
#   TI-R2 测试文件断言计数净下降（全 PR 净额 < 0，文件间迁移不受影响）        → 红
#   TI-R3 测试文件新增抑制标记（skip/xfail/only/t.Skip/mark.skip 等）         → 红
#   TI-R4 期望值改写嫌疑（测试文件有删改行 + 零实现文件变更）                  → 严格度由
#        policy 声明（缺省 require_adr：引用 ADR 可豁免；可调为不可豁免 red）
#
# 逃生门：命中后 PR title/body 引用 \bADR-NNNN\b（存在性校验防幽灵 ADR）可豁免，
# 豁免计数入账（TI-ESC + step summary）。fail-closed：SHA 不可解析 / diff 生成失败 /
# ADR 清单读不到 = 红（TI-FC），绝不静默放行。
#
# 输入（环境变量）：
#   BASE_SHA/HEAD_SHA  git 模式：已全量 checkout 的仓内执行 base...head 三点 diff
#   DIFF_FILE          fixture 模式：直接喂构造的 `git diff` 输出文件（T8 单元级自检）
#   PR_TITLE/PR_BODY   逃生门 ADR 引用来源
#   GH_TOKEN / ADR_REGISTRY_API  豁免时拉 agent-registry/decisions 存在性清单
#   ADR_LISTING_FILE   本地清单文件（fixture 模式替代 GH_TOKEN）
#   TI_*               policy 覆盖（governance/policy/testing.yaml#test_integrity
#                      声明的模式/阈值；此处为同值内置缺省——policy 拉取失败时调用方
#                      应先红，而非依赖本缺省裸奔）
set -euo pipefail

# ── policy 内置缺省（与 governance/policy/testing.yaml#test_integrity 同值）──
: "${TI_TEST_FILE_RE:=((^|/)(tests?|__tests__|__snapshots__|testdata)(/|$)|_test\.go$|\.test\.[cm]?[jt]sx?$|\.spec\.[cm]?[jt]sx?$|(^|/)test_[^/]*\.py$|_test\.py$|_test\.rs$|\.snap$)}"
: "${TI_ASSERT_RE:=\bassert|\bexpect\(|\bexpect\.|\brequire\(|\brequire\.|\bshould\b|\bt\.Error|\bt\.Fatal|\bself\.assert|\bfail_if\(|\bAssert|\bExpect\(|\bRequire\(|\bSo\(}"
: "${TI_SUPPRESS_RE:=(^|[^A-Za-z_])([Ss]kip|[Xx][Ff][Aa][Ii][Ll])[[:space:]]*\(|\.skip\b|\.only\b|t\.Skip|mark\.skip|mark\.xfail|@Ignore|@Disabled|\[ignore|\.todo\(|unittest\.skip}"
: "${TI_META_PATH_RE:=^(\.github/|docs/|governance/)|(^|/)([^/]*\.md)$|(^|/)(LICENSE|CODEOWNERS|NOTICE|\.gitignore|\.gitattributes)$}"
: "${TI_R4_SEVERITY:=require_adr}"
[ "$TI_R4_SEVERITY" = "red" ] || [ "$TI_R4_SEVERITY" = "require_adr" ] || {
  echo "::error::TI-FC TI_R4_SEVERITY 非法（$TI_R4_SEVERITY，仅 red|require_adr）"; exit 1; }

tag() { echo "[$1] ${2:-}"; }

fail_closed() {
  echo "::error::TI-FC $*（检测器读不到=红——ADR-0035 fail-closed）"
  tag TI-FC "$*"
  exit 1
}

is_test() { [[ "$1" =~ $TI_TEST_FILE_RE ]]; }
is_meta() { [[ "$1" =~ $TI_META_PATH_RE ]]; }
count_lines() { printf '%s\n' "$1" | grep -c . || true; }

# ── 1. 取 diff（fail-closed）────────────────────────────────────────────
if [ -n "${DIFF_FILE:-}" ]; then
  [ -r "$DIFF_FILE" ] || fail_closed "DIFF_FILE 不可读: $DIFF_FILE"
  DIFF_SRC="$DIFF_FILE"
else
  [ -n "${BASE_SHA:-}" ] || fail_closed "BASE_SHA 缺失（git 模式必需）"
  [ -n "${HEAD_SHA:-}" ] || fail_closed "HEAD_SHA 缺失（git 模式必需）"
  git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null || fail_closed "BASE_SHA 不可解析: $BASE_SHA"
  git cat-file -e "${HEAD_SHA}^{commit}" 2>/dev/null || fail_closed "HEAD_SHA 不可解析: $HEAD_SHA"
  DIFF_SRC="$TMPDIR/ti-diff.txt"
  [ -n "${TMPDIR:-}" ] || DIFF_SRC="/tmp/ti-diff.txt"
  # -M：rename 识别（内容不变的 rename 不算删除）；-U0：无上下文，只看 +/- 行
  git -c core.quotepath=false diff -M --unified=0 --no-color \
    "${BASE_SHA}...${HEAD_SHA}" >"$DIFF_SRC" || fail_closed "git diff 生成失败"
fi

# ── 2. 解析 unified diff（行归属到文件；转义路径 fail-closed）────────────
cur_status="" cur_old="" cur_new=""
f_r=0 f_aa=0 f_ar=0 f_sa=0          # 文件级：删行/断言加/断言删/抑制加

assert_add=0 assert_rem=0 supp_add=0
deleted_tests="" supp_evidence="" test_evidence="" r4_suspects=""
impl_changed=0 test_mod_removed=0

reset_file() { cur_status="" cur_old="" cur_new=""; f_r=0 f_aa=0 f_ar=0 f_sa=0; }

UNQ=""
unquote() { # $1: a/x 或 b/x（可能带引号）；结果入 $UNQ（主 shell 执行，错误可见）
  local p="$1"
  case "$p" in
    '"'*)
      p="${p#\"}"; p="${p%\"}"
      case "$p" in *'\'*) fail_closed "diff 头路径含转义序列，行归属不可靠: $p" ;; esac
      ;;
  esac
  case "$p" in
    a/*) UNQ="${p#a/}" ;;
    b/*) UNQ="${p#b/}" ;;
    /dev/null) UNQ="" ;;
    *) fail_closed "diff 头路径形态异常: $p" ;;
  esac
}

finalize_file() {
  [ -n "$cur_status$cur_old$cur_new" ] || { reset_file; return 0; }
  local path="${cur_new:-$cur_old}"
  local old_test=0 new_test=0
  if [ -n "$cur_old" ]; then is_test "$cur_old" && old_test=1 || true; fi
  if [ -n "$cur_new" ]; then is_test "$cur_new" && new_test=1 || true; fi

  case "$cur_status" in
    R)
      # rename：old 是测试而 new 不是 = 测试资产移出测试路径（视同删除，TI-R1）
      if [ "$old_test" = 1 ] && [ "$new_test" = 0 ]; then
        deleted_tests="${deleted_tests}${cur_old}（改名移出测试路径→${cur_new}）
"
      fi
      [ "$old_test" = 1 ] && assert_rem=$((assert_rem + f_ar)) || true
      [ "$new_test" = 1 ] && assert_add=$((assert_add + f_aa)) || true
      [ "$new_test" = 1 ] && supp_add=$((supp_add + f_sa)) || true
      if [ "$old_test" = 0 ] && [ "$new_test" = 0 ]; then
        is_meta "$path" || impl_changed=$((impl_changed + 1)) || true
      fi
      ;;
    D)
      if [ "$old_test" = 1 ]; then
        deleted_tests="${deleted_tests}${cur_old}
"
        assert_rem=$((assert_rem + f_ar))
        test_evidence="${test_evidence}${cur_old}(-${f_ar} 断言,删除)
"
      else
        is_meta "$cur_old" || impl_changed=$((impl_changed + 1)) || true
      fi
      ;;
    A)
      if [ "$new_test" = 1 ]; then
        assert_add=$((assert_add + f_aa))
        supp_add=$((supp_add + f_sa))
        [ "$f_aa" -gt 0 ] && test_evidence="${test_evidence}${cur_new}(+${f_aa} 断言)
" || true
      else
        is_meta "$path" || impl_changed=$((impl_changed + 1)) || true
      fi
      ;;
    *)  # M（含 mode-only 变更）
      if [ "$new_test" = 1 ] || [ "$old_test" = 1 ]; then
        assert_add=$((assert_add + f_aa)); assert_rem=$((assert_rem + f_ar))
        supp_add=$((supp_add + f_sa))
        test_evidence="${test_evidence}${path}(+${f_aa}/-${f_ar} 断言)
"
        if [ "$f_r" -gt 0 ]; then
          test_mod_removed=$((test_mod_removed + 1))
          r4_suspects="${r4_suspects}${path}
"
        fi
      else
        is_meta "$path" || impl_changed=$((impl_changed + 1)) || true
      fi
      ;;
  esac
  reset_file
}

in_hunk=0
while IFS= read -r line || [ -n "$line" ]; do
  if [ "$in_hunk" = 0 ]; then
    case "$line" in
      "diff --git "*)  finalize_file ;;
      "deleted file mode "*) cur_status=D ;;
      "new file mode "*)    cur_status=A ;;
      "rename from "*)      cur_status=R; cur_old="${line#rename from }" ;;
      "rename to "*)        cur_new="${line#rename to }" ;;
      "--- a/"*)            cur_old="${line#--- a/}" ;;
      "--- \""*)
        raw="${line#--- }"
        unquote "$raw"; cur_old="$UNQ" ;;
      "--- /dev/null")      : ;;
      "+++ b/"*)            cur_new="${line#+++ b/}" ;;
      "+++ \""*)
        raw="${line#+++ }"
        unquote "$raw"; cur_new="$UNQ" ;;
      "+++ /dev/null")      : ;;
      "@@"*)                in_hunk=1 ;;
    esac
  else
    case "$line" in
      "diff --git "*) finalize_file; in_hunk=0 ;;
      "+"*)
        c="${line#+}"
        [[ "$c" =~ $TI_ASSERT_RE ]] && f_aa=$((f_aa + 1)) || true
        if [[ "$c" =~ $TI_SUPPRESS_RE ]]; then
          f_sa=$((f_sa + 1))
          supp_evidence="${supp_evidence}${cur_new:-${cur_old}}: +${c}
"
        fi
        ;;
      "-"*)
        c="${line#-}"; f_r=$((f_r + 1))
        [[ "$c" =~ $TI_ASSERT_RE ]] && f_ar=$((f_ar + 1)) || true
        ;;
      "\\"*) : ;;
      *) : ;;
    esac
  fi
done <"$DIFF_SRC"
finalize_file

NET=$((assert_add - assert_rem))

# ── 3. 计数入账（无论红绿，先落账——job log 即账本）──────────────────────
echo "TI-COUNT assertions_added=$assert_add assertions_removed=$assert_rem net=$NET"
echo "TI-COUNT suppression_markers_added=$supp_add"
echo "TI-COUNT test_files_deleted=$(count_lines "$deleted_tests")"
echo "TI-COUNT impl_files_changed=$impl_changed"
echo "TI-COUNT test_files_activity=$(count_lines "$test_evidence")"

write_summary() { # $1: verdict 行
  [ -n "${GITHUB_STEP_SUMMARY:-}" ] || return 0
  {
    echo "## test-integrity（ADR-0035 / P2-1）"
    echo ""
    echo "- 判定：$1"
    echo "- 断言：新增 $assert_add / 移除 $assert_rem / 净 $NET"
    echo "- 新增抑制标记：$supp_add"
    echo "- 删除的测试文件：${deleted_tests:-无}"
    echo "- 期望值改写嫌疑（TI-R4）：${r4_suspects:-无}"
    echo "- 实现文件变更数：$impl_changed"
  } >>"$GITHUB_STEP_SUMMARY" 2>/dev/null || true
}

# ── 4. 规则判定 ─────────────────────────────────────────────────────────
hits=""

if [ -n "$deleted_tests" ]; then
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    echo "::error file=${f%%（*}::TI-R1 测试文件被删除/移出测试路径（判据资产灭失——ADR-0035）"
  done <<<"$deleted_tests"
  tag TI-R1 "deleted: $(printf '%s ' "$deleted_tests")"
  hits="$hits TI-R1"
fi

if [ "$NET" -lt 0 ]; then
  echo "::error::TI-R2 断言计数净下降：+$assert_add/-$assert_rem（net=$NET）。测试文件账目：${test_evidence:-无}"
  tag TI-R2 "assertions +$assert_add/-$assert_rem net=$NET"
  hits="$hits TI-R2"
fi

if [ "$supp_add" -gt 0 ]; then
  while IFS= read -r e; do
    [ -n "$e" ] || continue
    echo "::error::TI-R3 新增抑制标记：$e"
  done <<<"$supp_evidence"
  tag TI-R3 "suppression_added=$supp_add"
  hits="$hits TI-R3"
fi

if [ "$test_mod_removed" -gt 0 ] && [ "$impl_changed" -eq 0 ]; then
  echo "::error::TI-R4 期望值改写嫌疑：测试文件存在删改行（$(printf '%s ' "$r4_suspects")）但零实现文件变更——只改判据不改实现（严格度 $TI_R4_SEVERITY，ADR-0035）"
  tag TI-R4 "rewrite_suspect: $(printf '%s ' "$r4_suspects") severity=$TI_R4_SEVERITY"
  hits="$hits TI-R4"
fi

if [ -z "$hits" ]; then
  tag TI-OK "无篡改形态（断言 net=$NET，抑制新增 $supp_add，删除测试文件 0）"
  write_summary "绿（TI-OK）——断言 net=$NET"
  exit 0
fi

# ── 5. 逃生门：ADR 引用式豁免（复用 adr-required 机制，计数入账）──────────
if [ "$TI_R4_SEVERITY" = "red" ] && [[ "$hits" == *TI-R4* ]]; then
  echo "::error::TI-R4 严格度=red（policy 声明），不可豁免。命中：$hits"
  write_summary "红——$hits（TI-R4=red 不可豁免）"
  exit 1
fi

refs=$(printf '%s\n%s\n' "${PR_TITLE:-}" "${PR_BODY:-}" \
  | grep -oE '\bADR-[0-9]{4}\b' | sort -u || true)
if [ -z "$refs" ]; then
  echo "::error::命中规则（$hits）且 PR 未引用任何 ADR-NNNN——豁免需 PR title/body 引用 scope 覆盖该路径的 ADR（ADR-0035 逃生门）"
  write_summary "红——$hits（无 ADR 引用）"
  exit 1
fi

if [ -n "${ADR_LISTING_FILE:-}" ]; then
  [ -r "$ADR_LISTING_FILE" ] || fail_closed "ADR_LISTING_FILE 不可读: $ADR_LISTING_FILE"
  listing=$(cat "$ADR_LISTING_FILE")
else
  [ -n "${GH_TOKEN:-}" ] || fail_closed "豁免需要 GH_TOKEN 拉 ADR 清单"
  : "${ADR_REGISTRY_API:=repos/Cloudbird-Software/agent-registry/contents/decisions}"
  listing=$(gh api "$ADR_REGISTRY_API" --paginate --jq '.[].name' 2>/dev/null) \
    || fail_closed "ADR 清单拉取失败（$ADR_REGISTRY_API）——豁免判定无法保证"
  [ -n "$listing" ] || fail_closed "ADR 清单为空（$ADR_REGISTRY_API）"
fi

missing=""
for ref in $refs; do
  num="${ref#ADR-}"
  printf '%s\n' "$listing" | grep -q "^ADR-${num}-" || missing="$missing $ref"
done
if [ -n "$missing" ]; then
  echo "::error::引用的${missing} 在 agent-registry/decisions 无对应文件（幽灵 ADR）——豁免拒绝"
  write_summary "红——$hits（幽灵 ADR:${missing}）"
  exit 1
fi

waived_n=$(printf '%s' "$hits" | wc -w | tr -d ' ')
echo "TI-COUNT escape_hatch_waived=$waived_n via $(printf '%s ' $refs)（入账）"
tag TI-ESC "waived:$hits via $refs——命中已豁免，计数入账"
write_summary "绿（经 ADR 豁免）——命中$hits 经 $(printf '%s ' $refs) 豁免并入账；断言 net=$NET"
exit 0
