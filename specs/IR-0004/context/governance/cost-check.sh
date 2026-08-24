#!/usr/bin/env bash
# cost-check.sh —— 额度/成本熔断（ADR-0040，P2-8 .github#93）
#
# 当月 Actions 分钟用量（GET /orgs/{org}/settings/billing/usage —— 旧 /settings/billing/actions
# 端点 2025 迁移后 410）vs governance/policy/automation-limits.yaml 声明预算：
#   ≥ warn_pct（80）  → 告警 issue（.github 仓，label cost-budget-warning，同日去重，不硬停）
#   ≥ hardstop_pct（100）→ org Actions 变量 AUTO_MERGE_DISABLED=true + 撤全部 open PR 的
#                        auto-merge + P0 issue（label cost-circuit-breaker）
# LLM token 通道（W2-C3 .github#216，ADR-0062）：data_source=ciw-metering 时按角色档归账
#   （CI-Workflows metering-ledger 分支 → metering.py aggregate，先验链后归账）；
#   链断/拉取失败 = INFRA fail-closed（exit 2），不静默归零不盲熔断。
# 熔断消费点：agent 派发/automerge 前置检查（AGENTS.md 行为契约）+ auto-fix-limit.sh
# 每轮机器执法撤 auto-merge。复位仅人工（owner PATCH/DELETE 变量 + P0 issue 留评论）；
# 本脚本观察到"变量已复位且用量 <100%"后自动关闭 P0 issue（复位留痕=issue 评论历史）。
#
# 用法: GH_TOKEN=<org admin token> bash cost-check.sh
# 注入（T2，不依赖真实超支）: COST_USAGE_MINUTES_OVERRIDE / COST_QUOTA_MINUTES_OVERRIDE /
#   COST_LLM_TOKENS_USED_OVERRIDE / COST_LLM_TOKENS_QUOTA_OVERRIDE / COST_DRY_RUN=1（只报告不写）
# 退出码: 0=未达阈值 | 1=触发告警/熔断（运行变红=可见信号）| 2=基础设施故障（fail-closed）
set -uo pipefail

ORG="${ORG:-Cloudbird-Software}"
DIR="$(cd "$(dirname "$0")" && pwd)"
GOV_REPO="$ORG/.github"
GH="${GH:-gh}"
DRY_RUN="${COST_DRY_RUN:-0}"
INFRA=0
TRIPPED=0
TODAY=$(date -u +%F)

ok()    { echo "OK    $1"; }
act()   { echo "ACT   $1"; }
infra() { echo "INFRA $1" >&2; INFRA=$((INFRA+1)); }

# ---------- AUDIT（ADR-0057，INV-12：宪法 §11 行 3 预算检查的审计条目） ----------
# 本脚本纳入管家唤醒矩阵（cron 6h→1h）。trigger 由 workflow 注入 COST_TRIGGER
# （${{ github.event_name }}：schedule/workflow_dispatch——"谁唤醒"）；头行=running，
# 尾行由 EXIT 陷阱按实际退出码落（0=ok 1=tripped 2=infra-fail）——多出口脚本无需
# 逐出口插行，判定逻辑零改动。duration 由 butler-audit.sh 的审计起点口径计算
# （source 时刻起算，等效脚本内 SECONDS）。
source "$DIR/butler-audit.sh" || { echo "FATAL: butler-audit.sh 加载失败" >&2; exit 2; }
audit_emit cost-check "${COST_TRIGGER:-local}" running '{"phase":"start"}' \
  || infra "AUDIT 头行输出失败（INV-12 完整性受损）"
cost_audit_final() {
  local rc=$1 oc=ok
  [[ "$rc" == "1" ]] && oc=tripped
  [[ "$rc" == "2" ]] && oc=infra-fail
  audit_emit cost-check "${COST_TRIGGER:-local}" "$oc" '{"phase":"done"}' || true
}
trap 'cost_audit_final "$?"' EXIT

[[ -n "${GH_TOKEN:-}" ]] || { echo "FATAL: GH_TOKEN 未设置（需 org admin token）" >&2; exit 2; }

# 数值校验（fail-closed，须在父 shell 调用——子 shell 里 infra 计数会丢失）：
# 非数值 → infra 通道 + 置 0，不参与判定（不 fail-open）
check_num() {  # <varname> <what>
  local __v="__dummy"
  eval "__v=\$${1:?}"
  if [[ "$__v" =~ ^[0-9]+([.][0-9]+)?$ ]]; then return 0; fi
  infra "非数值（$2）: '$__v'——判定输入无效"
  eval "$1=0"
}

# ---------- policy 读取（逐行 KEY=value，无 eval——C1 内容也不执行；tr 去 CR 兼容 Windows python） ----------
POLICY_ENV=$(python3 - "$DIR/policy/automation-limits.yaml" <<'PYEOF' | tr -d '\r'
import sys, yaml
try:
    c = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
    am, lt, cb = c["cost"]["actions_minutes"], c["cost"]["llm_tokens"], c["circuit_breaker"]
    rows = [("AM_QUOTA", am["quota_per_month"]), ("AM_WARN", am["warn_pct"]), ("AM_STOP", am["hardstop_pct"]),
            ("LT_QUOTA", lt["quota_per_month"]), ("LT_WARN", lt["warn_pct"]), ("LT_STOP", lt["hardstop_pct"]),
            ("LT_SOURCE", lt["data_source"]), ("CB_VARIABLE", cb["variable"]), ("CB_RESET_BY", cb["reset_by"])]
    # W2-C3（ADR-0062）：ciw-metering 数据源定位（data_source 非 ciw-metering 时可为空）
    m = lt.get("metering") or {}
    rows += [("LT_M_REPO", m.get("repo", "")), ("LT_M_BRANCH", m.get("branch", "")),
             ("LT_M_CODE", m.get("code_path", ""))]
    for kk, vv in rows:
        vv = str(vv)
        assert "=" not in vv and "\n" not in vv, f"policy 值含非法字符: {kk}"
        print(f"{kk}={vv}")
except Exception as e:
    sys.exit(f"policy 解析失败: {e}")
PYEOF
) || { echo "FATAL: policy/automation-limits.yaml 解析失败" >&2; exit 2; }
while IFS='=' read -r key val; do declare "$key=$val"; done <<< "$POLICY_ENV"
for v in AM_QUOTA AM_WARN AM_STOP LT_QUOTA LT_WARN LT_STOP LT_SOURCE CB_VARIABLE CB_RESET_BY; do
  [[ -n "${!v:-}" ]] || { echo "FATAL: policy 缺 $v" >&2; exit 2; }
done
# ciw-metering 数据源须完整声明定位三件套（W2-C3，ADR-0062）——缺一 = 配置面残缺，fail-closed
if [[ "$LT_SOURCE" == "ciw-metering" ]]; then
  for v in LT_M_REPO LT_M_BRANCH LT_M_CODE; do
    [[ -n "${!v:-}" ]] || { echo "FATAL: data_source=ciw-metering 但 policy llm_tokens.metering 缺 $v" >&2; exit 2; }
  done
fi
# 环境注入优先（T2 注入式测试通道）
AM_QUOTA="${COST_QUOTA_MINUTES_OVERRIDE:-$AM_QUOTA}"
LT_QUOTA="${COST_LLM_TOKENS_QUOTA_OVERRIDE:-$LT_QUOTA}"
check_num AM_QUOTA "Actions 月预算"; check_num AM_WARN "Actions 告警阈值"; check_num AM_STOP "Actions 硬停阈值"
check_num LT_QUOTA "LLM 月预算";     check_num LT_WARN "LLM 告警阈值";     check_num LT_STOP "LLM 硬停阈值"

mutate() {
  if [[ "$DRY_RUN" == "1" ]]; then echo "DRY   (skip) $*"; else "$@"; fi
}
label_ensure() {
  mutate "$GH" label create "$2" --repo "$1" \
    --description "cost-check 熔断标记（勿手工使用）" --color "$3" >/dev/null 2>&1 || true
}
gov_open_issues() {  # <label> → "number<TAB>title" 行
  "$GH" issue list --repo "$GOV_REPO" --state open --label "$1" --limit 100 \
    --json number,title --jq '.[] | "\(.number)\t\(.title)"' 2>/dev/null
}
# 当日已有评论/创建则跳过评论（防 4-6h cron 重复灌水）
issue_silent_today() {  # <number>
  local last
  last=$("$GH" issue view "$1" --repo "$GOV_REPO" --json createdAt,comments \
    --jq '[.comments[].createdAt, .createdAt] | max' 2>/dev/null) || return 1
  [[ "$last" == "$TODAY"* ]]
}
# pct/ge：python -c 经 sys.argv 取值，代码零内嵌引号——兼容 MSYS→Windows 原生 python 的参数传递
pct() {  # <used> <quota> → 百分比（1 位小数）
  python3 -c "import sys; print(round(float(sys.argv[1])*100/float(sys.argv[2]), 1))" "$1" "$2" | tr -d '\r'
}
ge() {  # <a> <b> → True/False
  python3 -c "import sys; print(float(sys.argv[1]) >= float(sys.argv[2]))" "$1" "$2" | tr -d '\r'
}

# ---------- 用量获取 ----------
YEAR=$(date -u +%Y); MONTH=$(date -u +%-m)
if [[ -n "${COST_USAGE_MINUTES_OVERRIDE:-}" ]]; then
  USED_MIN="$COST_USAGE_MINUTES_OVERRIDE"; SRC_MIN="注入"
else
  if ! USED_MIN=$("$GH" api "orgs/$ORG/settings/billing/usage?year=$YEAR&month=$MONTH" \
      --jq '[.usageItems[] | select(.product == "actions" and .unitType == "Minutes") | .quantity] | add // 0' 2>/dev/null); then
    infra "billing usage 拉取失败（orgs/$ORG/settings/billing/usage）——用量不可知，fail-closed 出口 2（不盲置熔断）"
  fi
  USED_MIN="${USED_MIN:-0}"; SRC_MIN="billing API"
fi
check_num USED_MIN "当月 Actions 分钟"
PCT_MIN=$(pct "$USED_MIN" "$AM_QUOTA")
check_num PCT_MIN "Actions 用量百分比"
ok "Actions 分钟（$YEAR-$MONTH）: $USED_MIN / $AM_QUOTA = ${PCT_MIN}%（$SRC_MIN）"

# ---------- LLM token 用量（W2-C3 .github#216，ADR-0062：ciw-metering 数据源按角色档归账） ----------
# @w2c3-llm-channel-begin（governance/tests/test-cost-llm-channel.sh 按标记对提取本函数体
# 离线单测——标记对缺失=测试红，防"测试测影子"）
llm_channel_account() {
  # → stdout 单行 "DATA<TAB>当月token<TAB>角色档json" | "ZERO<TAB>说明" | "INFRA<TAB>说明"。
  # 本函数经命令替换调用（子 shell），不直接调 infra/ok（计数会丢）——标签由调用方
  # 在父 shell 落账。env 注入通道（T2）：COST_LLM_METERING_DIR=本地账本目录、
  # COST_LLM_METERING_PY=归账引擎（metering.py）路径；缺省走真实数据源
  # （metering-ledger 分支 tarball → metering.py aggregate，先验链后归账）。
  local led="${COST_LLM_METERING_DIR:-}" mpy="${COST_LLM_METERING_PY:-}" out rc=0
  if [[ -z "$led" ]]; then
    led=$(mktemp -d) || { printf 'INFRA\tmetering 账本临时目录创建失败\n'; return 0; }
    if ! "$GH" api "repos/$LT_M_REPO/tarball/$LT_M_BRANCH" >"$led.tar.gz" 2>/dev/null; then
      if "$GH" api "repos/$LT_M_REPO/branches/$LT_M_BRANCH" >/dev/null 2>&1; then
        printf 'INFRA\tmetering 账本分支存在但 tarball 拉取失败（%s@%s）\n' "$LT_M_REPO" "$LT_M_BRANCH"
      else
        printf 'ZERO\t计量账本分支 %s@%s 未建（尚无经 wrapper 的 LLM 调用落账）——当月用量记 0\n' "$LT_M_REPO" "$LT_M_BRANCH"
      fi
      return 0
    fi
    # 记录位于 metering-ledger 分支根（ledger-sync.sh 经 contents API 写回，路径=文件名）；
    # 旧路径 pipeline/metering/ 下不会有 records——原 glob 必失败 INFRA（#258 根因）。
    # strip-components=1 剥除 tarball 顶层 <repo>-<sha>/ 后落到提取根 = 记录文件。
    if ! tar -xzf "$led.tar.gz" -C "$led" --strip-components=1 --wildcards \
         "*-records-*.jsonl" "records-*.jsonl" 2>/dev/null; then
      printf 'INFRA\tmetering 账本 tar 解包失败（strip-components=1 + records-*.jsonl）\n'
      return 0
    fi
    # 落盘后如有子目录（旧形态兼容），统一挪到提取根供 aggregate --dir 扫描。
    find "$led" -mindepth 2 -name "records-*.jsonl" -exec mv -t "$led" {} + 2>/dev/null || true
  fi
  if [[ -z "$mpy" || ! -f "$mpy" ]]; then
    printf 'INFRA\t归账引擎不可用（COST_LLM_METERING_PY=%s——cost-check.yml 须 sparse checkout %s 的 %s）\n' "$mpy" "$LT_M_REPO" "$LT_M_CODE"
    return 0
  fi
  out=$(python3 "$mpy" aggregate --dir "$led" --since "$(date -u +%Y-%m-01)" --json 2>&1) || rc=$?
  if [[ $rc -eq 2 ]]; then
    printf 'ZERO\t账本无周片（aggregate: %.200s）——当月用量记 0\n' "$out"
    return 0
  fi
  if [[ $rc -ne 0 ]]; then
    printf 'INFRA\tmetering 归账失败（账本验链不过——不可信数据不入账）：%.300s\n' "$out"
    return 0
  fi
  printf 'DATA\t%s\t%s\n' \
    "$(python3 -c 'import json,sys;print(json.load(sys.stdin)["totals"]["total_tokens"])' <<<"$out")" \
    "$(python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin)["roles"],ensure_ascii=False,sort_keys=True))' <<<"$out")"
}
# @w2c3-llm-channel-end

PCT_TOK=""
USED_TOK=""
LLM_ROLES=""
LLM_SUMMARY=""
if [[ -n "${COST_LLM_TOKENS_USED_OVERRIDE:-}" ]]; then
  PCT_TOK=$(pct "$COST_LLM_TOKENS_USED_OVERRIDE" "$LT_QUOTA")
  check_num PCT_TOK "LLM 用量百分比"
  USED_TOK="$COST_LLM_TOKENS_USED_OVERRIDE"; LLM_ROLES="注入通道"
  LLM_SUMMARY="LLM token: $USED_TOK / $LT_QUOTA = ${PCT_TOK}%（注入通道——优先于计量账本）"
  ok "$LLM_SUMMARY"
elif [[ "$LT_SOURCE" == "pending" ]]; then
  LLM_SUMMARY="LLM token: 数据源 pending（回滚形态，ADR-0040 决策 6）——仅声明，不参与告警"
  ok "$LLM_SUMMARY"
elif [[ "$LT_SOURCE" == "ciw-metering" ]]; then
  LLINE=$(llm_channel_account) || true
  IFS=$'\t' read -r LTAG LVAL LREST <<<"$LLINE"
  case "$LTAG" in
    DATA)
      USED_TOK="$LVAL"; LLM_ROLES="$LREST"
      check_num USED_TOK "LLM 当月 token（归账）"
      PCT_TOK=$(pct "$USED_TOK" "$LT_QUOTA")
      check_num PCT_TOK "LLM 用量百分比"
      LLM_SUMMARY="LLM token（当月归账）: $USED_TOK / $LT_QUOTA = ${PCT_TOK}%（角色档 ${LLM_ROLES:-∅}）"
      ok "$LLM_SUMMARY"
      ;;
    ZERO) LLM_SUMMARY="LLM token: $LVAL"; ok "$LLM_SUMMARY" ;;
    INFRA) infra "$LVAL" ;;
    *) infra "LLM 通道输出不可解析（期望 DATA/ZERO/INFRA 标签）：$LLINE" ;;
  esac
else
  infra "LLM token 数据源未知：$LT_SOURCE（policy cost.llm_tokens.data_source 无此形态）"
fi

# ---------- 熔断当前状态 ----------
BREAKER_SET=0
# 读取 rc 单独捕获（W2-C3 顺带修复的潜在缺陷）：变量存在且 value=false（人工复位后
# 的常态）曾被当作"读取失败非 404"落 INFRA——复位确认路径每小时误红。现按 rc 判：
# 0=读到值（true→置位，false/其他→未置位）；404=未建；其余=infra fail-closed。
VCB_RC=0
VERR=$("$GH" api "orgs/$ORG/actions/variables/$CB_VARIABLE" --jq .value 2>&1) || VCB_RC=$?
if [[ $VCB_RC -eq 0 ]]; then
  [[ "$VERR" == *"true"* ]] && BREAKER_SET=1   # 读到值：true=置位；false/其他=未置位（读取成功即终态）
elif grep -q "Not Found" <<<"$VERR"; then
  :
else
  infra "org 变量 $CB_VARIABLE 读取失败（非 404）——熔断状态未知"
fi

# ---------- tier 判定 ----------
STOP_MIN=$(ge "$PCT_MIN" "$AM_STOP")
WARN_MIN=$(ge "$PCT_MIN" "$AM_WARN")
STOP_TOK=False; WARN_TOK=False
if [[ -n "$PCT_TOK" ]]; then
  STOP_TOK=$(ge "$PCT_TOK" "$LT_STOP")
  WARN_TOK=$(ge "$PCT_TOK" "$LT_WARN")
fi

strip_all_automerge() {  # 硬停执法：撤全部受管仓 open PR 的 auto-merge（不分作者）
  local repos
  repos=$(python3 - "$DIR/REPOS.yaml" <<'PYEOF' | tr -d '\r'
import sys, yaml
try:
    repos = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["repos"]
    print(" ".join(r["name"] for r in repos if r.get("status") == "active"))
except Exception as e:
    sys.exit(f"REPOS.yaml 解析失败: {e}")
PYEOF
) || { infra "REPOS.yaml 解析失败——auto-merge 撤销清单不可得"; return; }
  local r n am
  for r in $repos; do
    while IFS=$'\t' read -r n am; do
      [[ "${am:-}" == "1" ]] || continue
      if mutate "$GH" api -X DELETE "repos/$ORG/$r/pulls/$n/auto-merge" >/dev/null 2>&1; then
        act "熔断执法: 撤销 $r#$n 的 auto-merge"
      fi
    done < <("$GH" pr list --repo "$ORG/$r" --state open --limit 200 \
      --json number,autoMergeRequest \
      --jq '.[] | [.number, (if .autoMergeRequest != null then "1" else "0" end)] | @tsv' 2>/dev/null)
  done
}

set_breaker() {  # PATCH 已有 / POST 新建（404 时）
  # 端点勘误（2026-08-21 deadman 演习实测）：创建 org 变量必须 POST 到集合端点
  # /orgs/{org}/actions/variables（不带变量名——带名 POST 是 404）；value 是字符串
  # 类型（-F 布尔会 422 "not of type string"）。此前两处错叠加致变量从未创建成功
  # （首次真实触发前从未执行过该路径——W1-C5 演习抓出）。
  if ! mutate "$GH" api -X PATCH "orgs/$ORG/actions/variables/$CB_VARIABLE" \
       -f name="$CB_VARIABLE" -f value=true >/dev/null 2>&1; then
    mutate "$GH" api -X POST "orgs/$ORG/actions/variables" \
      -f name="$CB_VARIABLE" -f value=true -f visibility=all >/dev/null 2>&1 \
      || infra "org 变量 $CB_VARIABLE 置位失败"
  fi
}

# ---------- 硬停档（任一指标 ≥100%） ----------
if [[ "$STOP_MIN" == "True" || "$STOP_TOK" == "True" ]]; then
  TRIPPED=1
  act "硬停档触发（Actions=${PCT_MIN}% LLM=${PCT_TOK:--}%）——置 $CB_VARIABLE + 撤 auto-merge + P0"
  set_breaker
  strip_all_automerge
  label_ensure "$GOV_REPO" cost-circuit-breaker b60205
  P0_EXISTING=$(gov_open_issues cost-circuit-breaker | grep -m1 "成本熔断" | cut -f1)
  P0_BODY="P0：额度/成本熔断已置位（ADR-0040，运行 $(date -u +%FT%TZ)）。

- Actions 分钟（$YEAR-$MONTH）: $USED_MIN / $AM_QUOTA = ${PCT_MIN}%（阈值 $AM_STOP%）${PCT_TOK:+
- ${LLM_SUMMARY:-LLM token: $USED_TOK}（阈值 $LT_STOP%，ADR-0062 归账通道）}
- 已执行：org 变量 \`$CB_VARIABLE\`=true；全部 open PR 的 auto-merge 已撤销。
- 效果：agent 派发与 automerge 前置检查将拒绝启动（AGENTS.md）；auto-fix-limit 每轮机器执法撤销新 enable。

处置（仅 $CB_RESET_BY，人工）：
1. 排查用量根因（失控循环查 auto-fix-limit 的 issue 历史）；
2. 复位：\`gh api -X PATCH orgs/$ORG/actions/variables/$CB_VARIABLE -f name=$CB_VARIABLE -f value=false\`（或 DELETE 该变量）；
3. 在本 issue 留复位评论（留痕）；cost-check 确认变量复位且用量 <${AM_STOP}% 后自动关闭本 issue。"
  if [[ -n "$P0_EXISTING" ]]; then
    if ! issue_silent_today "$P0_EXISTING"; then
      mutate "$GH" issue comment "$P0_EXISTING" --repo "$GOV_REPO" --body "$P0_BODY" >/dev/null 2>&1 || true
    fi
  else
    mutate "$GH" issue create --repo "$GOV_REPO" \
      --title "P0 成本熔断：Actions 分钟 ${PCT_MIN}% 达硬停档（$CB_VARIABLE 已置位）" \
      --body "$P0_BODY" --label cost-circuit-breaker >/dev/null 2>&1 \
      || infra "P0 issue 开立失败"
  fi
  # 告警档 issue（若开着）升级关闭
  for row in $(gov_open_issues cost-budget-warning | cut -f1); do
    mutate "$GH" issue close "$row" --repo "$GOV_REPO" --comment "用量已达硬停档，本告警升级为熔断 P0 issue。" >/dev/null 2>&1 || true
  done
# ---------- 告警档（≥80%，未达硬停） ----------
elif [[ "$WARN_MIN" == "True" || "$WARN_TOK" == "True" ]]; then
  TRIPPED=1
  label_ensure "$GOV_REPO" cost-budget-warning fbca04
  W_TITLE="成本告警（$YEAR-$MONTH）：Actions 分钟 ${PCT_MIN}% / LLM token ${PCT_TOK:--}%（阈值 ${AM_WARN}%）"
  W_EXISTING=$(gov_open_issues cost-budget-warning | grep -m1 "$YEAR-$MONTH" | cut -f1)
  W_BODY="额度告警（ADR-0040，$(date -u +%FT%TZ)）：Actions 分钟（$YEAR-$MONTH）$USED_MIN / $AM_QUOTA = ${PCT_MIN}%，达 ${AM_WARN}% 告警档——未硬停；达 ${AM_STOP}% 将置 \`$CB_VARIABLE\` 熔断并撤全部 auto-merge。
${LLM_SUMMARY:+$LLM_SUMMARY
}（LLM 阈值 ${LT_WARN}%/${LT_STOP}%，ADR-0062 归账通道）"
  if [[ -n "$W_EXISTING" ]]; then
    if ! issue_silent_today "$W_EXISTING"; then
      mutate "$GH" issue comment "$W_EXISTING" --repo "$GOV_REPO" --body "$W_BODY" >/dev/null 2>&1 || true
    fi
  else
    mutate "$GH" issue create --repo "$GOV_REPO" --title "$W_TITLE" \
      --body "$W_BODY" --label cost-budget-warning >/dev/null 2>&1 || infra "告警 issue 开立失败"
  fi
  act "告警档触发: Actions ${PCT_MIN}% / LLM token ${PCT_TOK:--}%（阈值 ${AM_WARN}%，issue 已开/更新）"
else
  # 用量回落：关闭过期的告警 issue（月度滚动或已回落）
  for row in $(gov_open_issues cost-budget-warning | cut -f1); do
    mutate "$GH" issue close "$row" --repo "$GOV_REPO" \
      --comment "用量回落（${PCT_MIN}% < ${AM_WARN}%）或月度滚动——自动关闭；再达阈值会重新开启。" >/dev/null 2>&1 || true
  done
  ok "未达告警档（${PCT_MIN}% < ${AM_WARN}%）"
fi

# ---------- 熔断持续/复位检测（复位仅人工——ADR-0040 决策 4） ----------
if [[ $BREAKER_SET -eq 1 && "$STOP_MIN" != "True" ]]; then
  # 熔断仍在但本轮用量未达硬停（月初滚动或注入回落）：保持置位，P0 issue 提醒（同日去重）
  P0_EXISTING=$(gov_open_issues cost-circuit-breaker | grep -m1 "成本熔断" | cut -f1)
  if [[ -n "$P0_EXISTING" ]] && ! issue_silent_today "$P0_EXISTING"; then
    mutate "$GH" issue comment "$P0_EXISTING" --repo "$GOV_REPO" --body \
      "熔断持续中（$CB_VARIABLE=true，本轮用量 ${PCT_MIN}%）。复位仅人工（$CB_RESET_BY）：PATCH 变量后在本 issue 留评论。" \
      >/dev/null 2>&1 || true
  fi
  act "熔断保持置位（用量 ${PCT_MIN}% < ${AM_STOP}%——不自动复位）"
elif [[ $BREAKER_SET -eq 0 ]]; then
  # 复位确认：变量未置位 + 用量 < 硬停档 + P0 issue 开着 → 自动关闭（人工复位已发生且留痕在评论）
  for row in $(gov_open_issues cost-circuit-breaker | cut -f1); do
    mutate "$GH" issue close "$row" --repo "$GOV_REPO" --comment \
      "复位确认：$CB_VARIABLE 已未置位且用量 ${PCT_MIN}% < ${AM_STOP}%——全流程恢复（agent 派发/automerge 前置检查放行）。自动关闭。" \
      >/dev/null 2>&1 || true
    act "复位确认，关闭 P0 issue #$row"
  done
fi

# ---------- 基础设施故障通道 ----------
if [[ $INFRA -gt 0 ]]; then
  label_ensure "$GOV_REPO" cost-infra 9a6700
  I_EXISTING=$(gov_open_issues cost-infra | grep -m1 "cost-check" | cut -f1)
  I_BODY="cost-check 出现基础设施故障（$INFRA 处，$(date -u +%FT%TZ)）——用量/熔断状态不可知。
fail-closed：本次运行 exit 2 变红；未盲置熔断（假熔断要求人工复位，会停摆整条流水线——ADR-0040 决策 5）。
agent 侧补盲（AGENTS.md）：派发前须确认无未决本 label issue。"
  if [[ -n "$I_EXISTING" ]]; then
    if ! issue_silent_today "$I_EXISTING"; then
      mutate "$GH" issue comment "$I_EXISTING" --repo "$GOV_REPO" --body "$I_BODY" >/dev/null 2>&1 || true
    fi
  else
    mutate "$GH" issue create --repo "$GOV_REPO" --title "cost-check 基础设施故障（用量不可知）" \
      --body "$I_BODY" --label cost-infra >/dev/null 2>&1 || true
  fi
  exit 2
fi

# 基础设施恢复通道（ADR-0040）：本轮零 INFRA 且存在未决 cost-check cost-infra issue
# → 自动关闭（与熔断复位确认对称——否则权限修复后告警单永久滞留，#201 实例）
if gov_open_issues cost-infra | grep -q "cost-check"; then
  for row in $(gov_open_issues cost-infra | grep "cost-check" | cut -f1); do
    mutate "$GH" issue close "$row" --repo "$GOV_REPO" --comment       "基础设施恢复确认：本轮零 INFRA（billing 用量与 org 变量读写全通，$(date -u +%FT%TZ)）——自动关闭（对称于熔断复位确认）。"       >/dev/null 2>&1 || true
    act "infra 恢复，关闭 issue #$row"
  done
fi

[[ $TRIPPED -eq 1 ]] && exit 1
exit 0

# retrigger（gate.yml 索引分支已在 main 修复，重新评估）
