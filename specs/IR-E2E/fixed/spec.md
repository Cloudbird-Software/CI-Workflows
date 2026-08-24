# [已修复 spec] 红队守门端到端 — 强套件靶标（survived 腿）

> W5-C2 端到端实跑 fixture（Veto→修复→survived 闭环的 survived 腿）。
> 与 `specs/IR-E2E/test`（故意极差 spec）成对：同一审计管线，两次真实 LLM 运行，
> 弱套件判 insufficient（Veto）、强套件判 survived（放行）。

specVersion: 1
taskId: ISSUE-263-W5C2-E2E-FIXED
title: E2E fixture — tax 计算器（行为化验收标准 + 属性/负控制/边界用例）

## 意图
提供一个最小但**行为可判定**的实现目标：分层个税计算器。验收标准全部为
输入→输出断言（无"已核对"式声明性 AC），套件含正向用例、边界用例、
负控制（错误实现必红）与属性测试（常数实现必红）。

## 验收标准
- AC-1: calculate(income, brackets) 按给定制表逐层累进计税；表为
  [(upper_bound, rate), ...]，最后一层 upper_bound=None 表示无穷。
- AC-2: income ≤ 起征层 upper 的部分税额为 0（数值断言：income=3000 → tax=0）。
- AC-3: 跨层收入正确拆分（income=10000 → 3000*0 + 7000*0.1 = 700）。
- AC-4: 非法输入（income<0、brackets 为空）抛 ValueError（负控制）。
- AC-5: 对任意 0≤income≤10^6，tax 单调不减且 0 ≤ tax ≤ income（属性测试）。
