#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""precision.py 自测（W2-C4 AC-3）：建议被后续修复（hit）/未修复（miss）双形态断言
+ bot 污染防御 + 阈值谓词。零网络零真实推理——fixture 输入。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import precision  # noqa: E402


def rec(pr, path, start, end, ts="2026-08-01T00:00:00Z", content="SQL injection risk"):
    return {"schema": "ocr-shadow/v1", "record": "suggestion", "ts": ts,
            "repo": "Cloudbird-Software/CI-Workflows", "pr": pr, "head_sha": "b" * 40,
            "run_id": "r1", "model": "glm-4.5-air",
            "suggestion": {"path": path, "start_line": start, "end_line": end,
                           "content": content, "rule_id": "injection"}}


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.write(json.dumps({"schema": "ocr-shadow/v1", "record": "summary", "stats": {}}, ensure_ascii=False) + "\n")


# 后续修复 diff：删除 src/handler.py 旧侧第 11 行（坏行删除也是修复——口径见模块 docstring）
FIX_DIFF_101 = """\
diff --git a/src/handler.py b/src/handler.py
--- a/src/handler.py
+++ b/src/handler.py
@@ -9,3 +9,3 @@
     ctx = get_ctx()
-    q = "SELECT * FROM t WHERE id=" + uid
+    q = build_query(uid)
     return run(q)
"""

TOUCH_OTHER_FILE = """\
diff --git a/src/other.py b/src/other.py
--- a/src/other.py
+++ b/src/other.py
@@ -3,2 +3,2 @@
-a
+b
 c
"""

BOT_FIX_DIFF_102 = """\
diff --git a/src/util.py b/src/util.py
--- a/src/util.py
+++ b/src/util.py
@@ -3,2 +3,2 @@
-old
+new
 ctx
"""


class TestPrecisionFixtures(unittest.TestCase):
    """双形态：hit（人工后续修复触及锚点）/ miss（无修复触及），外加 bot 防御与 pending。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        write_jsonl(os.path.join(d, "records.jsonl"), [
            rec(101, "src/handler.py", 11, 11),                       # 人工删除坏行 → hit
            rec(102, "src/util.py", 3, 4),                            # 仅 bot 触及锚点 → miss（防御）
            rec(103, "src/gone.py", 7, 7),                            # 无后续 commit → pending
        ])
        fu = os.path.join(d, "followups")
        os.makedirs(fu)
        with open(os.path.join(fu, "pr-101.json"), "w", encoding="utf-8") as f:
            json.dump({"pr": 101, "commits": [
                {"sha": "f" * 40, "author": "alice", "committed_at": "2026-08-03T00:00:00Z",
                 "diff": FIX_DIFF_101}]}, f)
        with open(os.path.join(fu, "pr-102.json"), "w", encoding="utf-8") as f:
            json.dump({"pr": 102, "commits": [
                {"sha": "1" * 40, "author": "dependabot[bot]", "committed_at": "2026-08-03T00:00:00Z",
                 "diff": BOT_FIX_DIFF_102},                       # bot 修复：不得计命中
                {"sha": "2" * 40, "author": "bob", "committed_at": "2026-08-04T00:00:00Z",
                 "diff": TOUCH_OTHER_FILE}]}, f)                  # bob 只改别的文件：不算
        cls.d = d
        cls.fu = fu

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_hit_and_miss_forms(self):
        records = precision.load_records([os.path.join(self.d, "records.jsonl")])
        self.assertEqual(len(records), 3)  # summary 行被忽略
        report = precision.evaluate(records, precision.load_followups_dir(self.fu), line_tol=3)
        by_pr = {p["pr"]: p for p in report["per_suggestion"]}
        self.assertTrue(by_pr[101]["matched"])                       # 双形态之一：被修复
        self.assertIsNotNone(by_pr[101]["hit_commit"])
        self.assertFalse(by_pr[102]["matched"])                      # 双形态之二：未修复（bot 防御生效）
        self.assertIsNone(by_pr[102]["hit_commit"])
        self.assertEqual(report["evaluated"], 2)
        self.assertEqual(report["pending_observation"], 1)
        self.assertEqual(report["hits"], 1)
        self.assertEqual(report["precision"], 0.5)
        self.assertFalse(report["promotion_ready"])                  # 0.5 < 0.8 且 2 < 30 例
        self.assertEqual(report["series"][0]["period"], "2026-08")   # 时序按月聚合
        self.assertEqual(report["series"][0]["cumulative_precision"], 0.5)

    def test_line_tolerance_absorbs_drift(self):
        """行漂移：修复落在锚点 ±tolerance 外不算命中（tolerance=0 时仅精确相交）。"""
        records = [rec(101, "src/handler.py", 20, 20)]               # 锚点在 20，修复 diff 触及 10-11
        report = precision.evaluate(records, precision.load_followups_dir(self.fu), line_tol=3)
        self.assertFalse(report["per_suggestion"][0]["matched"])

    def test_cli_roundtrip(self):
        out = os.path.join(self.d, "report.json")
        rc = precision.main(["--records", os.path.join(self.d, "records.jsonl"),
                             "--followups", self.fu, "--out", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["hits"], 1)
        # fail-closed：损坏 JSONL exit 2
        bad = os.path.join(self.d, "bad.jsonl")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{broken\n")
        self.assertEqual(precision.main(["--records", bad, "--followups", self.fu, "--out", out]), 2)


class TestPromotionThreshold(unittest.TestCase):
    """阈值谓词：precision≥0.8 且 ≥30 例才 promotion_ready（ADR-0063 决策 4）。"""

    def test_thresholds(self):
        fu = {1: [{"sha": "a" * 40, "author": "alice", "committed_at": "2026-08-05T00:00:00Z",
                   "diff": FIX_DIFF_101}]}
        hit = [rec(1, "src/handler.py", 11, 11, ts=f"2026-08-{i:02d}T00:00:00Z") for i in range(1, 31)]
        r = precision.evaluate(hit, fu, line_tol=3)
        self.assertEqual(r["evaluated"], 30)
        self.assertEqual(r["precision"], 1.0)
        self.assertTrue(r["promotion_ready"])                        # 1.0 ≥ 0.8 且 30 ≥ 30
        # 29 例即不足（fail-closed 方向：不足不晋升）
        r29 = precision.evaluate(hit[:29], fu, line_tol=3)
        self.assertFalse(r29["promotion_ready"])
        # precision 0.8 边界：30 例中 24 命中 = 0.8 恰达标
        mixed = hit[:24] + [rec(1, "src/nowhere.py", 1, 1, ts=f"2026-08-{i:02d}T00:00:00Z") for i in range(25, 31)]
        rm = precision.evaluate(mixed, fu, line_tol=3)
        self.assertEqual(rm["precision"], 0.8)
        self.assertTrue(rm["promotion_ready"])


if __name__ == "__main__":
    unittest.main()
