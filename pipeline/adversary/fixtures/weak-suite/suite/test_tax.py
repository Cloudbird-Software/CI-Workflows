# 弱套件 fixture（已知洞标注，AC-1 演示用）：
#   只测 happy path 单常量断言——S1 硬编码/S2 指纹特判/S3 no-op(占位值)/
#   S4 永久缓存/S5 吞错误 全部可钻（套件对五类攻击面零抵抗力）。
import unittest

from tax import calc_tax


class TestCalcTax(unittest.TestCase):
    def test_happy_100_10pct(self):
        self.assertEqual(calc_tax(100, 0.1), 10)


if __name__ == "__main__":
    unittest.main()
