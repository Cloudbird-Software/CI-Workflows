# 强套件 fixture（AC-2 演示用）：属性（参数化多组）+ 边界 + 错误路径负控制 +
# 重复/交替调用防永久缓存——对 S1-S5 攻击面均有抵抗力。
import unittest

from tax import calc_tax

# 同构异值对照（S1 硬编码/S2 指纹特判/S4 永久缓存的共同克星：期望不可枚举背诵）
CASES = [
    (100, 0.1, 10.0),
    (200, 0.25, 50.0),
    (7, 0.3, 2.1),
    (0, 0.5, 0.0),
    (1234.56, 0.075, 92.592),
]


class TestFormula(unittest.TestCase):
    def test_formula_pairs(self):
        for amount, rate, want in CASES:
            with self.subTest(amount=amount, rate=rate):
                self.assertAlmostEqual(calc_tax(amount, rate), want, places=6)

    def test_result_is_float_semantics(self):  # S3 no-op 桩（返回占位值）在此红
        self.assertAlmostEqual(calc_tax(100, 0.1), 10.0, places=6)


class TestErrors(unittest.TestCase):
    def test_negative_amount_raises(self):  # S5 吞错误在此红
        with self.assertRaises(ValueError):
            calc_tax(-1, 0.1)

    def test_negative_rate_raises(self):
        with self.assertRaises(ValueError):
            calc_tax(100, -0.1)

    def test_rate_above_one_raises(self):
        with self.assertRaises(ValueError):
            calc_tax(100, 1.5)


class TestAntiCache(unittest.TestCase):
    def test_alternating_calls(self):  # S4 永久缓存首值在此红（回放错值≠当前输入）
        self.assertAlmostEqual(calc_tax(100, 0.1), 10.0, places=6)
        self.assertAlmostEqual(calc_tax(200, 0.25), 50.0, places=6)
        self.assertAlmostEqual(calc_tax(100, 0.1), 10.0, places=6)


if __name__ == "__main__":
    unittest.main()
