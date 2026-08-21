#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""observation 桶自测（W3-C2 AC-1）：LLM"看着不对"两次独立（不同 run 且不同
seed）出现才升级开单；单次/同 seed/同 run 一律不升级。零网络零真实 LLM。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import patrol  # noqa: E402

NOTE = "mul(6,7) 与 add(6,7) 一致性看着不对"
REPO = "Cloudbird-Software/CI-Workflows"


def obs(state, run_id, seed, ts="2026-08-22T03:43:00Z", note=NOTE):
    return patrol.record_observation(state, REPO, "llm:mul", note, run_id, seed, ts)


class TestObservationBucket(unittest.TestCase):
    def setUp(self):
        self.state = patrol.State(tempfile.mkdtemp(prefix="patrol-obs-"))

    def test_single_occurrence_no_escalation(self):
        _, escalated, _ = obs(self.state, "run-1", 42)
        self.assertFalse(escalated)

    def test_two_independent_escalate(self):
        obs(self.state, "run-1", 42)
        _, escalated, occ = obs(self.state, "run-2", 43)
        self.assertTrue(escalated)
        self.assertEqual(len(occ), 2)

    def test_same_seed_different_run_no_escalation(self):
        # 独立=不同 run 且不同 seed——同 seed（如 seed 固定配置）两次不算独立
        obs(self.state, "run-1", 42)
        _, escalated, _ = obs(self.state, "run-2", 42)
        self.assertFalse(escalated)

    def test_same_run_different_seed_no_escalation(self):
        obs(self.state, "run-1", 42)
        _, escalated, _ = obs(self.state, "run-1", 43)
        self.assertFalse(escalated)

    def test_third_occurrence_after_open_dedupes(self):
        obs(self.state, "run-1", 42)
        fp, escalated, _ = obs(self.state, "run-2", 43)
        self.assertTrue(escalated)
        patrol.append_jsonl(self.state.fp, {"fingerprint": fp, "repo": REPO,
                                            "scenario_id": "observation",
                                            "source": "llm-metamorphic", "oracle": {"class": "invariant"},
                                            "payloads": [], "symptom": "observation-escalated",
                                            "target_service": "x", "ts": "2026-08-22T04:00:00Z",
                                            "run_id": "run-2", "issue_ref": "draft:x.md"})
        _, escalated_again, _ = obs(self.state, "run-3", 44)
        self.assertFalse(escalated_again)  # 已开单 → 指纹层去重（AC-2 同语义）

    def test_fingerprint_stable_per_note(self):
        a = patrol.fingerprint_of(REPO, "llm:mul", "looks-wrong:" + patrol.sha256_text(NOTE)[:16])
        b = patrol.fingerprint_of(REPO, "llm:mul", "looks-wrong:" + patrol.sha256_text(NOTE)[:16])
        c = patrol.fingerprint_of(REPO, "llm:mul", "looks-wrong:" + patrol.sha256_text(NOTE + "!")[:16])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main()
