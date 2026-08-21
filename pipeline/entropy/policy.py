# -*- coding: utf-8 -*-
"""policy.py —— 语义熵分歧度量策略常量（W4-C1 .github#220，ADR-0066）。

判定链路（cluster.py/judge.py）唯一的配置源：本模块零 LLM/零网络/零第三方依赖
（AC-4 静态可证——tests/test_static_zero_llm.py 扫 import 面）。阈值改动=C1 变更
（引 ADR）。质询轮次硬上限：ADR-0066 决策 5 记"默认 3 轮"，本实现钉更保守的
2 轮为 policy 默认（成本更低，可按 ADR 上调——非机制偏离，PR body 已披露）。
"""
import os

# ---- k=5 跨族冷上下文独立重派生（ADR-0066 决策 1）----
K = 5

# 5 路派生者族标记（AC-4：k 路输入含族标记且各族 >=1 路——5 族 x 1 路）。
FAMILIES = ["glm", "qwen", "llama", "mistral", "gemma"]

# 跨族 live 路由表：family -> (model, base_url 环境变量)。一期 provider 直连
# 形态（ADR-0048）仅 glm 族可达（open.bigmodel.cn）；其余族路由 model=None =
# 未配置——live 模式下缺任一族路由即 fail-closed 拒跑（跨族是前置条件而非
# 装饰：单族 5 路=伪跨族，会退化红队语义。回放模式不受限）。
# 多 provider 接入 = 在此表补路由 + 配 <FAMILY>_BASE_URL org secret（ADR-0066
# 后果：派生者档案在 registry 可替换——本表即接线点）。
FAMILY_ROUTES = {
    "glm":     ("glm-4.5-air", "LLM_BASE_URL"),
    "qwen":    (None, "QWEN_BASE_URL"),
    "llama":   (None, "LLAMA_BASE_URL"),
    "mistral": (None, "MISTRAL_BASE_URL"),
    "gemma":   (None, "GEMMA_BASE_URL"),
}

# ---- 底噪扣减（ADR-0066 决策 4，AC-2）----
# 同族模型同提示重采样 m 次测自分歧簇数 B；仅 跨族簇数 - B >= NOISE_MARGIN
# 才归因 spec 歧义（把模型固有噪声误报成 spec 问题会摧毁红队公信力）。
NOISE_RESAMPLE_M = 3
NOISE_FAMILY = "glm"           # 底噪测量族（与主派生同源可比）
NOISE_MARGIN = 2               # 扣减后净分歧簇数阈值（卡面 AC-2：>= 2 才报）

# ---- 交叉质询（ADR-0066 决策 5，AC-3）----
CROSS_EXAM_MAX_ROUNDS = 2      # 轮次硬上限（policy 常量；见模块 docstring 披露）

# ---- 启发式蕴含引擎参数（cluster.py，零依赖形态）----
# 双向 set-containment 阈值（token 集=拉丁词 + CJK uni/bigram 并集）。0.5 由
# fixture 集校准：同读法改写（含语序调整）≥0.5（直连或传递闭包并入同簇），
# 不同读法 ≤0.2（fixtures 实测最大 0.19）。校准集回流修订走 PR（C1）。
HEURISTIC_THETA = 0.5

# ---- 派生提示（冷上下文：不带任何先前实现残留，ADR-0066 决策 1）----
DERIVE_ROLE = "entropy-derive"     # metering wrapper 归账键（BEH-09）
NOISE_ROLE = "entropy-noise"
EXAMINE_ROLE = "entropy-examine"


def live_routes_ready():
    """live 路由完备性检查：返回 (ready, 缺失族清单)。fail-closed 前置。"""
    missing = [f for f, (m, _) in FAMILY_ROUTES.items() if m is None]
    return (not missing), missing


def route_base_url(family):
    """族路由 base_url（env 未设 → None，调用方回退 wrapper 默认 provider）。"""
    _, env = FAMILY_ROUTES[family]
    return os.environ.get(env) or None
