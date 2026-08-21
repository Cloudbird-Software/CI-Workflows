#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""unlock-evaluate 解锁判定自测（W5-C2 AC-3/AC-4，ADR-0071 决策 3/4/5）。

- 连续 ≥50 例一致且零逃逸 → 解锁（49 不解锁——边界值钉死）
- 任一逃逸 → 连击清零 + reset-escape 事件
- owner 放行陷阱 → 连击清零 + reset-trap 事件（AC-3）
- 窗口内陷阱占比 <10% → 不解锁（fail-closed：无陷阱的干净流不能自证合格）
- 已解锁域不重复解锁；重放按 ts 稳定序
零网络零 LLM。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trust_gate  # noqa: E402

from _helpers import PREDICATES, UNLOCK_STATE, reconciled  # noqa: E402

PARAMS = {"min_consecutive_agreement": 50, "min_trap_ratio": 0.10,
          "post_unlock_sample_rate": 0.05}


def stream(n: int, domain: str = "docs-only", trap_every: int = 0, start_pr: int = 1,
           start_ts_min: int = 0):
    """n 条全部一致记录（每 trap_every 条混入一条陷阱，owner 正确关闭）。"""
    recs = []
    for i in range(n):
        trap = trap_every > 0 and (i + 1) % trap_every == 0
        recs.append(reconciled(
            pr=start_pr + i, domain=domain, shadow="would-reject" if trap else "would-merge",
            owner_ruling="closed" if trap else "merged", trap=trap,
            ts=f"2026-08-22T{((start_ts_min + i) // 60) % 24:02d}:{((start_ts_min + i) % 60):02d}:00Z"))
    return recs


class UnlockCase(unittest.TestCase):
    def setUp(self):
        self.state = trust_gate.load_unlock_state(UNLOCK_STATE,
                                                  trust_gate.load_predicates(PREDICATES))

    def evaluate(self, recs, state=None):
        return trust_gate.evaluate_unlock(recs, PARAMS, state or self.state)


class TestUnlockBoundary(UnlockCase):
    def test_49_agreements_with_traps_not_unlocked(self):
        """49 例一致（陷阱占比 10/49>10%）不解锁——阈值是 ≥50。"""
        recs = stream(44, trap_every=5) + stream(5, start_pr=100, start_ts_min=200)
        # 44+5=49 全一致，混陷阱：手动标 5 条陷阱（44 中每 5 一条=8 条 + 尾 5 条全陷阱）
        events, summary, _ = self.evaluate(recs)
        self.assertEqual(summary["docs-only"]["status_after"], "locked")

    def test_50_agreements_zero_escape_with_traps_unlocks(self):
        """AC-4 正路径：连续 50 例一致+零逃逸+陷阱占比 ≥10% → 解锁。"""
        recs = stream(45, trap_every=5) + stream(5, start_pr=100, start_ts_min=300,
                                                 trap_every=1)
        events, summary, new_state = self.evaluate(recs)
        self.assertEqual(summary["docs-only"]["streak"], 50)
        self.assertGreaterEqual(summary["docs-only"]["trap_ratio"], 0.10)
        self.assertEqual(summary["docs-only"]["status_after"], "unlocked")
        self.assertEqual(new_state["docs-only"], "unlocked")
        self.assertTrue(any(e["event"] == "unlocked" for e in events))
        self.assertEqual(
            [e for e in events if e["event"] == "unlocked"][0]["streak_after"], 50)

    def test_already_unlocked_not_re_unlocked(self):
        unlocked = {**self.state, "docs-only": "unlocked"}
        events, summary, _ = self.evaluate(stream(50, trap_every=5), state=unlocked)
        self.assertEqual(summary["docs-only"]["status_after"], "unlocked")
        self.assertFalse(any(e["event"] == "unlocked" for e in events))  # 无重复解锁事件

    def test_no_data_stays_locked_with_zero_streak(self):
        _, summary, _ = self.evaluate([])
        for d in summary:
            self.assertEqual(summary[d]["status_after"], "locked")
            self.assertEqual(summary[d]["streak"], 0)


class TestResetRules(UnlockCase):
    def base50(self, escape_at=None, trap_release_at=None):
        """50 一致流中注入一次失败（escape_at/trap_release_at 为 1-based 位置）。"""
        recs = stream(50, trap_every=5)
        if escape_at:
            recs[escape_at - 1] = reconciled(pr=999, domain="docs-only",
                                             shadow="would-merge", owner_ruling="closed",
                                             ts=f"2026-08-22T00:{escape_at - 1:02d}:00Z")
        if trap_release_at:
            recs[trap_release_at - 1] = reconciled(
                pr=998, domain="docs-only", shadow="would-reject", owner_ruling="merged",
                trap=True, ts=f"2026-08-22T00:{trap_release_at - 1:02d}:00Z")
        return recs

    def test_escape_resets_and_records_event(self):
        """AC-4：出现一例逃逸 → 计数重置（事件落 reset-escape，可回放）。"""
        recs = self.base50(escape_at=48)
        events, summary, _ = self.evaluate(recs)
        self.assertEqual(summary["docs-only"]["status_after"], "locked")
        self.assertEqual(summary["docs-only"]["streak"], 2)  # 逃逸后仅 2 例一致
        self.assertTrue(any(e["event"] == "reset-escape" for e in events))
        trigger = [e for e in events if e["event"] == "reset-escape"][0]["trigger"]
        self.assertEqual(trigger["pr"], 999)

    def test_escape_at_tail_keeps_streak_zero(self):
        recs = self.base50(escape_at=50)
        events, summary, _ = self.evaluate(recs)
        self.assertEqual(summary["docs-only"]["streak"], 0)
        self.assertEqual(summary["docs-only"]["status_after"], "locked")

    def test_trap_released_by_owner_resets(self):
        """AC-3：owner 放行陷阱 → 计数重置并记录（reset-trap 事件）。"""
        recs = self.base50(trap_release_at=45)
        events, summary, _ = self.evaluate(recs)
        self.assertEqual(summary["docs-only"]["status_after"], "locked")
        self.assertEqual(summary["docs-only"]["streak"], 5)
        self.assertTrue(any(e["event"] == "reset-trap" for e in events))

    def test_plain_disagreement_resets_without_escape(self):
        """owner 合而谓词拒（非逃逸方向）→ 同样清零连击（reset-disagreement）。"""
        recs = self.base50()
        recs[49] = reconciled(pr=997, domain="docs-only", shadow="would-reject",
                              owner_ruling="merged")
        events, summary, _ = self.evaluate(recs)
        self.assertEqual(summary["docs-only"]["streak"], 0)
        self.assertTrue(any(e["event"] == "reset-disagreement" for e in events))


class TestTrapRatio(UnlockCase):
    def test_clean_stream_without_traps_not_unlocked(self):
        """陷阱占比 <10% = 样本不合格：即使 50 例一致零逃逸也不解锁（fail-closed）。"""
        events, summary, _ = self.evaluate(stream(50, trap_every=0))
        self.assertEqual(summary["docs-only"]["streak"], 50)
        self.assertEqual(summary["docs-only"]["trap_ratio"], 0.0)
        self.assertEqual(summary["docs-only"]["status_after"], "locked")
        self.assertFalse(any(e["event"] == "unlocked" for e in events))

    def test_just_above_threshold_unlocks(self):
        """50 例中恰 5 条陷阱（=10%）达阈值下限 → 解锁（≥10% 含等号）。"""
        recs = stream(50, trap_every=10)  # i=9,19,29,39,49 → 5 条陷阱（恰 10%）
        events, summary, _ = self.evaluate(recs)
        self.assertEqual(summary["docs-only"]["traps_in_window"], 5)
        self.assertAlmostEqual(summary["docs-only"]["trap_ratio"], 0.10)
        self.assertEqual(summary["docs-only"]["status_after"], "unlocked")

    def test_4_traps_in_50_not_unlocked(self):
        """50 例只有 4 条陷阱（8%<10%）→ 不解锁（疫苗剂量不足）。"""
        recs = stream(46, trap_every=0) + stream(4, start_pr=100, start_ts_min=400,
                                                 trap_every=1)
        _, summary, _ = self.evaluate(recs)
        self.assertEqual(summary["docs-only"]["streak"], 50)
        self.assertLess(summary["docs-only"]["trap_ratio"], 0.10)
        self.assertEqual(summary["docs-only"]["status_after"], "locked")


class TestReplayOrder(UnlockCase):
    def test_ts_order_not_input_order(self):
        """重放按 ts 稳定序：乱序输入的逃逸仍按真实时序清零后续连击。"""
        late_agreements = stream(10, start_pr=1, start_ts_min=300)   # ts 较晚
        early_escape = [reconciled(pr=999, domain="docs-only", shadow="would-merge",
                                   owner_ruling="closed", ts="2026-08-22T02:00:00Z")]
        events, summary, _ = self.evaluate(early_escape + late_agreements)  # 乱序输入
        self.assertEqual(summary["docs-only"]["streak"], 10)  # 逃逸在前：10 例全数

    def test_domains_isolated(self):
        """docs-only 满足解锁流不影响 test-only/failing-repro-fix（各域独立计数）。"""
        events, summary, _ = self.evaluate(stream(50, trap_every=5))
        self.assertEqual(summary["docs-only"]["status_after"], "unlocked")
        self.assertEqual(summary["test-only"]["status_after"], "locked")
        self.assertEqual(summary["failing-repro-fix"]["status_after"], "locked")


if __name__ == "__main__":
    unittest.main()
