#!/usr/bin/env bash
# ledger-sync.sh —— 计量账本同步到远端 ledger 分支（W2-C3 .github#216，ADR-0062）
#
# cost-check LLM 预算通道（AC-4）的数据源管道：把本 run 的 GATE_METERING_DIR
# 周片经 relink 续接到远端基链（本地与远端各自验链通过才合并——远端链已坏时
# 绝不覆盖，防 push 掩盖篡改），合并片再整链验证后经 contents API 写回。
# 周片文件名 records-<ISO 周>.jsonl 与 wrapper 落盘约定一致，远端按周追加。
#
# 用法:
#   bash pipeline/metering/ledger-sync.sh [--dir <GATE_METERING_DIR>] \
#        [--repo owner/name] [--branch metering-ledger] [--dry-run]
# env:
#   GH_TOKEN  必填——对 --repo 有 contents 写权限（同仓 GITHUB_TOKEN 或 org token）
#   GITHUB_REPOSITORY  --repo 缺省值（workflow 内即本仓）
# 退出码: 0=同步完成（或 dry-run 预演通过）| 2=参数/环境 | 3=账本/链无效 | 4=写回失败
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${METERING_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
fi
GH="${GH:-gh}"
DRY_RUN=0
LEDGER_DIR="${GATE_METERING_DIR:-.metering}"
REPO="${GITHUB_REPOSITORY:-}"
BRANCH="metering-ledger"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)     LEDGER_DIR="${2:?}"; shift 2 ;;
    --repo)    REPO="${2:?}"; shift 2 ;;
    --branch)  BRANCH="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "未知参数 $1（用法见文件头）" >&2; exit 2 ;;
  esac
done
[[ -n "$REPO" ]] || { echo "需要 --repo owner/name（或 GITHUB_REPOSITORY env）" >&2; exit 2; }
[[ -n "${GH_TOKEN:-}" ]] || { echo "GH_TOKEN 未设置（需对 $REPO 有 contents 写权限）" >&2; exit 2; }
[[ -d "$LEDGER_DIR" ]] || { echo "账本目录不存在：$LEDGER_DIR（wrapper 先跑出记录再同步）" >&2; exit 2; }
mutate() {  # dry-run 只报告不写（T2 注入式预演通道）
  if [[ "$DRY_RUN" == "1" ]]; then echo "DRY   (skip) $*"; else "$@"; fi
}

# 本地新记录条数（0 条=无事可做，幂等退出；glob 无匹配时 cat 失败→0）
LOCAL_N=$(cat "$LEDGER_DIR"/records-*.jsonl 2>/dev/null | wc -l | tr -d ' ' || true)
if [[ "$LOCAL_N" -eq 0 ]]; then
  echo "OK 本地账本无新记录——不同步（幂等）"
  exit 0
fi

# ---- 1. 确保 ledger 分支存在（404 才建：以 main 头为基点；已存在则不动） ----
if ! "$GH" api "repos/$REPO/git/ref/heads/$BRANCH" >/dev/null 2>&1; then
  MAIN_SHA=$("$GH" api "repos/$REPO/git/ref/heads/main" --jq .object.sha) \
    || { echo "FATAL: main 头 sha 拉取失败（$REPO）——无法建 $BRANCH" >&2; exit 2; }
  mutate "$GH" api -X POST "repos/$REPO/git/refs" \
    -f "ref=refs/heads/$BRANCH" -f "sha=$MAIN_SHA" >/dev/null \
    || { echo "FATAL: $BRANCH 分支创建失败（已存在？重试即可）" >&2; exit 4; }
fi

# ---- 2. 拉远端周片（逐片：本地有哪些周片就同步哪些） ----
TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT
PUSHED=0
for LOCAL in "$LEDGER_DIR"/records-*.jsonl; do
  [[ -e "$LOCAL" ]] || continue
  NAME=$(basename "$LOCAL")
  BASE="$TMPD/base-$NAME"; BASE_BLOB_SHA=""
  if "$GH" api "repos/$REPO/contents/$NAME?ref=$BRANCH" >"$TMPD/remote.json" 2>/dev/null; then
    BASE_BLOB_SHA=$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8"))["sha"])' "$TMPD/remote.json")
    "$PY" -c 'import base64,json,sys;d=json.load(open(sys.argv[1],encoding="utf-8"));open(sys.argv[2],"w",encoding="utf-8",newline="\n").write(base64.b64decode(d["content"]).decode("utf-8"))' "$TMPD/remote.json" "$BASE"
    REMOTE_N=$(wc -l <"$BASE" | tr -d ' ')
    LOCAL_M=$(wc -l <"$LOCAL" | tr -d ' ')
    if [[ "$REMOTE_N" -ge "$LOCAL_M" ]]; then
      echo "OK $NAME：远端 $REMOTE_N 条 ≥ 本地 $LOCAL_M 条——本片无需同步（幂等跳过）"
      continue
    fi
  fi
  # ---- 3. relink：本片记录续接到远端基链（双侧验链在 relink 内，任一坏即 exit 3） ----
  "$PY" "$DIR/metering.py" relink --base "$BASE" --local "$LOCAL" --out "$TMPD/merged-$NAME" >/dev/null
  # ---- 4. 合并片整链复验（fail-closed：写回前最后一道） ----
  "$PY" "$DIR/metering.py" verify --file "$TMPD/merged-$NAME" >/dev/null
  # ---- 5. contents API 写回（PUT 更新需 blob sha；409 冲突=并发同步，退出重试） ----
  B64="$("$PY" -c 'import base64,sys;print(base64.b64encode(open(sys.argv[1],"rb").read()).decode())' "$TMPD/merged-$NAME")"
  MERGED_N=$(wc -l <"$TMPD/merged-$NAME" | tr -d ' ')
  ARGS=(-X PUT "repos/$REPO/contents/$NAME" -f "branch=$BRANCH"
        -f "message=chore(metering): $NAME 追加至 $MERGED_N 条（W2-C3 ADR-0062，链验通过）"
        -f "content=$B64")
  [[ -n "$BASE_BLOB_SHA" ]] && ARGS+=(-f "sha=$BASE_BLOB_SHA")
  if mutate "$GH" api "${ARGS[@]}" >/dev/null; then
    PUSHED=$((PUSHED+1)); echo "OK $NAME → $BRANCH（$MERGED_N 条，链验通过）"
  else
    echo "FATAL: $NAME 写回失败（并发冲突？重跑本脚本即可续接）" >&2; exit 4
  fi
done
echo "同步完成：$PUSHED 片更新（本地待同步 $LOCAL_N 条）"
