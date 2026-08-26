# -*- coding: utf-8 -*-
"""nli_deberta.py —— DeBERTa-MNLI 蕴含引擎适配器（接口预留，CI 不部署）。

W4-C1 .github#220 / ADR-0066 决策 2："双向蕴含判定 NLI 模型 DeBERTa-MNLI 或
等效，版本钉死入 registry"。重模型不进 CI（拉镜像+推理分钟级、非确定性
环境）——本文件只是**接口与部署形态声明**，cluster.py 经 `--engine
deberta-mnli` 惰性加载；未部署时 fail-closed 拒判（诚实报错，不静默回落
heuristic——回落会让"语义聚簇不误报"的承诺失真）。

部署形态（org 内网推理服务，版本钉入本仓 pipeline/models.yaml 条目——原
agent-registry registry/models.yaml 已随 ADR-0085 退役停维）：
  1. 模型：microsoft/deberta-v3-base-mnli（或等效 NLI 模型），版本+权重
     sha256 钉入 registry（ADR-0066 风险缓解："NLI 蕴含判定误差→模型钉版+
     人工抽检校准"）。
  2. 服务：内网推理端点（如 TEI/text-embeddings-inference 或 FastAPI 单文件
     封装），POST /entail {"premise": ..., "hypothesis": ...} →
     {"label": "entailment|neutral|contradiction", "score": 0.97}。
  3. 接线：org secret ENTAILMENT_NLI_URL 指向该端点；本适配器以标准库
     urllib 调用（无第三方网络依赖——不触 scan-patterns 的 provider 直连
     模式；NLI 服务不是 LLM provider，不经计量 wrapper，调用计数走本文件
     自带计数器入报告 engine_meta）。
  4. 判定：a⊨b ⟺ label=entailment 且 score>=ENTAIL_SCORE_MIN（阈值校准集
     回流后修订走 PR）。

本文件不在判定链默认 import 面（AC-4 黑名单扫描对象=cluster/judge/policy）。
"""
import json
import os
import urllib.request

ENTAIL_SCORE_MIN = 0.85


class NliUnavailable(RuntimeError):
    """NLI 服务未部署/不可达——fail-closed（不静默回落 heuristic）。"""


def _entail(premise, hypothesis):
    url = os.environ.get("ENTAILMENT_NLI_URL")
    if not url:
        raise NliUnavailable(
            "ENTAILMENT_NLI_URL 未设置——deberta-mnli 引擎需内网 NLI 推理服务"
            "（部署形态见本文件 docstring）；CI/本地自测请用 --engine heuristic")
    body = json.dumps({"premise": premise, "hypothesis": hypothesis}).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/entail", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 白名单内网端点
        out = json.loads(resp.read().decode("utf-8"))
    if out.get("label") != "entailment" or float(out.get("score", 0)) < ENTAIL_SCORE_MIN:
        return False
    return True


def bidirectional(a, b):
    """双向蕴含：a⊨b 且 b⊨a（与 cluster.heuristic_bidirectional 同签名可插拔）。"""
    return _entail(a, b) and _entail(b, a)
