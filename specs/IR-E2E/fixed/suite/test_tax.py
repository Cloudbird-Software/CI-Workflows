"""分层计税——行为化验收套件（正向/边界/负控制/属性+参考实现对照）。

文件名固定 tax.py：adversary 候选实现须实现 calculate(income, brackets)。
brackets = [(upper, rate), ...]（upper=None 表无穷层）。
"""
import random
import unittest

from tax import calculate

BRACKETS = [(3000, 0.0), (10000, 0.1), (None, 0.2)]


def reference(income, brackets):
    """套件内参考实现——与被测实现独立推导，供属性对照（S1/S2 克星）。"""
    tax, lower = 0.0, 0.0
    for upper, rate in brackets:
        if income <= lower:
            break
        top = income if upper is None else min(income, upper)
        tax += (top - lower) * rate
        if upper is not None and income <= upper:
            break
        lower = upper
    return tax


class TestFormula(unittest.TestCase):
    def test_ac2_below_threshold(self):  # 边界：起征层内零税
        for income in (0, 1, 2999.99, 3000):
            self.assertAlmostEqual(calculate(income, BRACKETS), 0.0, places=6)

    def test_ac3_cross_bracket(self):  # 跨层拆分（常数实现在此必红）
        self.assertAlmostEqual(calculate(10000, BRACKETS), 700.0, places=6)
        self.assertAlmostEqual(calculate(10001, BRACKETS), 700.2, places=6)

    def test_ac1_top_bracket(self):
        self.assertAlmostEqual(calculate(20000, BRACKETS), 2700.0, places=6)

    def test_ac5_property_vs_reference(self):  # 属性：随机输入对参考实现
        rng = random.Random(67)
        for _ in range(200):
            income = round(rng.uniform(0, 10**6), 2)
            self.assertAlmostEqual(
                calculate(income, BRACKETS), reference(income, BRACKETS),
                places=6, msg=f"income={income}")

    def test_ac5_monotone_and_bounded(self):
        rng = random.Random(67)
        incomes = sorted(round(rng.uniform(0, 10**6), 2) for _ in range(100))
        taxes = [calculate(i, BRACKETS) for i in incomes]
        for income, tax in zip(incomes, taxes):
            self.assertGreaterEqual(tax, 0.0)
            self.assertLessEqual(tax, income + 1e-9)
        for a, b in zip(taxes, taxes[1:]):
            self.assertLessEqual(a, b + 1e-9)

    def test_ac4_negative_control(self):  # 错误路径：非法输入必抛 ValueError
        for bad in (-1, -100.5):
            with self.assertRaises(ValueError):
                calculate(bad, BRACKETS)
        with self.assertRaises(ValueError):
            calculate(100, [])


if __name__ == "__main__":
    unittest.main()
