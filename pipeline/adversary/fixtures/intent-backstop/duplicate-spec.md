---
taskId: ISSUE-DUP
specVersion: 1
title: 为仓库增加 flaky-retry 重试工具
irRef: Cloudbird-Software/.github#999
acceptanceCriteria:
  - id: AC-1
    given: 仓库已存在 CI 脚本
    when: 某步骤失败
    then: 自动按策略重试并记录 flaky 事件
blastRadius:
  - repo: .github
  - repo: CI-Workflows
nonGoals:
  - 不替代现有 CI 工作流
---

## 背景
CI 中 flaky 测试需要重试机制。本卡引入 flaky-retry 重试脚本，失败后按策略重试。
