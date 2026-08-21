#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reconcile 比对自测（W5-C2 AC-2，ADR-0071 决策 2/4）。

shadow 决策 × owner 实际裁决（merged/closed）：
- 一致性判定两方向（放行↔merged、拒绝↔closed）
- 逃逸=owner 拒而谓词放行（零逃逸是解锁必要条件）
- 陷阱两类失败分记：谓词放行已知应拒样本（=逃逸）/ owner 放行陷阱（AC-3 重置原料）
- 排除域与 human-sign 不具可比性：counted=false
零网络零 LLM。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trust_gate  # noqa: E402

from _helpers import PREDICATES, decision_rec, ruling_rec  # noqa: E402


class ReconcileCase(unittest.TestCase):
    def setUp(self):
        self.p = trust_gate.load_predicates(PREDICATES)
        self.unlockable = set(self.p["domains"])

    def run_reconcile(self, decisions, rulings):
        return trust_gate.reconcile(decisions, rulings, self.unlockable)


class TestAgreement(ReconcileCase):
    def test_would_merge_merged_agrees(self):
        recs, s = self.run_reconcile(
            [decision_rec(1, "docs-only", "would-merge")], [ruling_rec(1, "merged")])
        self.assertTrue(recs[0]["agreement"])
        self.assertFalse(recs[0]["escape"])
        self.assertEqual(s["counted_pairs"], 1)
        self.assertEqual(s["per_domain"]["docs-only"]["agreements"], 1)

    def test_would_reject_closed_agrees(self):
        recs, _ = self.run_reconcile(
            [decision_rec(1, "test-only", "would-reject", ts="2026-08-22T01:00:00Z")],
            [ruling_rec(1, "closed", ts="2026-08-22T05:00:00Z")])
        self.assertTrue(recs[0]["agreement"])

    def test_owner_merged_but_shadow_rejected_is_disagreement(self):
        """owner 合而谓词拒：不一致（清零连击）但不是逃逸（方向性）。"""
        recs, s = self.run_reconcile(
            [decision_rec(1, "docs-only", "would-reject")], [ruling_rec(1, "merged")])
        self.assertFalse(recs[0]["agreement"])
        self.assertFalse(recs[0]["escape"])
        self.assertEqual(s["per_domain"]["docs-only"]["agreements"], 0)


class TestEscape(ReconcileCase):
    def test_owner_closed_but_predicates_passed_is_escape(self):
        """逃逸定义（卡面原文）：owner 拒而谓词放行。"""
        recs, s = self.run_reconcile(
            [decision_rec(1, "docs-only", "would-merge")], [ruling_rec(1, "closed")])
        self.assertTrue(recs[0]["escape"])
        self.assertFalse(recs[0]["agreement"])
        self.assertEqual(s["escapes"], 1)

    def test_escape_recorded_as_disagreement_too(self):
        recs, _ = self.run_reconcile(
            [decision_rec(2, "docs-only", "auto-merge")], [ruling_rec(2, "closed")])
        self.assertEqual(recs[0]["shadow_decision"], "auto-merge")
        self.assertTrue(recs[0]["escape"])


class TestTrap(ReconcileCase):
    def test_trap_passed_by_predicate_is_escape(self):
        """陷阱=已知应拒样本；谓词放行了它=免疫失败=逃逸形态。"""
        recs, s = self.run_reconcile(
            [decision_rec(9, "docs-only", "would-merge", trap=True)],
            [ruling_rec(9, "closed")])
        self.assertTrue(recs[0]["trap"])
        self.assertTrue(recs[0]["trap_passed_by_predicate"])
        self.assertTrue(recs[0]["escape"])
        self.assertEqual(s["traps"], 1)

    def test_trap_released_by_owner_recorded(self):
        """AC-3：owner 放行陷阱 → 记录重置原料（unlock-evaluate 消费清零连击）。"""
        recs, _ = self.run_reconcile(
            [decision_rec(9, "docs-only", "would-reject", trap=True)],
            [ruling_rec(9, "merged")])
        self.assertTrue(recs[0]["trap_released_by_owner"])
        self.assertFalse(recs[0]["trap_passed_by_predicate"])
        self.assertFalse(recs[0]["agreement"])  # 谓词拒 vs owner 合=不一致

    def test_trap_double_failure(self):
        """最坏形态：谓词放行陷阱且 owner 也放行——两类失败同时记录。"""
        recs, _ = self.run_reconcile(
            [decision_rec(9, "docs-only", "would-merge", trap=True)],
            [ruling_rec(9, "merged")])
        self.assertTrue(recs[0]["trap_passed_by_predicate"])
        self.assertTrue(recs[0]["trap_released_by_owner"])
        self.assertTrue(recs[0]["agreement"])  # 判定一致但都是错的——一致≠正确，
        # 免疫力靠陷阱占比与 unlock-evaluate 的重置规则兜底（见 test_unlock）

    def test_trap_correctly_rejected_and_closed_agrees(self):
        recs, _ = self.run_reconcile(
            [decision_rec(9, "docs-only", "would-reject", trap=True)],
            [ruling_rec(9, "closed")])
        self.assertTrue(recs[0]["agreement"])
        self.assertFalse(recs[0]["trap_passed_by_predicate"])


class TestCountingScope(ReconcileCase):
    def test_excluded_domain_not_counted(self):
        """排除域（如 ci-workflows）决策不具可比性——不计入域计数。"""
        recs, s = self.run_reconcile(
            [decision_rec(1, "ci-workflows", "would-merge")], [ruling_rec(1, "closed")])
        self.assertFalse(recs[0]["counted"])
        self.assertIsNone(recs[0]["agreement"])
        self.assertEqual(s["counted_pairs"], 0)
        self.assertEqual(s["escapes"], 0)

    def test_human_sign_not_counted(self):
        recs, s = self.run_reconcile(
            [decision_rec(1, "docs-only", "human-sign")], [ruling_rec(1, "merged")])
        self.assertFalse(recs[0]["counted"])
        self.assertEqual(s["counted_pairs"], 0)

    def test_pr_without_ruling_skipped(self):
        """尚开着（无裁决）的 PR 不比对——留待下轮（数据完整性）。"""
        _, s = self.run_reconcile([decision_rec(1, "docs-only", "would-merge")], [])
        self.assertEqual(s["pairs"], 0)

    def test_per_domain_isolation(self):
        """域计数互不污染（docs-only 的逃逸不计入 test-only）。"""
        _, s = self.run_reconcile(
            [decision_rec(1, "docs-only", "would-merge"),
             decision_rec(2, "test-only", "would-merge")],
            [ruling_rec(1, "closed"), ruling_rec(2, "merged")])
        self.assertEqual(s["per_domain"]["docs-only"]["escapes"], 1)
        self.assertEqual(s["per_domain"]["test-only"]["escapes"], 0)
        self.assertEqual(s["per_domain"]["test-only"]["agreements"], 1)


class TestFailClosedInputs(ReconcileCase):
    def test_duplicate_ruling_rejected(self):
        """append-only 流不允许改判：同 PR 双裁决=输入非法（infra）。"""
        with self.assertRaises(trust_gate.TrustGateError):
            self.run_reconcile([], [ruling_rec(1, "merged"), ruling_rec(1, "closed")])

    def test_invalid_ruling_value_rejected(self):
        with self.assertRaises(trust_gate.TrustGateError):
            self.run_reconcile([], [{"schema": "trust-shadow/v1", "record": "ruling",
                                     "ts": "t", "repo": "R", "pr": 1, "ruling": "reopened"}])

    def test_invalid_decision_value_rejected(self):
        with self.assertRaises(trust_gate.TrustGateError):
            self.run_reconcile(
                [decision_rec(1, "docs-only", "probably-fine")], [ruling_rec(1, "merged")])


if __name__ == "__main__":
    unittest.main()
