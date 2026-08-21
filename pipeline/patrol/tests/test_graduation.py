#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""毕业机制自测（W3-C2 AC-3）：复现成功 → 场景毕业（回归测试文件 fail-before 红）
+ 离开语料（后续 run 不再消费）；非 reproduced 毕业被拒。零网络零真实 LLM。"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _helpers as H  # noqa: E402


class TestGraduation(unittest.TestCase):
    def test_full_graduation_flow(self):
        state, out = H.tmpdir("patrol-grad-"), H.tmpdir("patrol-grad-out-")
        m1 = H.run_patrol(state, os.path.join(out, "r1"), "g-1", 42, "2026-08-22T03:43:00Z")
        self.assertEqual(m1["opened_this_run"], 6)
        fps = {r["scenario_id"]: r["fingerprint"]
               for r in H.read_jsonl(os.path.join(state, "fingerprints.jsonl"))}
        fp = fps["ac-AC-DEM-2"]

        # 未判定 → 毕业拒绝（exit 2）
        r = H.patrol_cli("graduate", "--state", state, "--fingerprint", fp,
                         "--out", os.path.join(out, "grad"))
        self.assertEqual(r.returncode, 2)

        # falsified → 毕业仍拒绝（毕业仅由复现成功触发，ADR-0065 决策 4）
        H.patrol_cli("verdict", "--state", state, "--fingerprint", fp,
                     "--verdict", "falsified")
        r = H.patrol_cli("graduate", "--state", state, "--fingerprint", fp,
                         "--out", os.path.join(out, "grad"))
        self.assertEqual(r.returncode, 2)

        # falsified → reproduced 追记：最新判定生效
        H.patrol_cli("verdict", "--state", state, "--fingerprint", fp,
                     "--verdict", "reproduced")
        r = H.patrol_cli("graduate", "--state", state, "--fingerprint", fp,
                         "--out", os.path.join(out, "grad"))
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(rec["scenario_id"], "ac-AC-DEM-2")
        self.assertTrue(rec["removed_from_corpus"])

        # 回归测试文件：fail-before（靶场缺陷仍在 → 必须红，且是断言红而非语法/环境错）
        reg = rec["regression_test"]
        self.assertTrue(os.path.isfile(reg))
        red = subprocess.run([sys.executable, reg], capture_output=True, text=True)
        self.assertNotEqual(red.returncode, 0, "毕业回归在缺陷修复前必须红（ADR-0061）")
        self.assertIn("AssertionError", red.stderr + red.stdout,
                      "红必须是断言失败（语法/环境错=生成的测试自身有 bug）")

        # 语料移除：后续 run 该场景不再生成（防刷熟）
        m2 = H.run_patrol(state, os.path.join(out, "r2"), "g-2", 43, "2026-08-22T05:43:00Z")
        self.assertEqual(m2["scenarios"]["ac-registry"], 3)
        self.assertIn("ac-AC-DEM-2", m2["graduated_active"])

        # 重复毕业幂等（同一指纹再 graduate 不产生第二张回归/不炸）
        r2 = H.patrol_cli("graduate", "--state", state, "--fingerprint", fp,
                          "--out", os.path.join(out, "grad"))
        self.assertEqual(r2.returncode, 0)

        # 毕业实录可审计
        grads = H.read_jsonl(os.path.join(state, "graduations.jsonl"))
        self.assertTrue(any(g["scenario_id"] == "ac-AC-DEM-2" for g in grads))

    def test_verdict_unknown_fingerprint_rejected(self):
        state = H.tmpdir("patrol-verd-")
        H.run_patrol(state, os.path.join(state, "out"), "v-1", 42, "2026-08-22T03:43:00Z")
        r = H.patrol_cli("verdict", "--state", state, "--fingerprint", "sha256:deadbeef",
                         "--verdict", "reproduced")
        self.assertEqual(r.returncode, 3, "未开过单的指纹不接受判定（无单判定=空转）")


if __name__ == "__main__":
    unittest.main()
