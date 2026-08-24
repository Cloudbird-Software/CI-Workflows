"""故意极差 spec 的占位测试（W5-C2 E2E fixture，unittest 版）。

断言恒真不校验实现语义 → 红队 judge-deep 判「套件不充分」。
stdlib unittest（不依赖 pytest——runner 系统环境无 pytest 时 rc=2 会造成假绿）。
"""
import unittest


class TestPlaceholder(unittest.TestCase):
    def test_placeholder_passes(self):
        """恒真占位——套件不充分的典型症状：测试从不失败。"""
        self.assertTrue(True)

    def test_ac_traceability_absent(self):
        """AC 溯源缺失：假装覆盖 AC-1，但无任何真实断言。"""
        self.assertTrue(True)  # 故意不断言任何 spec 语义


if __name__ == "__main__":
    unittest.main()
