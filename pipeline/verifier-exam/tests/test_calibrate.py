# -*- coding: utf-8 -*-
"""calibrate.py 自测试（AC-3；ADR-0072 决策 3/4；零网络零真实推理）。

覆盖：Wilson CI 已知值；owner merge/reject 事件→校准样本静默回流（去重幂等）；
出分带校正值±CI；CI 下界低于及格线→needs_human（含边界：恰等于及格线不升）；
needs_human 只是信号不阻断（exit 0）；owner 确认率 ≥20% 指标位从 0 起累积。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import calibrate as C  # noqa: E402

EVENTS = [
    {"number": 101, "state": "closed", "merged_at": "2026-08-20T10:00:00Z",
     "closed_at": "2026-08-20T10:00:00Z"},
    {"number": 102, "state": "closed", "merged_at": None,
     "closed_at": "2026-08-20T11:00:00Z",
     "_reviews": [{"state": "CHANGES_REQUESTED"}]},
    {"number": 103, "state": "closed", "merged_at": None,
     "closed_at": "2026-08-20T12:00:00Z"},   # 关闭无评审证据=ambiguous
    {"number": 101, "state": "closed", "merged_at": "2026-08-20T10:00:00Z",
     "closed_at": "2026-08-20T10:00:00Z"},   # 重复事件
    {"number": 104, "state": "closed", "merged_at": "2026-07-01T00:00:00Z",
     "closed_at": "2026-07-01T00:00:00Z"},   # 早于 since=超出回看窗
]


class TestWilson(unittest.TestCase):
    def test_known_interval(self):
        # 独立手算基线：p̂=0.7, n=10, z=1.96 → (0.3965, 0.8920)
        lo, hi = C.wilson_ci(0.7, 10)
        self.assertAlmostEqual(lo, 0.3965, places=3)
        self.assertAlmostEqual(hi, 0.8920, places=3)

    def test_degenerate_edges(self):
        self.assertEqual(C.wilson_ci(1.0, 5)[1], 1.0)
        self.assertEqual(C.wilson_ci(0.0, 5)[0], 0.0)
        self.assertEqual(C.wilson_ci(0.5, 0), (0.0, 1.0))   # 无数据=满宽


class TestCollect(unittest.TestCase):
    def _run(self, tmp, twice=False):
        evf = tmp / "events.json"
        evf.write_text(json.dumps(EVENTS), encoding="utf-8")
        out = tmp / "samples.jsonl"
        argv = ["collect", "--repo", "ORG/REPO", "--since", "2026-08-19T00:00:00Z",
                "--events-file", str(evf), "--out", str(out)]
        rc1 = C.main(argv)
        if twice:
            rc1 = (rc1, C.main(argv))   # 同事件重放：幂等（零额外副作用）
        return rc1, out

    def test_owner_actions_become_samples_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (rc, rc2), out = self._run(tmp, twice=True)
            self.assertEqual(rc, 0)
            self.assertEqual(rc2, 0)
            lines = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines), 2)   # merged + rejected；重复/ambiguous/超窗不入
            actions = {s["pr"]: s["owner_action"] for s in lines}
            self.assertEqual(actions, {101: "approve", 102: "reject"})
            self.assertTrue(all(s["judge_verdict"] is None for s in lines))   # 待 join

    def test_classify_ambiguous_closed_without_review(self):
        self.assertEqual(C.classify_pr(EVENTS[0]), "approve")
        self.assertEqual(C.classify_pr(EVENTS[1]), "reject")
        self.assertEqual(C.classify_pr(EVENTS[2]), "")   # 宁缺勿滥


class TestScore(unittest.TestCase):
    def _samples(self, tp, fn, tn, fp):
        s = []
        for i in range(tp):
            s.append({"owner_action": "reject", "judge_verdict": "negative"})
        for i in range(fn):
            s.append({"owner_action": "reject", "judge_verdict": "positive"})
        for i in range(tn):
            s.append({"owner_action": "approve", "judge_verdict": "positive"})
        for i in range(fp):
            s.append({"owner_action": "approve", "judge_verdict": "negative"})
        return s

    def test_insufficient_calibration_escalates(self):
        r = C.score([], raw_score=0.95, pass_line=0.80, min_join=30)
        self.assertEqual(r["calibration_status"], "insufficient-calibration")
        self.assertEqual(r["ci"], [0.0, 1.0])          # 不确定=不确定，不伪装
        self.assertTrue(r["needs_human"])              # CI 下界 0 < 及格线 → 升人类
        self.assertEqual(r["escalation"], "human")
        self.assertFalse(r["blocking"])                # 信号不阻断
        self.assertEqual(r["owner_confirmation_rate"]["value"], 0.0)   # 从 0 起累积
        self.assertEqual(r["owner_confirmation_rate"]["target_min"], 0.20)
        self.assertEqual(r["owner_confirmation_rate"]["status"], "accumulating")

    def test_low_sensitivity_ci_lower_below_line(self):
        r = C.score(self._samples(tp=18, fn=12, tn=30, fp=2), 0.90, 0.80, 30)
        self.assertEqual(r["calibration_status"], "calibrated")
        self.assertEqual(r["confusion"], {"tp": 18, "fn": 12, "tn": 30, "fp": 2,
                                          "joined_pairs": 62})
        self.assertAlmostEqual(r["sensitivity"]["hat"], 0.6)
        self.assertAlmostEqual(r["corrected_score"], 0.9 * 0.6, places=6)
        self.assertLess(r["ci"][0], 0.80)              # 0.9×sens_lo < 0.8 → 升人类
        self.assertTrue(r["needs_human"])

    def test_high_sensitivity_no_escalation(self):
        r = C.score(self._samples(tp=60, fn=0, tn=60, fp=0), 0.90, 0.80, 30)
        self.assertFalse(r["needs_human"])
        self.assertEqual(r["escalation"], "none")
        self.assertGreaterEqual(r["ci"][0], 0.80)      # 0.9×wilson_lo(1.0,60)≈0.941≥0.8
        self.assertLessEqual(r["ci"][0] <= r["corrected_score"] <= r["ci"][1], True)

    def test_boundary_ci_lower_equals_pass_line_not_escalated(self):
        base = C.score(self._samples(tp=18, fn=12, tn=30, fp=2), 0.90, 0.80, 30)
        edge = C.score(self._samples(tp=18, fn=12, tn=30, fp=2), 0.90, base["ci"][0], 30)
        self.assertFalse(edge["needs_human"])          # 下界==及格线：不升（严格小于才升）
        above = C.score(self._samples(tp=18, fn=12, tn=30, fp=2), 0.90,
                        base["ci"][0] + 1e-9, 30)
        self.assertTrue(above["needs_human"])

    def test_cli_score_exit_zero_even_when_escalating(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sf = tmp / "s.jsonl"
            sf.write_text("".join(
                json.dumps(s) + "\n" for s in self._samples(18, 12, 30, 2)), encoding="utf-8")
            rc = C.main(["score", "--samples", str(sf), "--raw-score", "0.9",
                         "--pass-line", "0.8", "--min-join", "30"])
            self.assertEqual(rc, 0)   # AC-3：升人类=输出信号，不阻断记录

    def test_owner_confirmation_slot_grows_with_joins(self):
        joined = self._samples(5, 5, 5, 5) + [{"owner_action": "approve", "judge_verdict": None}] * 5
        r = C.score(joined, 0.9, 0.8, 1)
        self.assertAlmostEqual(r["owner_confirmation_rate"]["value"], 0.8)   # 25 样本 20 join
        self.assertEqual(r["owner_confirmation_rate"]["status"], "accumulating")


if __name__ == "__main__":
    unittest.main(verbosity=2)
