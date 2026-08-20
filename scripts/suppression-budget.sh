#!/usr/bin/env bash
# suppression-budget.sh —— 抑制标记预算门（自动合并计划 P2-2，.github #87 / ADR-0036）
#
# 判定（fail-closed；两维度同时评估，逃生门优先）：
#   单 PR 维度：diff 中抑制标记净增量（新增 − 删除）> per_pr_max_net_add → 红
#   总量维度：  PR 合入树标记总量 > baselines[<repo>] → 红（棘轮：持平/下降绿）
#   逃生门：    PR title/body 引用的 ADR 文件（--adr-dir 下）内容含
#               escape_hatch.scope_marker 字样（scope 覆盖本门）→ 豁免绿 + 入账输出
#
# 参数源唯一：policy/suppressions.yaml（本仓 C1 路径——被审 PR 改不到审判自己的阈值）。
# 输出：stdout 机器行（SUPPRESSION_BUDGET / SUPPRESSION_MARKER / SUPPRESSION-EXEMPT /
#       SUPPRESSION-OK）+ 判定摘要；红因经 ::error:: 发 stderr 且两种红因显式区分。
# 退出码：0 绿（含豁免）；1 红（判定越界）；2 fail-closed（输入/政策/环境失效）。
#
# 依赖：bash 4+（关联数组）、python3+PyYAML（policy 解析）、GNU grep/awk/find。
# 自身约束：本文件不得出现任何已声明标记的字面形态（否则计入本仓总量）。
set -euo pipefail

SCRIPT_NAME=suppression-budget

die() { echo "::error::${SCRIPT_NAME}: $*" >&2; exit 2; }

usage() {
  cat >&2 <<'USAGE'
用法: suppression-budget.sh --repo <name> --diff <file> --tree <dir> --policy <file>
                            [--pr-title <str>] [--pr-body <file>] [--adr-dir <dir>]
  --repo       被审仓库名（policy baselines/count_exclude 的键，如 template-service）
  --diff       PR unified diff 文件（pulls API application/vnd.github.diff 的输出）
  --tree       PR 合入树根目录（post-merge 工作树——总量维度计数面）
  --policy     CI-Workflows policy/suppressions.yaml
  --pr-title   PR 标题（逃生门 ADR 引用提取，可省）
  --pr-body    PR 正文文件（同上，可省）
  --adr-dir    agent-registry decisions 本地检出目录（ADR 存在性 + scope 判定，可省）
USAGE
  exit 2
}

# ---------- 参数 ----------
REPO= DIFF= TREE= POLICY= PR_TITLE= PR_BODY_FILE= ADR_DIR=
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)     REPO=${2-}; shift 2 ;;
    --diff)     DIFF=${2-}; shift 2 ;;
    --tree)     TREE=${2-}; shift 2 ;;
    --policy)   POLICY=${2-}; shift 2 ;;
    --pr-title) PR_TITLE=${2-}; shift 2 ;;
    --pr-body)  PR_BODY_FILE=${2-}; shift 2 ;;
    --adr-dir)  ADR_DIR=${2-}; shift 2 ;;
    -h|--help)  usage ;;
    *)          echo "未知参数: $1" >&2; usage ;;
  esac
done
[ -n "$REPO" ]   && [ -n "$DIFF" ]   && [ -n "$TREE" ] && [ -n "$POLICY" ] || usage
[ -f "$DIFF" ]   || die "diff 文件不存在或不可读: $DIFF"
[ -d "$TREE" ]   || die "tree 目录不存在: $TREE"
[ -f "$POLICY" ] || die "policy 文件不存在或不可读: $POLICY"
[ -z "${PR_BODY_FILE:-}" ] || [ -f "$PR_BODY_FILE" ] || die "pr-body 文件不存在: $PR_BODY_FILE"

# ---------- policy 解析（python + PyYAML，严格 schema 校验，fail-closed）----------
# 解释器探测：优先 python3（Linux runner）；Windows 镜像 python3 可能是商店
# 占位 stub（存在但不可执行）——逐候选真跑 -c 探活，全不可用 → fail-closed
resolve_python() {
  local cand
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import sys" >/dev/null 2>&1; then
      echo "$cand"; return 0
    fi
  done
  return 1
}
PY_BIN=$(resolve_python) || die "无可用 python 解释器（python3/python 均不可用或为 stub）"
POLICY_ENV_FILE=$(mktemp)
declare -A MARKER_KIND MARKER_PATTERN MARKER_FILE BASELINE EXCLUDED
if ! "$PY_BIN" - "$POLICY" >"$POLICY_ENV_FILE" <<'PYEOF'; then
import shlex, sys

ERRS = []
def bad(msg): ERRS.append(msg)

try:
    import yaml
except Exception as e:
    print("POLICY_ERROR=" + shlex.quote("PyYAML 不可用: %r" % (e,)))
    sys.exit(0)

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
except Exception as e:
    print("POLICY_ERROR=" + shlex.quote("YAML 解析失败: %s" % e))
    sys.exit(0)

if not isinstance(data, dict):
    bad("顶层必须是映射")
    data = {}

if data.get("version") != 1:
    bad("version 必须为 1，得到 %r" % (data.get("version"),))

def need_int(d, k, lo=0):
    v = d.get(k)
    if not isinstance(v, int) or isinstance(v, bool) or v < lo:
        bad("%s 必须为 >=%d 的整数，得到 %r" % (k, lo, v))
        return None
    return v

PER_PR = need_int(data, "per_pr_max_net_add", 0)

eh = data.get("escape_hatch")
if not isinstance(eh, dict):
    bad("escape_hatch 必须为映射")
    eh = {}
for k in ("adr_repo", "adr_dir", "scope_marker"):
    v = eh.get(k)
    if not isinstance(v, str) or not v.strip():
        bad("escape_hatch.%s 必须为非空字符串，得到 %r" % (k, v))

markers = data.get("markers")
if not isinstance(markers, list) or not markers:
    bad("markers 必须为非空列表")
    markers = []
ids, mdata = [], []
for i, m in enumerate(markers):
    if not isinstance(m, dict):
        bad("markers[%d] 必须为映射" % i)
        continue
    mid = m.get("id")
    if not isinstance(mid, str) or not mid or not all(c.isalnum() or c == "-" for c in mid):
        bad("markers[%d].id 非法（须 [a-z0-9-] 形）: %r" % (i, mid))
        continue
    if mid in ids:
        bad("markers id 重复: %s" % mid)
        continue
    kind = m.get("kind")
    if kind == "regex":
        pat = m.get("pattern")
        if not isinstance(pat, str) or not pat.strip():
            bad("markers[%s]（regex）缺非空 pattern" % mid)
            continue
        ids.append(mid); mdata.append((mid, "regex", pat, None))
    elif kind == "file":
        fn = m.get("file")
        if not isinstance(fn, str) or not fn.strip() or "/" in fn:
            bad("markers[%s]（file）file 须为纯文件名（不含 /）" % mid)
            continue
        ids.append(mid); mdata.append((mid, "file", None, fn))
    else:
        bad("markers[%d].kind 须为 regex|file，得到 %r" % (i, kind))

bl = data.get("baselines")
if not isinstance(bl, dict) or not bl:
    bad("baselines 必须为非空映射（每仓总量基线）")
    bl = {}
BASES = {}
for k, v in bl.items():
    if not isinstance(k, str) or not k.strip():
        bad("baselines 键非法: %r" % (k,))
    elif not isinstance(v, int) or isinstance(v, bool) or v < 0:
        bad("baselines[%s] 必须为 >=0 的整数，得到 %r" % (k, v))
    else:
        BASES[k] = v

ce = data.get("count_exclude", {})
if not isinstance(ce, dict):
    bad("count_exclude 必须为映射")
    ce = {}
EXCS = {}
for k, v in ce.items():
    if not isinstance(k, str) or not k.strip() or not isinstance(v, list) \
            or not all(isinstance(x, str) and x.strip() for x in v):
        bad("count_exclude[%s] 必须为非空字符串列表" % (k,))
    else:
        EXCS[k] = v

if ERRS:
    print("POLICY_ERROR=" + shlex.quote("; ".join(ERRS)))
    sys.exit(0)

out = ["PER_PR_MAX=%d" % PER_PR,
       "ESCAPE_ADR_REPO=%s" % shlex.quote(eh["adr_repo"]),
       "ESCAPE_ADR_DIR=%s" % shlex.quote(eh["adr_dir"]),
       "ESCAPE_SCOPE_MARKER=%s" % shlex.quote(eh["scope_marker"]),
       "MARKER_IDS=%s" % shlex.quote(" ".join(ids))]
for mid, kind, pat, fn in mdata:
    if kind == "regex":
        out.append("MARKER_PATTERN[%s]=%s" % (shlex.quote(mid), shlex.quote(pat)))
    else:
        out.append("MARKER_FILE[%s]=%s" % (shlex.quote(mid), shlex.quote(fn)))
    out.append("MARKER_KIND[%s]=%s" % (shlex.quote(mid), kind))
for k in sorted(BASES):
    out.append("BASELINE[%s]=%d" % (shlex.quote(k), BASES[k]))
for k in sorted(EXCS):
    out.append("EXCLUDED[%s]=%s" % (shlex.quote(k), shlex.quote(" ".join(EXCS[k]))))
print("\n".join(out))
PYEOF
  die "policy 解析器执行失败（python3 异常退出）"
fi
# shellcheck disable=SC1090
source "$POLICY_ENV_FILE" || die "policy 环境文件 source 失败"
[ -n "${POLICY_ERROR:-}" ] && die "policy 无效: $POLICY_ERROR"

# ---------- 排除判定（仓库作用域：仅声明仓自己可排除，业务仓零排除项）----------
is_excluded() {  # $1 = 仓库相对路径
  [ -n "${EXCLUDED[$REPO]+x}" ] || return 1
  local p=${1#./} ex
  for ex in ${EXCLUDED[$REPO]}; do
    # shellcheck disable=SC2053  # glob 匹配为有意语义（与 [[ == ]] 一致）
    [[ $p == $ex ]] && return 0
  done
  return 1
}

# ---------- diff 维度：行 → 文件路径归因 ----------
# 输出形如 "<path>\t<+|-><content>"；`+++ b/x` / `--- a/x` 头行不进内容流
# （已知极限：内容行恰为 "+++ "/"--- " 前缀时与头行语法同形——git 自身同样有此歧义）
attrib_diff() {
  awk '
    /^diff --git / { next }
    /^\+\+\+ / { p = substr($0, 7); sub(/^b\//, "", p); if (p == "/dev/null") p = ""; next }
    /^--- /     { next }
    /^\+/       { if (p != "") print p "\t+" substr($0, 2) }
    /^-/        { if (p != "") print p "\t-" substr($0, 2) }
  ' "$1"
}

TMPD=$(mktemp -d)
ATTR_ADD="$TMPD/attr-add"   # 每行: <path>\t<content>（新增行，排除路径已滤）
ATTR_REM="$TMPD/attr-rem"
: >"$ATTR_ADD"; : >"$ATTR_REM"
attrib_diff "$DIFF" | while IFS=$'\t' read -r path rest; do
  [ -n "$path" ] || continue
  is_excluded "$path" && continue
  case ${rest:0:1} in
    +) printf '%s\t%s\n' "$path" "${rest:1}" >>"$ATTR_ADD" ;;
    -) printf '%s\t%s\n' "$path" "${rest:1}" >>"$ATTR_REM" ;;
  esac
done

# 文件类标记在属性流中的计数：路径以 /<file> 结尾（或根级同名），
# 且内容非空白、非 # 注释行
file_lines_in() {  # $1 = attr 流, $2 = 文件名
  awk -F'\t' -v f="$2" '
    BEGIN { e = f; gsub(/[.[\*^$()+?{}|]/, "\\\\&", e); re = "(^|.*/)" e "$" }
    $1 ~ re && $2 != "" && $2 !~ /^[[:space:]]*$/ && $2 !~ /^#/ { n++ }
    END { print n + 0 }
  ' "$1"
}

# ---------- 计数 ----------
declare -A ADDED_BY REMOVED_BY TREE_BY
ADDED_TOTAL=0; REMOVED_TOTAL=0; TREE_TOTAL=0

for id in $MARKER_IDS; do
  a=0; r=0
  if [ "${MARKER_KIND[$id]}" = regex ]; then
    a=$(cut -f2- "$ATTR_ADD" | grep -oE -- "${MARKER_PATTERN[$id]}" | wc -l | tr -d '[:space:]') || true
    r=$(cut -f2- "$ATTR_REM" | grep -oE -- "${MARKER_PATTERN[$id]}" | wc -l | tr -d '[:space:]') || true
  else
    a=$(file_lines_in "$ATTR_ADD" "${MARKER_FILE[$id]}")
    r=$(file_lines_in "$ATTR_REM" "${MARKER_FILE[$id]}")
  fi
  ADDED_BY[$id]=$a; REMOVED_BY[$id]=$r
  ADDED_TOTAL=$((ADDED_TOTAL + a)); REMOVED_TOTAL=$((REMOVED_TOTAL + r))
done

while IFS= read -r -d '' f; do
  p=${f#"$TREE"}
  p=${p#./}
  p=${p#/}   # TREE 以 / 结尾或 find 相对输出时剥离残留下前导斜杠
  case "$p" in .git/*) continue ;; esac
  is_excluded "$p" && continue
  for id in $MARKER_IDS; do
    n=0
    if [ "${MARKER_KIND[$id]}" = regex ]; then
      n=$(grep -IohE -- "${MARKER_PATTERN[$id]}" "$f" 2>/dev/null | wc -l | tr -d '[:space:]') || true
    else
      case "$f" in
        */${MARKER_FILE[$id]}|${MARKER_FILE[$id]})
          n=$(awk 'NF && $0 !~ /^[[:space:]]*#/ { c++ } END { print c + 0 }' "$f" 2>/dev/null) || true ;;
        *) continue ;;
      esac
    fi
    TREE_BY[$id]=$(( ${TREE_BY[$id]:-0} + n ))
    TREE_TOTAL=$((TREE_TOTAL + n))
  done
done < <(find "$TREE" -type d -name .git -prune -o -type f -print0)

NET=$((ADDED_TOTAL - REMOVED_TOTAL))

# ---------- 机器可读输出（判定前先落——红/fail-closed 时计数仍可审计）----------
echo "SUPPRESSION_BUDGET repo=$REPO net=$NET added=$ADDED_TOTAL removed=$REMOVED_TOTAL total=$TREE_TOTAL threshold=$PER_PR_MAX"
for id in $MARKER_IDS; do
  echo "SUPPRESSION_MARKER $id diff+${ADDED_BY[$id]} diff-${REMOVED_BY[$id]} tree=${TREE_BY[$id]:-0}"
done

# ---------- 逃生门：引用 scope 覆盖本门的 ADR → 豁免（绿）+ 入账 ----------
EXEMPT=0; EXEMPT_ADR=""
refs=$({ printf '%s\n' "${PR_TITLE:-}"
         if [ -n "${PR_BODY_FILE:-}" ]; then cat "$PR_BODY_FILE" 2>/dev/null || true; fi; } \
       | grep -oE '\bADR-[0-9]{4}\b' | sort -u || true)
if [ -n "$refs" ]; then
  if [ -n "${ADR_DIR:-}" ] && [ -d "$ADR_DIR" ]; then
    for ref in $refs; do
      num=${ref#ADR-}
      for f in $(ls "$ADR_DIR" 2>/dev/null | grep -E "^ADR-${num}-" || true); do
        if grep -qF -- "$ESCAPE_SCOPE_MARKER" "$ADR_DIR/$f" 2>/dev/null; then
          EXEMPT=1; EXEMPT_ADR="$ref"; break 2
        fi
      done
    done
  fi
fi

# ---------- 判定 ----------
REASONS=()
if [ "$NET" -gt "$PER_PR_MAX" ]; then
  REASONS+=("SUPPRESSION-BUDGET-PR: 单 PR 净增超阈值——净增 $NET > 阈值 $PER_PR_MAX（policy per_pr_max_net_add，ADR-0036）")
fi
if [ -z "${BASELINE[$REPO]+x}" ]; then
  die "仓库 $REPO 未在 policy baselines 声明基线——fail-closed（新仓接入须先盘点总量入册）"
fi
BASE=${BASELINE[$REPO]}
if [ "$TREE_TOTAL" -gt "$BASE" ]; then
  REASONS+=("SUPPRESSION-BUDGET-TOTAL: 总量上升——合入后 $REPO 标记总量 $TREE_TOTAL > 基线 $BASE（累计棘轮不得上升，ADR-0036）")
fi

if [ "$EXEMPT" -eq 1 ] && [ "${#REASONS[@]}" -gt 0 ]; then
  echo "SUPPRESSION-EXEMPT $EXEMPT_ADR 豁免生效（scope 标记 '$ESCAPE_SCOPE_MARKER' 覆盖）——入账：净增 $NET，合入总量 $TREE_TOTAL（基线 $BASE）；红因：${REASONS[*]}"
  if [ "$TREE_TOTAL" -gt "$BASE" ]; then
    echo "SUPPRESSION-EXEMPT-NEXT 棘轮同步义务：后续 PR 须将 baselines[$REPO] 上调至 $TREE_TOTAL，否则下一个 PR 即红——豁免不等于棘轮失效"
  fi
  exit 0
fi
if [ "${#REASONS[@]}" -eq 0 ]; then
  echo "SUPPRESSION-OK: 净增 $NET ≤ 阈值 $PER_PR_MAX，总量 $TREE_TOTAL ≤ 基线 $BASE——绿"
  exit 0
fi
for r in "${REASONS[@]}"; do
  echo "::error::$r" >&2
done
[ -n "$refs" ] && echo "NOTE: PR 引用了 ADR（$refs）但无 scope 覆盖本门（scope 标记 '$ESCAPE_SCOPE_MARKER'）——不构成豁免" >&2
exit 1
