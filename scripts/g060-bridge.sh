#!/usr/bin/env bash
# g060-bridge.sh —— g060 测试分片锁定跨仓调用桥（W2-C2 .github#274，ADR-0061）
#
# 当 .github 仓的 g060-guard.yml 因 cloudbrid-agent 无 workflows 权限无法推送时，
# 本脚本提供等价跨仓触发路径：由 CI-Workflows 的 workflow 经 repository_dispatch
# 或 workflow_dispatch 调用，拉取 .github 仓 scripts/g060-lock.sh 执行分片锁定。
#
# 拉取策略（降级链）：
#   1. GitHub REST API 取 main 分支文件内容（raw.githubusercontent.com 经 API）
#   2. 回退：git sparse-checkout pull（公开仓免凭据）
# 选 REST API 为主路径：git push/pull 在当前网络环境易超时，REST API 已验证可用。
#
# 职责：
#   1. 拉取 .github 仓的 scripts/g060-lock.sh
#   2. 收集当前仓/触发源的变更文件中命中 specs/*/suite/** 的列表
#   3. 调用 g060-lock.sh 执行身份判定；未授权 exit 2 → 本脚本 exit 2（阻断）
#
# 用法：
#   bash scripts/g060-bridge.sh \
#     --actor <actor-login> \
#     --github-repo Cloudbird-Software/.github \
#     --changed-files <f1> [<f2> ...] \
#     [--token $GH_TOKEN] \
#     [--workdir .g060-bridge]
#
# 退出码（与 g060-lock.sh 一致）：
#   0 = 已授权 | 1 = 无测试路径变更（跳过） | 2 = 未授权阻断（已开 issue）
set -euo pipefail

ACTOR=""
GH_REPO="Cloudbird-Software/.github"
TOKEN="${GITHUB_TOKEN:-}"
WORKDIR=".g060-bridge"
CHANGED_FILES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --actor)          ACTOR="${2:?}"; shift 2 ;;
    --github-repo)    GH_REPO="${2:?}"; shift 2 ;;
    --token)          TOKEN="${2:?}"; shift 2 ;;
    --workdir)        WORKDIR="${2:?}"; shift 2 ;;
    --changed-files)  shift; while [[ $# -gt 0 && "$1" != --* ]]; do CHANGED_FILES+=("$1"); shift; done ;;
    *) echo "未知参数 $1" >&2; exit 2 ;;
  esac
done

[[ -n "$ACTOR" ]] || { echo "::error::需要 --actor" >&2; exit 2; }

# 快速预筛：无测试路径变更则提前退出
touched=0
for f in "${CHANGED_FILES[@]}"; do
  [[ "$f" =~ ^specs/[^/]+/suite/ ]] && { touched=1; break; }
done
if [[ "$touched" -eq 0 ]]; then
  echo "g060-bridge: 无 specs/*/suite/** 变更，跳过"
  exit 1
fi

# 经 REST API 取 main 分支 g060-lock.sh 内容（base64 编码）
fetch_via_api() {
  local url="https://api.github.com/repos/${GH_REPO}/contents/scripts/g060-lock.sh?ref=main"
  local auth=()
  [[ -n "$TOKEN" ]] && auth=(-H "Authorization: Bearer ${TOKEN}")
  curl -s "${auth[@]}" -H "Accept: application/vnd.github.raw" \
    -o "$1" -w "%{http_code}" "$url" | grep -qE "^(200|302)$"
}

rm -rf "$WORKDIR"
mkdir -p "$WORKDIR/scripts"

if fetch_via_api "$WORKDIR/scripts/g060-lock.sh"; then
  echo "g060-bridge: 经 REST API 拉取 g060-lock.sh 成功"
else
  # 回退：git sparse-checkout（公开仓免凭据）
  echo "g060-bridge: REST API 拉取失败，回退 git sparse-checkout"
  cd "$WORKDIR"
  git init -q .
  git remote add origin "https://github.com/${GH_REPO}.git" 2>/dev/null || \
    git remote set-url origin "https://github.com/${GH_REPO}.git"
  git config core.sparseCheckout true
  printf 'scripts/g060-lock.sh\n' > .git/info/sparse-checkout
  git pull --depth=1 origin main >&2 || {
    echo "::error::g060-bridge: 无法拉取 ${GH_REPO} scripts/" >&2
    cd ..; rm -rf "$WORKDIR"; exit 2
  }
  cd ..
fi

LOCK_SCRIPT="$WORKDIR/scripts/g060-lock.sh"
[[ -f "$LOCK_SCRIPT" ]] || { echo "::error::g060-lock.sh 未在 ${GH_REPO} 中找到" >&2; exit 2; }

echo "g060-bridge: 执行锁定（actor=${ACTOR}, changed=${#CHANGED_FILES[@]}）"
set +e
bash "$LOCK_SCRIPT" \
  --actor "$ACTOR" \
  --token "$TOKEN" \
  --repo "$GH_REPO" \
  --changed-files "${CHANGED_FILES[@]}"
RC=$?
set -e

rm -rf "$WORKDIR"
exit "$RC"
