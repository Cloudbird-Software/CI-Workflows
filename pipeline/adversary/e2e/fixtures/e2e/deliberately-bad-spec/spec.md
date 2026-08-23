# [故意极差 spec] 红队守门端到端 — 占位 spec（用于触发真实 Veto）

> 本 spec 为 W5-C2 端到端实跑测试 fixture，故意构造为「明显不合格」，
> 用于在本地模拟中触发真实的 `verdict=insufficient`（Veto）。
> 构造方法独立于判定脚本：以全零聚合分 + 缺失 criteria 溯源实现，
> 与 W5-C1 golden known-bad 样本（`golden-known-bad-01-empty-criteria`）同构。

specVersion: 99
taskId: ISSUE-263-W5C2-E2E
title: W5-C2 E2E fixture — deliberately insufficient spec

## 意图
本 spec 无任何有效验收标准溯源，criteria 聚合分全零，
用于验证红队守门 gate 不被特判绕过（AC-8 反向测试 + AC-4 故意极差 spec）。

## 验收标准（故意缺失溯源）
- AC-1: 占位，无 criteria 分解、无阈值 gate、无逐 criterion 连续分来源。
- AC-2: 占位，声明「已核对」但无运行时刻工件引用。

## 说明
本 fixture 的审计评分输入见 `adversary-scores.json`（全零聚合分），
预期 verdict = `insufficient`，触发 Veto → state=needs-human。
