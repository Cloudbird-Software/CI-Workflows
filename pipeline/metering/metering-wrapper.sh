#!/usr/bin/env bash
# metering-wrapper.sh —— LLM 调用统一计量 wrapper 完整版（W2-C3 .github#216，ADR-0062）
#
# 一切 LLM 调用的唯一入口（INV-06/BEH-09/宪法 §4A；前身 scripts/llm-call.sh，
# ADR-0048 第一期形态）。直连 provider API（AR-3 第一期），每次 invoke 恰一条
# 聚合记录：流式分片（SSE）在 metering.py emit 内聚合——绝不按分片落记录
# （spike T8 教训：分片重复计数→成本虚增）。记录落 JSONL hash 链账本
# （GATE_METERING_DIR），过 record.schema.json 断言（fail-closed：无计量不算成功）。
#
# 用法:
#   bash pipeline/metering/metering-wrapper.sh --model <name> --prompt-file p.txt \
#        --role <role档> [--system-file s.txt] [--max-tokens N] [--temperature F] \
#        [--top-p F] [--seed N] [--thinking disabled|enabled] [--tag <t>] \
#        [--stream|--no-stream] [--invoke-id <id>] [--replay-file <f>] \
#        [--usage-compat-dir <dir>]
# env:
#   LLM_API_KEY      必填（org secret，直连 provider key——ADR-0048）；--replay-file 离线回放模式免
#   LLM_BASE_URL     可选——默认 https://open.bigmodel.cn/api/paas/v4（OpenAI 兼容，含版本段）
#   GATE_METERING_DIR 可选——JSONL 账本目录，默认 ./.metering
# 出:
#   stdout = 回复正文（流式=全分片聚合后整体输出）；stderr = 诊断
#   退出码 0=成功 | 2=参数/环境错误 | 3=计量自检失败 | 4=provider 调用失败
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

# python 解释器解析：CI（ubuntu）python3 直用；本地 Windows（MSYS）python3 是商店
# stub（不可执行脚本）→ 探测失败回落 python。可用 METERING_PYTHON 强制指定。
PY="${METERING_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
fi

MODEL="" PROMPT_FILE="" SYSTEM_FILE="" MAX_TOKENS="" TEMPERATURE="" TOP_P="" SEED=""
THINKING="" ROLE="" TAG="" INVOKE_ID="" REPLAY="" STREAM=0 COMPAT_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)           MODEL="${2:?}"; shift 2 ;;
    --prompt-file)     PROMPT_FILE="${2:?}"; shift 2 ;;
    --system-file)     SYSTEM_FILE="${2:?}"; shift 2 ;;
    --max-tokens)      MAX_TOKENS="${2:?}"; shift 2 ;;
    --temperature)     TEMPERATURE="${2:?}"; shift 2 ;;
    --top-p)           TOP_P="${2:?}"; shift 2 ;;
    --seed)            SEED="${2:?}"; shift 2 ;;
    --thinking)        THINKING="${2:?}"; shift 2 ;;
    --role)            ROLE="${2:?}"; shift 2 ;;
    --tag)             TAG="${2:?}"; shift 2 ;;
    --invoke-id)       INVOKE_ID="${2:?}"; shift 2 ;;
    --replay-file)     REPLAY="${2:?}"; shift 2 ;;
    --usage-compat-dir) COMPAT_DIR="${2:?}"; shift 2 ;;
    --stream)          STREAM=1; shift ;;
    --no-stream)       STREAM=0; shift ;;
    *) echo "未知参数 $1（用法见文件头）" >&2; exit 2 ;;
  esac
done
[[ -n "$MODEL" && -n "$ROLE" && -n "$PROMPT_FILE" && -f "$PROMPT_FILE" ]] \
  || { echo "需要 --model、--role（归账键）与存在的 --prompt-file" >&2; exit 2; }
[[ -n "$TAG" ]] || TAG="$ROLE"
if [[ -z "$REPLAY" ]]; then
  [[ -n "${LLM_API_KEY:-}" ]] \
    || { echo "LLM_API_KEY 未设置（org secret 缺失——ADR-0048 落地件）；离线测试用 --replay-file" >&2; exit 2; }
fi
METER_DIR="${GATE_METERING_DIR:-.metering}"
mkdir -p "$METER_DIR"

TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT

# ---- 请求体组装（python 单点：与计量记录同源，免 jq 依赖） ----
REQ_ARGS=(--model "$MODEL" --prompt-file "$PROMPT_FILE" --max-tokens "${MAX_TOKENS:-64}")
[[ -n "$SYSTEM_FILE" ]] && REQ_ARGS+=(--system-file "$SYSTEM_FILE")
[[ -n "$TEMPERATURE" ]] && REQ_ARGS+=(--temperature "$TEMPERATURE")
[[ -n "$TOP_P" ]] && REQ_ARGS+=(--top-p "$TOP_P")
[[ -n "$SEED" ]] && REQ_ARGS+=(--seed "$SEED")
[[ -n "$THINKING" ]] && REQ_ARGS+=(--thinking "$THINKING")
[[ $STREAM -eq 1 ]] && REQ_ARGS+=(--stream)
"$PY" "$DIR/metering.py" mkreq "${REQ_ARGS[@]}" >"$TMPD/req.json" || exit 2

# ---- 调用（真实 provider / 离线回放两形态；耗时只计调用段） ----
TS_START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HTTP_CODE="" CURL_RC=0
if [[ -n "$REPLAY" ]]; then
  # 离线回放（自测/重放审计）：响应文件直接作为 provider 应答，跳过网络
  cp "$REPLAY" "$TMPD/resp.out"
  HTTP_CODE=200
  T0=$(date +%s%3N); T1=$T0
else
  BASE_URL="${LLM_BASE_URL:-https://open.bigmodel.cn/api/paas/v4}"
  # 超时可调（LLM_TIMEOUT_S，默认 120s）：推理型模型（kimi-for-coding 等）16k
  # completion 实测 >120s——adversary 等长生成场景由调用方显式放宽，fail-closed 不变
  CURL_ARGS=(--max-time "${LLM_TIMEOUT_S:-120}" -sS -o "$TMPD/resp.out" -w '%{http_code}'
             -X POST "$BASE_URL/chat/completions"
             -H "Authorization: Bearer $LLM_API_KEY" -H "Content-Type: application/json"
             --data-binary "@$TMPD/req.json")
  [[ $STREAM -eq 1 ]] && CURL_ARGS=(-N --no-buffer "${CURL_ARGS[@]}")
  T0=$(date +%s%3N)
  HTTP_CODE=$(curl "${CURL_ARGS[@]}") || CURL_RC=$?
  T1=$(date +%s%3N)
fi
LATENCY=$((T1 - T0))

# invoke 终态判定（记录始终尝试落链——error 记录零用量但可审计）
RESP_ARG=""
if [[ $CURL_RC -ne 0 ]]; then
  EXIT_STATUS="error:transport"; HTTP_PAR=""
elif [[ ! "$HTTP_CODE" =~ ^2 ]]; then
  EXIT_STATUS="error:http_${HTTP_CODE:-none}"; HTTP_PAR="${HTTP_CODE:-}"
else
  EXIT_STATUS="ok"; HTTP_PAR="$HTTP_CODE"
fi
[[ -s "$TMPD/resp.out" ]] && RESP_ARG="$TMPD/resp.out"

# ---- 计量落链（一次 invoke 恰一条；流式聚合/schema 断言/hash 链/invoke_id 去重全在 emit） ----
# --content-out 恒传：聚合正文只在 emit 解析一次（唯一实现点），wrapper 只透传；
# prompt 经文件传递（多行文本不走 argv）
EMIT_ARGS=(--ledger "$METER_DIR" --model "$MODEL" --role "$ROLE" --prompt-file "$PROMPT_FILE"
           --ts-start "$TS_START" --latency-ms "$LATENCY"
           --exit-status "$EXIT_STATUS" --request-file "$TMPD/req.json"
           --content-out "$TMPD/content.txt"
           --max-tokens "${MAX_TOKENS:-64}")
[[ -n "$SYSTEM_FILE" ]] && EMIT_ARGS+=(--system-file "$SYSTEM_FILE")
[[ -n "$TEMPERATURE" ]] && EMIT_ARGS+=(--temperature "$TEMPERATURE")
[[ -n "$TOP_P" ]] && EMIT_ARGS+=(--top-p "$TOP_P")
[[ -n "$SEED" ]] && EMIT_ARGS+=(--seed "$SEED")
[[ -n "$THINKING" ]] && EMIT_ARGS+=(--thinking "$THINKING")
[[ $STREAM -eq 1 ]] && EMIT_ARGS+=(--stream)
[[ -n "$INVOKE_ID" ]] && EMIT_ARGS+=(--invoke-id "$INVOKE_ID")
[[ -n "$HTTP_PAR" ]] && EMIT_ARGS+=(--http-status "$HTTP_PAR")
[[ -n "$RESP_ARG" ]] && EMIT_ARGS+=(--resp-file "$RESP_ARG")
[[ -n "$COMPAT_DIR" ]] && EMIT_ARGS+=(--usage-compat-dir "$COMPAT_DIR")

if ! "$PY" "$DIR/metering.py" emit "${EMIT_ARGS[@]}" >"$TMPD/record.json" 2>"$TMPD/emit.err"; then
  cat "$TMPD/emit.err" >&2
  exit 3
fi
echo "metering → $(grep -o '"record_sha256":"[^"]*"' "$TMPD/record.json" | head -1 | cut -d'"' -f4) role=$ROLE exit=$EXIT_STATUS latency=${LATENCY}ms（账本 $METER_DIR）" >&2

if [[ "$EXIT_STATUS" != "ok" ]]; then
  head -c 300 "$TMPD/resp.out" 2>/dev/null >&2 || true
  echo "provider 调用失败（$EXIT_STATUS http=${HTTP_PAR:--}）" >&2
  exit 4
fi
cat "$TMPD/content.txt"
exit 0
