#!/usr/bin/env bash
# g060-bridge.sh —— CI-Workflows 跨仓桥接（ISSUE-263 W2-C2）
#
# 供 CI-Workflows 或被治理仓调用，对 Cloudbird-Software/.github 的
# specs/*/suite/** 变更执行 g060 锁定检查；非授权身份修改时在上游 .github 仓
# 开裁决 issue 路由 owner。
#
# 用法：
#   bash g060-bridge.sh --target-repo Cloudbird-Software/.github \
#                       --pr <number> --actor <actor> \
#                       [--owner <owner>] [--verifier <app-slug>]
#   环境变量：GH_TOKEN / GITHUB_TOKEN（须对 target-repo 有 issues:write）

set -euo pipefail

TARGET_REPO="${G060_TARGET_REPO:-Cloudbird-Software/.github}"
PR_NUMBER=""
ACTOR="${G060_ACTOR:-${GITHUB_ACTOR:-}}"
OWNER="${G060_OWNER:-randypanding}"
VERIFIER_SLUG="${G060_VERIFIER:-verifier-app}"
VERIFIER_ACTOR="${VERIFIER_SLUG}[bot]"
IR_CARD="Cloudbird-Software/.github#274"
LOCK_PATTERN='specs/*/suite/*'
SHOW_HELP=0

usage() {
  cat <<EOF
用法: $(basename "$0") [选项]
  --target-repo <owner/repo>  目标治理仓（默认：Cloudbird-Software/.github）
  --pr <number>               目标 PR 编号（必须）
  --actor <actor>             触发者 GitHub login（默认：\$GITHUB_ACTOR）
  --owner <owner>             人类 owner（默认：randypanding）
  --verifier <slug>           验证者 App slug（默认：verifier-app）
  -h, --help                  显示本帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-repo) TARGET_REPO="$2"; shift 2 ;;
    --pr) PR_NUMBER="$2"; shift 2 ;;
    --actor) ACTOR="$2"; shift 2 ;;
    --owner) OWNER="$2"; shift 2 ;;
    --verifier) VERIFIER_SLUG="$2"; VERIFIER_ACTOR="${VERIFIER_SLUG}[bot]"; shift 2 ;;
    -h|--help) SHOW_HELP=1; shift ;;
    *) echo "错误：未知参数 $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ $SHOW_HELP -eq 1 ]] && { usage; exit 0; }

if [[ -z "$PR_NUMBER" ]]; then
  echo "错误：--pr 为必填项" >&2
  usage >&2
  exit 1
fi
if [[ -z "$ACTOR" ]]; then
  echo "错误：--actor 或 \$GITHUB_ACTOR 未设置" >&2
  exit 1
fi

is_authorized() {
  local actor="$1"
  [[ "$actor" == "$OWNER" || "$actor" == "$VERIFIER_ACTOR" ]]
}

# ---------- 拉取目标 PR 的变更文件 ----------
mapfile -t FILES < <(gh pr view "$PR_NUMBER" --repo "$TARGET_REPO" \
  --json files --jq '.files[].path' 2>/dev/null || true)

LOCKED_FILES=()
for f in "${FILES[@]}"; do
  [[ -z "$f" ]] && continue
  case "$f" in
    specs/*/suite/*) LOCKED_FILES+=("$f") ;;
  esac
done

if [[ ${#LOCKED_FILES[@]} -eq 0 ]]; then
  echo "g060-bridge: 目标 PR #${PR_NUMBER} 未涉及 ${TARGET_REPO} 的 specs/*/suite/**"
  exit 0
fi

if is_authorized "$ACTOR"; then
  echo "g060-bridge: 触发者 $ACTOR 为授权身份，放行："
  printf '  - %s\n' "${LOCKED_FILES[@]}"
  exit 0
fi

echo "::error::g060-bridge: 触发者 $ACTOR 非授权身份，修改了 ${TARGET_REPO} 的锁定路径："
printf '  - %s\n' "${LOCKED_FILES[@]}" >&2

PR_URL="https://github.com/${TARGET_REPO}/pull/${PR_NUMBER}"

ISSUE_BODY=$(cat <<EOF
> 由 CI-Workflows g060-bridge.sh 自动生成 | ADR-0061 g060 扩展 | 卡：${IR_CARD}

触发者 \`$ACTOR\` 在 ${TARGET_REPO} 的 PR #${PR_NUMBER} 中修改了以下按 IR 分片的锁定测试路径：
$(printf '%s\n' "${LOCKED_FILES[@]}" | sed 's/^/- `/; s/$/`/')

关联 PR：${PR_URL}

## 请 owner 裁决
- \`/g060-adopt <证据引用>\`：采纳变更（终态机器可核）。
- \`/g060-reject <证据引用>\`：驳回变更（终态机器可核）。
- 无裁决且超过 TTL（72h）将触发 dead-man 提醒。
EOF
)

ISSUE_TITLE="g060 blocked: unauthorized change to specs/*/suite/** by ${ACTOR}"

if gh issue create --repo "$TARGET_REPO" --title "$ISSUE_TITLE" --body "$ISSUE_BODY" \
   --assignee "$OWNER" --label state:needs-human >/dev/null 2>&1; then
  echo "::error::已在 ${TARGET_REPO} 创建裁决 issue 并 assign 给 $OWNER"
else
  if gh issue create --repo "$TARGET_REPO" --title "$ISSUE_TITLE" --body "$ISSUE_BODY" \
     --assignee "$OWNER" >/dev/null 2>&1; then
    echo "::warning::已在 ${TARGET_REPO} 创建裁决 issue（无 state:needs-human 标签）" >&2
  else
    echo "::error::g060-bridge: 在 ${TARGET_REPO} 创建裁决 issue 失败" >&2
  fi
fi

exit 2
