#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_golden_set.py —— golden_set.py 单元测试（W5-C1 .github#286 / AC-8）

覆盖：
  - compute_verdict 纯函数（survived / insufficient / void 三路径 + 浮点边界）
  - blind_sample 元数据剥离（id/source/type/expected_verdict 剥除，gate 入参保留）
  - regress 回归断言（已知不合格仍不合格；变合格 = 失败）
  - 端到端：golden_set.py regress CLI（native 模式）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import golden_set as gs


class TestComputeVerdict(unittest.TestCase):
    def test_all_pass(self):
        scores = [
            {"id": "a", "aggregated": 0.8, "threshold": 0.7},
            {"id": "b", "aggregated": 0.9, "threshold": 0.7},
        ]
        self.assertEqual(gs.compute_verdict(scores, True), gs.GATE_PASS)

    def test_one_below_threshold(self):
        scores = [
            {"id": "a", "aggregated": 0.8, "threshold": 0.7},
            {"id": "b", "aggregated": 0.5, "threshold": 0.7},
        ]
        self.assertEqual(gs.compute_verdict(scores, True), gs.GATE_FAIL)

    def test_token_mismatch_void(self):
        scores = [{"id": "a", "aggregated": 0.99, "threshold": 0.7}]
        self.assertEqual(gs.compute_verdict(scores, False), gs.GATE_VOID)

    def test_boundary_exact_threshold(self):
        # 恰好等于阈值 → 通过（浮点容差内）
        scores = [{"id": "a", "aggregated": 0.7, "threshold": 0.7}]
        self.assertEqual(gs.compute_verdict(scores, True), gs.GATE_PASS)

    def test_boundary_just_below(self):
        # 略低于阈值 → 失败
        scores = [{"id": "a", "aggregated": 0.699, "threshold": 0.7}]
        self.assertEqual(gs.compute_verdict(scores, True), gs.GATE_FAIL)

    def test_empty_scores_pass(self):
        # 无 criterion 且 token ok → 通过（vacuous truth）
        self.assertEqual(gs.compute_verdict([], True), gs.GATE_PASS)

    def test_blocking(self):
        self.assertFalse(gs.compute_blocking(gs.GATE_PASS))
        self.assertTrue(gs.compute_blocking(gs.GATE_FAIL))
        self.assertTrue(gs.compute_blocking(gs.GATE_VOID))


class TestBlindSample(unittest.TestCase):
    def test_strips_metadata(self):
        s = {"id": "x", "source": "y", "type": "known-bad", "expected_verdict": "insufficient",
             "criteria_scores": [], "token_account_ok": True}
        b = gs.blind_sample(s)
        for k in ("id", "source", "type", "expected_verdict"):
            self.assertNotIn(k, b)
        self.assertIn("criteria_scores", b)
        self.assertIn("token_account_ok", b)

    def test_preserves_gate_inputs(self):
        s = {"id": "x", "criteria_scores": [{"id": "a", "aggregated": 0.1, "threshold": 0.7}],
             "token_account_ok": True, "threshold_global": 0.7}
        b = gs.blind_sample(s)
        self.assertEqual(b["threshold_global"], 0.7)
        self.assertEqual(len(b["criteria_scores"]), 1)


class TestRegress(unittest.TestCase):
    def test_all_bad_still_bad(self):
        replay = {
            "results": [
                {"id": "a", "expected_verdict": "insufficient", "actual_verdict": "insufficient"},
                {"id": "b", "expected_verdict": "void", "actual_verdict": "void"},
            ]
        }
        summary = gs.regress(replay)
        self.assertTrue(summary["regress_ok"])
        self.assertEqual(summary["failed"], 0)

    def test_bad_becomes_good_fails(self):
        replay = {
            "results": [
                {"id": "a", "expected_verdict": "insufficient", "actual_verdict": "survived"},
            ]
        }
        summary = gs.regress(replay)
        self.assertFalse(summary["regress_ok"])
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["failed_details"][0]["id"], "a")


class TestEndToEndCLI(unittest.TestCase):
    SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures", "golden", "samples")

    def test_load_cli(self):
        rc = subprocess.call([sys.executable, gs.__file__, "load",
                              "--samples-dir", self.SAMPLES_DIR])
        self.assertEqual(rc, 0)

    def test_regress_cli_native(self):
        rc = subprocess.call([sys.executable, gs.__file__, "regress",
                              "--samples-dir", self.SAMPLES_DIR, "--mode", "native"])
        self.assertEqual(rc, 0)

    def test_blind_cli(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "blind.json")
            rc = subprocess.call([sys.executable, gs.__file__, "blind",
                                  "--samples-dir", self.SAMPLES_DIR, "--out", out])
            self.assertEqual(rc, 0)
            blinded = json.loads(open(out, encoding="utf-8").read())
            self.assertEqual(blinded["count"], 4)
            for s in blinded["samples"]:
                for k in ("id", "source", "type", "expected_verdict"):
                    self.assertNotIn(k, s)


if __name__ == "__main__":
    unittest.main()
