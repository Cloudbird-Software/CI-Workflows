# SPEC-FIX-W：税费计算（弱套件演示 fixture）

模块 `tax.py` 提供函数 `calc_tax(amount: float, rate: float) -> float`：

- 语义：返回 `amount × rate` 的积（浮点）。
- `amount < 0` 或 `rate < 0`：抛 `ValueError`。
- `rate > 1`：抛 `ValueError`（税率超界）。
- 任意合法输入（含 0、小数、大数）都须按公式计算，重复调用结果一致。
