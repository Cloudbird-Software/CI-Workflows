#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""频控 + SNR 降频自测（W3-C2 AC-2/AC-4）：每小时/每日开单上限；信噪比低于
阈值自动降频（收敛到 downshift_daily_issue_cap）+ needs-human；窗口恢复自动
解除。零网络零真实 LLM。"""
import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _helpers as H  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import patrol  # noqa: E402

T0 = "2026-08-22T03:43:00Z"


def open_rec(state, i, ts):
    patrol.append_jsonl(state.fp, {"fingerprint": f"sha256:{i:064x}", "repo": H.REPO,
                                   "scenario_id": f"ac-X-{i}", "source": "ac-registry",
                                   "oracle": {"class": "crash"}, "payloads": [],
                                   "symptom": f"exit={i}", "target_service": "x",
                                   "ts": ts, "run_id": "seed-run", "issue_ref": "draft:x"})


class TestRateLimit(unittest.TestCase):
    def setUp(self):
        self.state = patrol.State(H.tmpdir("patrol-rl-"))
        self.policy = patrol.load_policy(H.POLICY)

    def test_hourly_cap(self):
        for i in range(self.policy["rate_limit"]["max_issues_per_repo_per_hour"]):
            open_rec(self.state, i, T0)
        ok, why = patrol.allow_open(self.state, self.policy, H.REPO, T0, None)
        self.assertFalse(ok)
        self.assertIn("hourly-cap", why)

    def test_hour_window_rolls(self):
        open_rec(self.state, 0, T0)
        t2 = (dt.datetime.strptime(T0, "%Y-%m-%dT%H:%M:%SZ") + dt.timedelta(hours=2)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        ok, _ = patrol.allow_open(self.state, self.policy, H.REPO, t2, None)
        self.assertTrue(ok, "小时窗口滚动后恢复可开（deferred 获得再攻击机会）")

    def test_run_level_throttle_defers(self):
        # 集成：上限 1 → 首 finding 开单、其余 deferred（频控在 run 级真实生效）
        state, out = H.tmpdir("patrol-rl2-"), H.tmpdir("patrol-rl2-out-")
        tight = os.path.join(state, "policy-tight.yaml")
        p = dict(self.policy)
        p["rate_limit"] = {"max_issues_per_repo_per_hour": 1, "max_issues_per_repo_per_day": 12}
        import yaml
        with open(tight, "w", encoding="utf-8", newline="\n") as f:
            yaml.safe_dump(p, f, allow_unicode=True, sort_keys=True)
        r = H.patrol_cli("run", "--policy", tight, "--state", state,
                         "--out", out, "--target-base", H.ROOT, "--repo", H.REPO,
                         "--run-id", "rl-1", "--seed", "42", "--clock", T0)
        self.assertEqual(r.returncode, 0, r.stderr)
        m = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(m["opened_this_run"], 1)
        self.assertGreater(len(m["deferred"]), 0)
        self.assertTrue(all("cap" in d["reason"] for d in m["deferred"]))


class TestDownshift(unittest.TestCase):
    def _seed_low_snr(self, state, n, reproduced=0):
        # 造窗口：n 张已开单、reproduced 张确认 → snr=reproduced/n
        for i in range(n):
            open_rec(state, 1000 + i, T0)
        for i in range(reproduced):
            patrol.append_jsonl(state.verd, {"fingerprint": f"sha256:{1000 + i:064x}",
                                             "verdict": "reproduced",
                                             "ts": T0, "by": "t"})
        for _ in range(3):  # run 分母
            patrol.append_jsonl(state.runs, {"run_id": "r", "ts": T0, "repo": H.REPO,
                                             "probes": 1, "opened": 0, "seed": 1})

    def test_low_snr_trips_downshift(self):
        state = patrol.State(H.tmpdir("patrol-ds-"))
        policy = patrol.load_policy(H.POLICY)
        self._seed_low_snr(state, policy["yield"]["snr_window_issues"], reproduced=0)
        met = patrol.compute_metrics(state, policy)
        self.assertEqual(met["snr"], 0.0)
        ds = patrol.maybe_downshift(state, policy, met)
        self.assertIsNotNone(ds)
        self.assertTrue(ds["active"] and ds["needs_human"], "低信噪比 → 降频 + needs-human")
        # 降频态收敛开单面
        for i in range(5):
            open_rec(state, 2000 + i, "2026-08-22T06:00:00Z")
        ok, why = patrol.allow_open(state, policy, H.REPO, "2026-08-22T06:30:00Z", ds)
        self.assertFalse(ok, "降频态每日 1 单上限收紧")
        self.assertIn("cap", why)

    def test_recovery_unsets_downshift(self):
        state = patrol.State(H.tmpdir("patrol-ds2-"))
        policy = patrol.load_policy(H.POLICY)
        n = policy["yield"]["snr_window_issues"]
        self._seed_low_snr(state, n, reproduced=0)
        ds = patrol.maybe_downshift(state, policy, patrol.compute_metrics(state, policy))
        self.assertIsNotNone(ds)
        # 全部翻正为 reproduced → 窗口恢复 → 自动解除（数据驱动调参，ADR-0065 决策 6）
        for i in range(n):
            patrol.append_jsonl(state.verd, {"fingerprint": f"sha256:{1000 + i:064x}",
                                             "verdict": "reproduced", "ts": T0, "by": "t"})
        ds2 = patrol.maybe_downshift(state, policy, patrol.compute_metrics(state, policy))
        self.assertIsNone(ds2)

    def test_insufficient_window_no_trip(self):
        state = patrol.State(H.tmpdir("patrol-ds3-"))
        policy = patrol.load_policy(H.POLICY)
        self._seed_low_snr(state, n=3, reproduced=0)  # 窗口样本不足——不武断降频
        met = patrol.compute_metrics(state, policy)
        self.assertLess(met["snr_window"]["opened"], policy["yield"]["snr_window_issues"])
        self.assertIsNone(patrol.maybe_downshift(state, policy, met))


if __name__ == "__main__":
    unittest.main()
