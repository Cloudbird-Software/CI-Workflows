#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 端到端链路自测（W5-C2 AC-1/2/4，ADR-0071）。

fixture 驱动零网络：adjudicate（缺证据 exit 1 + 列缺失键）→ shadow-record
（JSONL 落盘 executed=false）→ reconcile → unlock-evaluate（50 例一致+陷阱达阈
→ unlocked 事件+新状态文件；逃逸流 → locked）→ sample。退出码三分约定钉死。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trust_gate  # noqa: E402

from _helpers import all_ev, bundle, reconciled, run_cli, write_jsonl  # noqa: E402


class TestCliChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trust-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def w(self, name: str, obj) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False)
        return path

    def test_full_chain_unlock_and_shadow_record(self):
        # 1) adjudicate：证据全绿（锁定域）→ would-merge exit 0
        d_ok = self.w("d-ok.json", bundle(pr=42))
        cp = run_cli(["adjudicate", "--bundle", d_ok, "--out", os.path.join(self.tmp, "v1.json")],
                     expect_rc={0})
        v = json.loads(cp.stdout)
        self.assertEqual(v["decision"], "would-merge")

        # 2) adjudicate：缺负控制记录 → exit 1 且 stderr 列出该谓词键（AC-1 e2e）
        ev = all_ev("docs-only")
        del ev["evidence.negative-control"]
        d_miss = self.w("d-miss.json", bundle(pr=43, evidence=ev))
        cp = run_cli(["adjudicate", "--bundle", d_miss, "--out",
                      os.path.join(self.tmp, "v2.json")], expect_rc={1})
        self.assertIn("evidence.negative-control", cp.stderr)
        self.assertIn("缺证据=拒绝", cp.stderr)

        # 3) shadow-record：判定落 JSONL（executed=false——AC-2 不执行）
        out_dir = os.path.join(self.tmp, "trust-shadow")
        run_cli(["shadow-record", "--decision", os.path.join(self.tmp, "v1.json"),
                 "--out-dir", out_dir, "--repo", "ORG/REPO", "--pr", "42",
                 "--head-sha", "f" * 40, "--run-id", "r1", "--event", "pull_request",
                 "--date", "2026-08-22"], expect_rc={0})
        jsonl = os.path.join(out_dir, "2026-08-22.jsonl")
        with open(jsonl, encoding="utf-8") as f:
            rec = json.loads(f.read().splitlines()[0])
        self.assertEqual(rec["record"], "decision")
        self.assertEqual(rec["decision"], "would-merge")
        self.assertFalse(rec["executed"])

        # 4) reconcile：50 例一致流（10 条陷阱=20%）× owner 裁决
        decisions, rulings = [], []
        for i in range(50):
            trap = (i + 1) % 5 == 0
            decisions.append({"schema": "trust-shadow/v1", "record": "decision",
                              "ts": f"2026-08-22T00:{i:02d}:00Z", "repo": "ORG/REPO",
                              "pr": 100 + i, "head_sha": "f" * 40, "run_id": "r",
                              "event": "pull_request", "domain": "docs-only",
                              "mode": "shadow",
                              "decision": "would-reject" if trap else "would-merge",
                              "reason": "x", "missing_predicates": [],
                              "required_predicates": [], "breaker_tripped": False,
                              "trap": trap, "sample_review": False, "executed": False})
            rulings.append({"schema": "trust-shadow/v1", "record": "ruling",
                            "ts": f"2026-08-22T12:{i:02d}:00Z", "repo": "ORG/REPO",
                            "pr": 100 + i, "ruling": "closed" if trap else "merged",
                            "by": "randypanding"})
        run_cli(["reconcile", "--decisions", write_jsonl(os.path.join(self.tmp, "dec.jsonl"), decisions),
                 "--rulings", write_jsonl(os.path.join(self.tmp, "rul.jsonl"), rulings),
                 "--out", os.path.join(self.tmp, "rec.jsonl"),
                 "--summary", os.path.join(self.tmp, "sum.json")], expect_rc={0})
        with open(os.path.join(self.tmp, "sum.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["per_domain"]["docs-only"]["agreements"], 50)

        # 5) unlock-evaluate：50 一致+零逃逸+陷阱 20% → unlocked + 新状态文件
        run_cli(["unlock-evaluate", "--reconcile", os.path.join(self.tmp, "rec.jsonl"),
                 "--out-events", os.path.join(self.tmp, "ev.jsonl"),
                 "--summary", os.path.join(self.tmp, "unlock.json"),
                 "--write-state", os.path.join(self.tmp, "new-state.yaml")], expect_rc={0})
        with open(os.path.join(self.tmp, "unlock.json"), encoding="utf-8") as f:
            u = json.load(f)["domains"]["docs-only"]
        self.assertEqual(u["status_after"], "unlocked")
        with open(os.path.join(self.tmp, "new-state.yaml"), encoding="utf-8") as f:
            self.assertIn("docs-only:\n    status: unlocked", f.read())
        with open(os.path.join(self.tmp, "ev.jsonl"), encoding="utf-8") as f:
            self.assertTrue(any(json.loads(ln)["event"] == "unlocked" for ln in f))

    def test_escape_stream_stays_locked(self):
        recs = [reconciled(pr=i + 1, domain="docs-only", shadow="would-merge",
                           owner_ruling="merged" if i < 49 else "closed",
                           ts=f"2026-08-22T00:{i:02d}:00Z") for i in range(50)]
        run_cli(["unlock-evaluate", "--reconcile", write_jsonl(os.path.join(self.tmp, "rec.jsonl"), recs),
                 "--out-events", os.path.join(self.tmp, "ev.jsonl"),
                 "--summary", os.path.join(self.tmp, "u.json")], expect_rc={0})
        with open(os.path.join(self.tmp, "u.json"), encoding="utf-8") as f:
            u = json.load(f)["domains"]["docs-only"]
        self.assertEqual(u["status_after"], "locked")   # 逃逸 → 不解锁
        self.assertEqual(u["streak"], 0)

    def test_infra_exit_codes(self):
        # 坏 bundle（缺键）→ exit 2（infra，非拒绝非放行）
        bad = self.w("bad.json", {"repo": "R"})
        cp = run_cli(["adjudicate", "--bundle", bad], expect_rc={2})
        self.assertIn("infra", cp.stderr)
        # 坏 predicates.yaml → exit 2
        with open(os.path.join(self.tmp, "p.yaml"), "w", encoding="utf-8") as f:
            f.write("schema: wrong/v9\n")
        run_cli(["adjudicate", "--bundle", self.w("ok.json", bundle()),
                 "--predicates", os.path.join(self.tmp, "p.yaml")], expect_rc={2})
        # breaker 置位 → exit 1（拒绝，不是 infra）
        run_cli(["adjudicate", "--bundle", self.w("brk.json", bundle(breaker=True))],
                expect_rc={1})

    def test_sample_cli(self):
        cp = run_cli(["sample", "--prs", "1,2,3,4,5,6,7,8,9,10", "--domain", "docs-only",
                      "--seed", "s1", "--rate", "0.2", "--out",
                      os.path.join(self.tmp, "s.json")], expect_rc={0})
        r = json.loads(cp.stdout)
        self.assertEqual(r["seed"], "s1")
        with open(os.path.join(self.tmp, "s.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["selected"], r["selected"])


if __name__ == "__main__":
    unittest.main()
