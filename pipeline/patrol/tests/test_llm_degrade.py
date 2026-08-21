#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 源降级路径自测（W3-C2 卡面：无凭据时该源降级跳过并计数，不伪装）：
- 无 LLM_API_KEY 且无回放 → llm_status=skipped-no-creds，零前沿场景伪造，
  metamorphic 半源（确定性）照常运行
- 离线回放（零真实 LLM）→ 前沿场景生成 + 经 metering wrapper 计量落链
  （W2-C3 ADR-0062 一次 invoke 恰一条记录——验链断言）"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _helpers as H  # noqa: E402

T0 = "2026-08-22T03:43:00Z"


class TestLLMDegrade(unittest.TestCase):
    def test_no_creds_honest_skip(self):
        state, out = H.tmpdir("patrol-llm-"), H.tmpdir("patrol-llm-out-")
        m = H.run_patrol(state, os.path.join(out, "r1"), "l-1", 42, T0, replay=None)
        self.assertEqual(m["llm_status"], "skipped-no-creds")
        # 只剩 metamorphic 两个确定性场景——没有伪造的 LLM 前沿场景
        self.assertEqual(m["scenarios"]["llm-metamorphic"], 2)
        self.assertEqual(m["observations_seen"], 0, "无 LLM 输出就没有 observation")

    def test_replay_offline_generation_and_metering(self):
        state, out = H.tmpdir("patrol-llm2-"), H.tmpdir("patrol-llm2-out-")
        m = H.run_patrol(state, os.path.join(out, "r1"), "l-2", 42, T0)
        self.assertEqual(m["llm_status"], "ok")
        self.assertEqual(m["scenarios"]["llm-metamorphic"], 3,
                         "metamorphic×2 + llm-frontier×1")
        self.assertEqual(m["observations_seen"], 1)
        self.assertFalse(m["observations_escalated"], "首次出现只进桶不升级")
        # metering 账本验链（W2-C3 wrapper 约定合规：一次 invoke 恰一条 + hash 链）
        ledger = os.path.join(state, "metering")
        files = [f for f in os.listdir(ledger) if f.startswith("records-")]
        self.assertEqual(sum(1 for f in files
                             for _ in H.read_jsonl(os.path.join(ledger, f))), 1,
                         "一次 invoke 恰一条计量记录")
        metering_verify = os.path.join(H.ROOT, "pipeline", "metering", "metering.py")
        import subprocess
        v = subprocess.run([sys.executable, metering_verify, "verify", "--dir", ledger],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(v.returncode, 0, v.stderr)
        self.assertIn("链完整", v.stdout)

    def test_frontier_payloads_probed_not_trusted(self):
        # 回放里的 div(a="x") 崩溃 payload 是探针输入不是真相——照打（INV-10 精神）；
        # 频控下可能 deferred，但必须成为 finding（开单或入延迟队列，不允许静默丢弃）
        state, out = H.tmpdir("patrol-llm3-"), H.tmpdir("patrol-llm3-out-")
        m = H.run_patrol(state, os.path.join(out, "r1"), "l-3", 42, T0)
        seen = {o["scenario"] for o in m["opened"]} | {d["scenario"] for d in m["deferred"]}
        self.assertIn("llm-frontier", seen)


if __name__ == "__main__":
    unittest.main()
