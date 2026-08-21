#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指纹去重自测（W3-C2 AC-2）：同指纹（repo+场景+症状 sha256）不重复开单，
跨 run 幂等收敛。零网络零真实 LLM（离线回放）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _helpers as H  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import patrol  # noqa: E402


class TestFingerprintDedup(unittest.TestCase):
    def test_fingerprint_composition(self):
        # 沿用 drift-report 症状签名模式：repo/场景/症状任一维度不同 → 不同指纹
        base = patrol.fingerprint_of("a/b", "ac-X", "exit=1")
        self.assertTrue(base.startswith("sha256:"))
        self.assertNotEqual(base, patrol.fingerprint_of("a/c", "ac-X", "exit=1"))
        self.assertNotEqual(base, patrol.fingerprint_of("a/b", "ac-Y", "exit=1"))
        self.assertNotEqual(base, patrol.fingerprint_of("a/b", "ac-X", "exit=2"))
        self.assertEqual(base, patrol.fingerprint_of("a/b", "ac-X", "exit=1"))

    def test_cross_run_idempotent(self):
        state, out = H.tmpdir("patrol-fp-"), H.tmpdir("patrol-fp-out-")
        m1 = H.run_patrol(state, os.path.join(out, "r1"), "t-1", 42,
                          "2026-08-22T03:43:00Z")
        self.assertEqual(m1["opened_this_run"], 6)
        # +2h：小时窗口滚动后 deferred 可再攻击（去重只针对已开指纹）
        m2 = H.run_patrol(state, os.path.join(out, "r2"), "t-2", 43,
                          "2026-08-22T05:43:00Z")
        m3 = H.run_patrol(state, os.path.join(out, "r3"), "t-3", 44,
                          "2026-08-22T07:43:00Z")
        self.assertEqual(m3["opened_this_run"], 0, "第三轮零新开单（全指纹已见）")
        fps = [r["fingerprint"] for r in H.read_jsonl(os.path.join(state, "fingerprints.jsonl"))]
        self.assertEqual(len(fps), len(set(fps)), "台账无重复指纹（幂等）")
        self.assertGreater(m3["deduped"], 0)

    def test_symptom_excludes_volatile_trace(self):
        # 指纹只含跨 run 稳定症状（traceback 明细不进指纹——否则同 bug 每轮换哈希刷单）
        v1 = {"class": "crash", "symptom": "exit=1", "detail": "Traceback ... line 3 (run A)"}
        v2 = {"class": "crash", "symptom": "exit=1", "detail": "Traceback ... line 3 (run B)"}
        self.assertEqual(patrol.fingerprint_of("a/b", "ac-X", v1["symptom"]),
                         patrol.fingerprint_of("a/b", "ac-X", v2["symptom"]))


if __name__ == "__main__":
    unittest.main()
