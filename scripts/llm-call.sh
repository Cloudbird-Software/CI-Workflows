#!/usr/bin/env bash
# llm-call.sh —— LLM 调用统一计量 wrapper（IR-0001 W0-C1，ADR-0048）
#
# 一切 LLM 调用的唯一入口（INV-06/BEH-09）：每次调用落盘一条 usage 记录
# （model/prompt 版本哈希/seed/采样参数/用量/时延/HTTP 状态），记录过
# scripts/llm-usage.schema.json 的结构自检——自检不过 = 调用失败
# （fail-closed：无计量不算成功）。
#
# 用法:
#   bash scripts/llm-call.sh --model <name> --prompt-file p.txt \
#        [--system-file s.txt] [--max-tokens N] [--temperature F] \
#        [--seed N] [--tag <stage-tag>]
# env:
#   LLM_API_KEY    必填——org secret（直连 provider key，ADR-0048）
#   LLM_BASE_URL   可选——默认 https://open.bigmodel.cn/api/paas/v4
#                  （OpenAI 兼容；须含到版本段，不含 /chat/completions）
#   LLM_USAGE_DIR  可选——usage 记录落盘目录，默认 ./llm-usage
# 出:
#   stdout = 回复正文（纯文本）——调用方只消费 stdout
#   stderr = 诊断信息
#   退出码 0=成功；2=参数/环境错误；3=计量自检失败；4=provider 调用失败
set -euo pipefail

MODEL="" PROMPT_FILE="" SYSTEM_FILE="" MAX_TOKENS="" TEMPERATURE="" SEED="" TAG="untagged"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)        MODEL="${2:?}"; shift 2 ;;
    --prompt-file)  PROMPT_FILE="${2:?}"; shift 2 ;;
    --system-file)  SYSTEM_FILE="${2:?}"; shift 2 ;;
    --max-tokens)   MAX_TOKENS="${2:?}"; shift 2 ;;
    --temperature)  TEMPERATURE="${2:?}"; shift 2 ;;
    --seed)         SEED="${2:?}"; shift 2 ;;
    --tag)          TAG="${2:?}"; shift 2 ;;
    *) echo "未知参数 $1（用法见文件头）" >&2; exit 2 ;;
  esac
done
[[ -n "$MODEL" && -n "$PROMPT_FILE" && -f "$PROMPT_FILE" ]] \
  || { echo "需要 --model 与存在的 --prompt-file" >&2; exit 2; }
[[ -n "${LLM_API_KEY:-}" ]] \
  || { echo "LLM_API_KEY 未设置（org secret 缺失——ADR-0048 落地件，owner 于 org Actions secrets 设置）" >&2; exit 2; }

BASE_URL="${LLM_BASE_URL:-https://open.bigmodel.cn/api/paas/v4}"
USAGE_DIR="${LLM_USAGE_DIR:-./llm-usage}"
mkdir -p "$USAGE_DIR"

PROMPT=$(cat "$PROMPT_FILE")
SYSTEM=$([[ -n "$SYSTEM_FILE" ]] && cat "$SYSTEM_FILE" || true)

# 请求体（seed 仅在显式给出时携带——provider 对未支持参数会整体拒绝；
# jq 程序保持单行——部分 jq 构建（Windows 原生 exe）不接受多行程序串）
REQ=$(jq -nc --arg m "$MODEL" --arg p "$PROMPT" --arg s "$SYSTEM" \
      --argjson mt "${MAX_TOKENS:-null}" --argjson tp "${TEMPERATURE:-null}" --argjson sd "${SEED:-null}" \
      '{model:$m, max_tokens:$mt, temperature:$tp, seed:$sd, messages:((if $s != "" then [{role:"system",content:$s}] else [] end) + [{role:"user",content:$p}])} | with_entries(select(.value != null))')

REQ_SHA=$(printf '%s' "$REQ" | sha256sum | cut -d' ' -f1)
PROMPT_VER=$(printf '%s\n%s' "$PROMPT" "$SYSTEM" | sha256sum | cut -d' ' -f1)

TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT
T0=$(date +%s%3N)
HTTP_CODE=$(curl -sS -o "$TMPD/resp.json" -w '%{http_code}' --max-time 120 \
  -X POST "$BASE_URL/chat/completions" \
  -H "Authorization: Bearer $LLM_API_KEY" -H "Content-Type: application/json" \
  -d "$REQ") || { echo "provider 请求传输失败（$BASE_URL 不可达？ASSUMPTION-01 监控：llm-connectivity workflow）" >&2; exit 4; }
T1=$(date +%s%3N)
LATENCY=$((T1 - T0))

if [[ ! "$HTTP_CODE" =~ ^2 ]]; then
  echo "provider HTTP $HTTP_CODE：$(head -c 300 "$TMPD/resp.json" 2>/dev/null)" >&2
  exit 4
fi

CONTENT=$(jq -r '.choices[0].message.content // empty' "$TMPD/resp.json")
[[ -n "$CONTENT" ]] || { echo "响应缺 choices[0].message.content：$(head -c 300 "$TMPD/resp.json")" >&2; exit 4; }

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
REC=$(jq -nc \
  --arg schema llm-usage/v1 --arg ts "$TS" --arg tag "$TAG" --arg model "$MODEL" \
  --arg pv "sha256:$PROMPT_VER" --argjson sd "${SEED:-null}" \
  --argjson mt "${MAX_TOKENS:-null}" --argjson tp "${TEMPERATURE:-null}" \
  --argjson pt "$(jq '.usage.prompt_tokens // 0' "$TMPD/resp.json")" \
  --argjson ct "$(jq '.usage.completion_tokens // 0' "$TMPD/resp.json")" \
  --argjson tt "$(jq '.usage.total_tokens // 0' "$TMPD/resp.json")" \
  --argjson latency "$LATENCY" --argjson http "$HTTP_CODE" \
  --arg req "sha256:$REQ_SHA" \
  --arg resp "sha256:$(sha256sum "$TMPD/resp.json" | cut -d' ' -f1)" \
  --argjson pb "$(printf '%s' "$PROMPT" | wc -c | tr -d ' ')" \
  '{schema:$schema, ts:$ts, tag:$tag, model:$model, prompt_version:$pv, prompt_bytes:$pb, seed:$sd, sampling:{max_tokens:$mt, temperature:$tp}, usage:{prompt_tokens:$pt, completion_tokens:$ct, total_tokens:$tt}, latency_ms:$latency, http_status:$http, request_sha256:$req, response_sha256:$resp}')

# 计量自检（fail-closed，schema 必填字段的机内镜像——完整 schema 校验由
# llm-usage.schema.json 定义、下游关卡消费）：任一必填为空/类型不符即失败，
# 记录仍落盘供诊断（后缀 .invalid）
invalid=0
jq -e '.schema=="llm-usage/v1" and (.ts|type=="string" and length>=20) and (.tag|type=="string" and length>0) and (.model|type=="string" and length>0) and (.prompt_version|startswith("sha256:")) and ((.seed==null) or (.seed|type=="number")) and (.sampling.max_tokens!=null) and (.usage.prompt_tokens|type=="number") and (.usage.completion_tokens|type=="number") and (.usage.total_tokens|type=="number") and ((.usage.prompt_tokens+.usage.completion_tokens)>0) and (.latency_ms|type=="number" and .>=0) and (.http_status|type=="number" and .>=200 and .<300) and (.request_sha256|startswith("sha256:")) and (.response_sha256|startswith("sha256:"))' <<<"$REC" >/dev/null 2>&1 || invalid=1

SAFE_TAG=$(printf '%s' "$TAG" | tr -c 'a-zA-Z0-9._-' '_')
OUT="$USAGE_DIR/usage-$TS-$SAFE_TAG.json"
if [[ $invalid -eq 1 ]]; then
  printf '%s\n' "$REC" > "${OUT%.json}.invalid.json"
  echo "计量自检失败（usage 记录缺必填字段）——本次调用按失败处理（fail-closed）" >&2
  exit 3
fi
printf '%s\n' "$REC" >"$OUT"
echo "usage → $OUT（tokens: $(jq -c .usage <<<"$REC")）" >&2
printf '%s' "$CONTENT"
