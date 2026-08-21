#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sample 抽审自测（W5-C2 AC-4，ADR-0071 决策 5）。

解锁后 5% 随机抽人审：sha256 PRF 确定性（同种子同结果——审计可复现）、
种子注入改变选择、rate 边界、非法输入 fail-closed。零网络零 LLM。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trust_gate  # noqa: E402

PRS = list(range(1, 201))  # 200 例：5% 期望 10 例上下


class TestSampleDeterminism(unittest.TestCase):
    def test_same_seed_same_selection(self):
        """同 (seed, domain, prs) 永远同结果——审计者可复现整次抽样。"""
        a = trust_gate.select_sample(PRS, "docs-only", 0.05, "seed-2026W34")
        b = trust_gate.select_sample(PRS, "docs-only", 0.05, "seed-2026W34")
        self.assertEqual(a["selected"], b["selected"])
        self.assertEqual(a["rate"], 0.05)
        self.assertEqual(a["total"], 200)

    def test_seed_injection_changes_selection(self):
        """种子是注入点：换种子应改变选择（且两者都确定）。"""
        a = trust_gate.select_sample(PRS, "docs-only", 0.05, "seed-A")
        b = trust_gate.select_sample(PRS, "docs-only", 0.05, "seed-B")
        self.assertNotEqual(a["selected"], b["selected"])

    def test_domain_scopes_selection(self):
        """抽样按域独立：同种子不同域选择不同（防跨域关联）。"""
        a = trust_gate.select_sample(PRS, "docs-only", 0.05, "seed-X")
        b = trust_gate.select_sample(PRS, "test-only", 0.05, "seed-X")
        self.assertNotEqual(a["selected"], b["selected"])

    def test_rate_one_selects_all(self):
        r = trust_gate.select_sample([1, 2, 3], "docs-only", 1.0, "s")
        self.assertEqual(r["selected"], [1, 2, 3])

    def test_rate_small_selects_few(self):
        """rate 单调性方向检查（PRF 无结构性保证，只锁「小 rate 不多抽」形态）。"""
        big = trust_gate.select_sample(PRS, "docs-only", 0.05, "s")
        tiny = trust_gate.select_sample(PRS, "docs-only", 0.005, "s")
        self.assertLessEqual(len(tiny["selected"]), len(big["selected"]) + 2)

    def test_ratio_reported(self):
        r = trust_gate.select_sample(PRS, "docs-only", 0.05, "seed-2026W34")
        self.assertAlmostEqual(r["selected_ratio"], len(r["selected"]) / 200)
        self.assertEqual(r["schema"], "trust-shadow/v1")
        self.assertEqual(r["record"], "sample")

    def test_bad_inputs_fail_closed(self):
        with self.assertRaises(trust_gate.TrustGateError):
            trust_gate.select_sample([], "docs-only", 0.05, "s")
        with self.assertRaises(trust_gate.TrustGateError):
            trust_gate.select_sample([0, -1], "docs-only", 0.05, "s")
        with self.assertRaises(trust_gate.TrustGateError):
            trust_gate.select_sample(PRS, "docs-only", 1.5, "s")
        with self.assertRaises(trust_gate.TrustGateError):
            trust_gate.select_sample(PRS, "docs-only", 0.0, "s")


if __name__ == "__main__":
    unittest.main()
