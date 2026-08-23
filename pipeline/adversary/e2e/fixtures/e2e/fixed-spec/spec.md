# [修复后 spec] 红队守门端到端 — 已修复 spec（用于触发 survived）

> 本 spec 为 W5-C2 端到端实跑测试 fixture 的修复版本。
> 修复内容回应 Veto 理由（`deliberately-bad-spec/adversary-scores.json` 中的逐 criterion 理由）：
>   - 补全 AC criteria 溯源（AC-1/AC-2 → 具体 criterion + 阈值 + 连续分来源）
>   - 还原 IR 保真度（#263 blastRadius/AC 机器可查映射）
>   - 补全反摆拍断言（golden 回归 / 配置面校验 / 凭据形状扫描）
>   - 声明受保护路径集（specs/** + pipeline/adversary/**）
>   - 强化套件：测试真实验证 AC 语义

specVersion: 100
taskId: ISSUE-263-W5C2-E2E
title: W5-C2 E2E fixture — fixed spec (survives red team)

## 验收标准（已补全溯源）
- AC-1 (criteria-derification): lock-pin / ac-traceability / ir-fidelity 三 criterion，
  阈值 0.7，连续分来源=llm-verifier 实际调用记录 + 逐 criterion JSON + token 消耗。
- AC-2 (evidence-check): 每条引用由代码对运行时刻真实工件做字符串级机械匹配，
  基准版本（SHA/抓取时间）在 run 开始时动态获取并写入报告。

## blastRadius
- 受保护路径: specs/**, pipeline/adversary/**
- 红队聚焦: 意图 → spec → 测试设计路径（DECISION-02）

## 反摆拍断言（已补全）
- golden 回归纳入 CI 常驻 required check（AC-8）
- 配置面恰为 1 org secret + 1 org variable（AC-6）
- 凭据形状扫描：出现第 2 个凭据即判红

修复 diff 与 Veto 理由的机械闭环见 `e2e-runner.py` 的 `verify_veto_fix_loop()`。
