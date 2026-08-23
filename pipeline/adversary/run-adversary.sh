#!/usr/bin/env bash
# run-adversary.sh —— 恶意合规 adversary CLI 入口（W4-C2 .github#221，ADR-0067；W3-C2 .github#278）
#
# 编排（各步皆 fail-closed）：配置锁校验 → prompt 组装 → LLM 调用 → 套件判定。
# LLM 调用唯一入口 = pipeline/metering/metering-wrapper.sh（ADR-0062：一次
# invoke 恰一条计量记录，judge-deep 档成本入 BUDGET-01）；--replay-file 离线
# 回放模式免凭据（自测/复现审计——攻击档案可重放）。
#
# 注意：adversary 产物是故意生成的不可信代码，judge 步会真实执行——只允许在
# 一次性 CI runner / 专用沙箱运行（勿在持有凭据的长活环境跑）。
#
# 用法:
#   bash pipeline/adversary/run-adversary.sh --target <dir> \
#        [--replay-file <resp.json>] [--report-out <report.json>]
# env:
#   LLM_API_KEY        真实调用必填（org secret，ADR-0048 落地件）；--replay-file 免
#   GATE_METERING_DIR  计量账本目录（缺省 ./.metering）
# 出:
#   stdout = 人类可读判定摘要；报告 JSON → --report-out（缺省 ./adversary-report.json）
# 退出码: 0=套件通过考验 | 1=套件不充分（blocking——实现 PR 阻塞，先补强套件）
#         | 2=配置/环境错误 | 3=adversary 空输出（恒绿防御 infra）/计量自检失败
#         | 4=provider 调用失败
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

# python 解释器解析：CI（ubuntu）python3 直用；本地 Windows（MSYS）python3 是
# 商店 stub → 探测失败回落 python。可用 METERING_PYTHON 强制指定（同 metering 约定）。
PY="${METERING_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
fi

TARGET="" REPLAY="" REPORT_OUT="$(pwd)/adversary-report.json"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)     TARGET="${2:?}"; shift 2 ;;
    --replay-file) REPLAY="${2:?}"; shift 2 ;;
    --report-out) REPORT_OUT="${2:?}"; shift 2 ;;
    *) echo "未知参数 $1（用法见文件头）" >&2; exit 2 ;;
  esac
done
[[ -n "$TARGET" ]] || { echo "需要 --target <dir>（含 spec.md + suite/ + run-suite.sh）" >&2; exit 2; }

# ---- 1) 配置锁校验 + 提取调用参数（AC-3：漂移即 exit 2，不进调用） ----
TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT
if ! "$PY" "$DIR/adversary.py" config >"$TMPD/lock.json" 2>"$TMPD/lock.err"; then
  cat "$TMPD/lock.err" >&2; exit 2
fi
"$PY" - "$TMPD/lock.json" >"$TMPD/lock.env" <<'PYEOF'
import json, shlex, sys
c = json.load(open(sys.argv[1], encoding="utf-8"))
s = c.get("sampling") or {}
for k, v in (("ADVMODEL", c.get("model")), ("ADVMAXTOK", s.get("max_tokens")),
             ("ADVTEMP", s.get("temperature")), ("ADVTOPP", s.get("top_p")),
             ("ADVSEED", s.get("seed")), ("ADVTHINK", s.get("thinking")),
             ("PROMPT_FILE", c.get("prompt_file"))):
    if v is not None:
        print(f"{k}={shlex.quote(str(v))}")
PYEOF
source "$TMPD/lock.env"
[[ -n "${ADVMODEL:-}" ]] || { echo "锁定配置缺 model（不应发生——load_lock 已校验）" >&2; exit 2; }
[[ -n "${PROMPT_FILE:-}" ]] || { echo "锁定配置缺 prompt_file（不应发生——load_lock 已校验）" >&2; exit 2; }

# ---- 2) prompt 组装（spec+套件+策略表） ----
"$PY" "$DIR/adversary.py" build-prompt --target "$TARGET" --out "$TMPD/user-prompt.md" >&2

# ---- 3) LLM 调用（唯一入口 = 计量 wrapper；回放模式免凭据） ----
if [[ -z "$REPLAY" && -z "${LLM_API_KEY:-}" ]]; then
  echo "LLM_API_KEY 未设置且未给 --replay-file（fail-closed：不静默降级出无意义判定）" >&2
  exit 2
fi
WRAP_ARGS=(--model "$ADVMODEL" --role adversary --tag malicious-compliance
           --prompt-file "$TMPD/user-prompt.md" --system-file "$DIR/$PROMPT_FILE"
           --max-tokens "${ADVMAXTOK:-4096}")
[[ -n "${ADVTEMP:-}" ]] && WRAP_ARGS+=(--temperature "$ADVTEMP")
[[ -n "${ADVTOPP:-}" ]] && WRAP_ARGS+=(--top-p "$ADVTOPP")
[[ -n "${ADVSEED:-}" ]] && WRAP_ARGS+=(--seed "$ADVSEED")
[[ -n "${ADVTHINK:-}" ]] && WRAP_ARGS+=(--thinking "$ADVTHINK")
[[ -n "$REPLAY" ]] && WRAP_ARGS+=(--replay-file "$REPLAY")
set +e
bash "$DIR/../metering/metering-wrapper.sh" "${WRAP_ARGS[@]}" >"$TMPD/content.txt" 2>"$TMPD/wrap.err"
WRAP_RC=$?
set -e
if [[ $WRAP_RC -ne 0 ]]; then
  cat "$TMPD/wrap.err" >&2
  echo "计量 wrapper 调用失败 rc=$WRAP_RC（4=provider 失败；3=计量自检 infra；2=环境）" >&2
  exit "$WRAP_RC"
fi

# ---- 4) 判定（逐尝试真实执行套件 + 钻洞归因 + 报告）；退出码透传 0/1/3 ----
set +e
"$PY" "$DIR/adversary.py" judge --target "$TARGET" --response "$TMPD/content.txt" \
  --report-out "$REPORT_OUT"
JUDGE_RC=$?
set -e
if [[ $JUDGE_RC -eq 0 || $JUDGE_RC -eq 1 || $JUDGE_RC -eq 3 ]]; then
  echo "报告 → $REPORT_OUT" >&2
fi
exit "$JUDGE_RC"
