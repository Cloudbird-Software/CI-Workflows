---
taskId: FIXTURE-ENTROPY-1
specVersion: 1
title: 网关配额与限制（语义熵 fixture——INV-2 人为植入歧义，W4-C1 AC-1）
irRef: "Cloudbird-Software/CI-Workflows#fixture"
acceptanceCriteria:
  - id: AC-1
    given: 语义熵分歧度量运行于本 fixture
    when: 5 路跨族派生回放 + 聚簇 + 判定
    then: 歧义定位到 INV-2，其余条款单簇不误报
blastRadius:
  - repo: CI-Workflows
    path: pipeline/entropy/tests/fixtures/
nonGoals:
  - 真实网关实现
---

## INV 不变量

- INV-1: 网关对每个租户维护独立的请求配额，配额按分钟窗口重置
- INV-2: 高峰期网关对超出配额的请求进行限制，保障核心服务可用
- INV-3: 所有被限制的请求必须记录租户标识与时间戳

## BEH 行为

- BEH-1: 当请求被限制时，网关返回统一的结构化错误载荷，载荷含限制原因码
- BEH-2: 当分钟窗口重置时，租户配额恢复满额且历史计数归零

## IFACE 契约

- IFACE-1: 网关对外暴露每租户配额用量的只读查询接口
