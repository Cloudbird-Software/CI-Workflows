# ok-via-wrapper.sh —— scan-direct-sdk 自测 fixture：合规形态（零命中）
# 一切 LLM 调用经计量 wrapper（ADR-0062），provider 域名不出现在本仓代码。
reply=$(bash pipeline/metering/metering-wrapper.sh \
  --model "$MODEL" --prompt-file prompt.txt --role demo --max-tokens 64)
printf '%s' "$reply" > out.txt
