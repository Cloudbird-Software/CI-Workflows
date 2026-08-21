# bad-with-allow.py —— scan-direct-sdk 自测 fixture：行内豁免标记形态
# 命中模式但带 metering-allow 标记 → 放行且留痕（ALLOW 行，可审计的人为放行）。
import openai  # metering-allow: openai-sdk-import
