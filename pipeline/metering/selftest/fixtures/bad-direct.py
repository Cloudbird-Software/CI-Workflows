# bad-direct.py —— scan-direct-sdk 自测 fixture：故意包含直连形态（W2-C3 INV-06）
# 【勿仿】本文件是违例样本：期望 scan-direct-sdk.sh 命中全部 4 类模式。
import openai
from openai import OpenAI

client = OpenAI(api_key="fixture-not-a-real-key")
resp = requests.post("https://api.openai.com/v1/chat/completions", json={})
# curl -s https://open.bigmodel.cn/api/paas/v4/chat/completions
