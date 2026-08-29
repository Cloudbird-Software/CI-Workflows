#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""postprocess.py 自测（W2-C4 AC-2）：越界丢弃/规则未中丢弃/去重三形态 + 正常保留。
零网络零真实推理——全部 fixture 输入。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import postprocess  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(HERE, "..", "rules.yaml")

# PR diff fixture：新侧新增行 = src/handler.py {11,12,13}（10 为 context）
DIFF = """\
diff --git a/src/handler.py b/src/handler.py
index 111..222 100644
--- a/src/handler.py
+++ b/src/handler.py
@@ -10,3 +10,5 @@ def old():
     ctx = get_ctx()
-    q = "SELECT * FROM t WHERE id=" + uid
+    q = build_query(uid)
+    token = "FIXTURE-REDACTED"
+    return run(q)
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,1 @@
-old
+new
"""


def comments(*items):
    return {"status": "success", "llm": {"provider": "z-ai", "model": "glm-4.5-air"}, "comments": list(items)}


class TestDiffParse(unittest.TestCase):
    def test_added_lines(self):
        added = postprocess.parse_unified_diff(DIFF)
        self.assertEqual(added["src/handler.py"], {11, 12, 13})
        self.assertEqual(added["README.md"], {1})

    def test_malformed_hunk_fail_closed(self):
        with self.assertRaises(ValueError):
            postprocess.parse_unified_diff("+++ b/x.py\n@@ garbage @@\n+line\n")

    def test_pure_deletion_diff_skips_dev_null_hunks(self):
        """纯删除 diff（+++ /dev/null 后跟 hunk 头）不炸——PR #131 实测回归。

        docstring 声称"删除文件跳过"，但旧实现把 path=None 与不可解析合并
        raise：删除文件的 hunk 头必炸。修复后应跳过且无新增行锚点。
        """
        added = postprocess.parse_unified_diff(
            "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
            "@@ -1,9 +0,0 @@\n-line1\n-line2\n"
            "diff --git a/keep.py b/keep.py\n--- a/keep.py\n+++ b/keep.py\n"
            "@@ -1,1 +1,2 @@\n ctx\n+new\n"
        )
        self.assertNotIn("gone.py", added)
        self.assertEqual(added["keep.py"], {2})


class TestPostprocess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = postprocess.load_rules(os.path.normpath(RULES))
        cls.added = postprocess.parse_unified_diff(DIFF)

    def run_pp(self, doc):
        return postprocess.postprocess(doc, self.added, self.rules)

    def test_keep_normal(self):
        """正常保留：锚点落 diff 新增行 + 命中规则集。"""
        kept, stats = self.run_pp(comments(
            {"path": "src/handler.py", "start_line": 11, "end_line": 11,
             "content": "SQL injection: query built by string concatenation of user input"}))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["rule_id"], "injection")
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(stats["dropped_by_reason"], {"outside-diff": 0, "no-rule-hit": 0, "duplicate": 0})
        self.assertEqual(stats["drop_rate"], 0.0)

    def test_drop_outside_diff(self):
        """越界丢弃：文件不在 diff / 行不在新增行集合 / 无行号锚点。"""
        kept, stats = self.run_pp(comments(
            {"path": "src/other.py", "start_line": 5, "end_line": 5, "content": "SQL injection risk"},
            {"path": "src/handler.py", "start_line": 15, "end_line": 15, "content": "SQL injection risk"},
            {"path": "src/handler.py", "content": "SQL injection risk, no line"}))
        self.assertEqual(kept, [])
        self.assertEqual(stats["dropped_by_reason"]["outside-diff"], 3)
        self.assertEqual(stats["kept"], 0)

    def test_drop_no_rule_hit(self):
        """规则未中丢弃：锚点在 diff 内但类别游离于规则表。"""
        kept, stats = self.run_pp(comments(
            {"path": "src/handler.py", "start_line": 12, "end_line": 12,
             "content": "这个变量命名不够清晰，建议改为更具体的名字"}))
        self.assertEqual(kept, [])
        self.assertEqual(stats["dropped_by_reason"]["no-rule-hit"], 1)

    def test_drop_duplicate(self):
        """去重丢弃：同 (path, start_line, rule_id) 只留首条。"""
        kept, stats = self.run_pp(comments(
            {"path": "src/handler.py", "start_line": 11, "end_line": 11,
             "content": "SQL injection via concatenation"},
            {"path": "src/handler.py", "start_line": 11, "end_line": 13,
             "content": "Command injection risk in query building too"}))
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats["dropped_by_reason"]["duplicate"], 1)

    def test_rule_categories(self):
        """规则表类别命中抽查（hardcoded-secret / concurrency / error-handling）。"""
        for line, content, want in [
            (12, 'hardcoded secret: token = "FIXTURE-REDACTED" committed to source', "hardcoded-secret"),
            (13, "data race: concurrent map access without mutex", "concurrency"),
            (11, "swallows the error: empty catch ignores failure", "error-handling"),
        ]:
            kept, _ = self.run_pp(comments(
                {"path": "src/handler.py", "start_line": line, "end_line": line, "content": content}))
            self.assertEqual([k["rule_id"] for k in kept], [want], content)

    def test_skipped_status_passthrough(self):
        """N/A 诚实降级：status=skipped 全零统计，不伪装成评审运行。"""
        kept, stats = self.run_pp({"status": "skipped", "message": "N/A（无凭据，跳过）"})
        self.assertEqual(kept, [])
        self.assertEqual(stats["ocr_status"], "skipped")
        self.assertEqual(stats["total"], 0)


class TestCli(unittest.TestCase):
    """CLI 端到端（含 fail-closed 退出码）。"""

    def test_cli_roundtrip_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            oj, df, out, st = (os.path.join(d, n) for n in ("o.json", "p.diff", "k.json", "s.json"))
            with open(oj, "w", encoding="utf-8") as f:
                json.dump(comments({"path": "README.md", "start_line": 1, "end_line": 1,
                                    "content": 'password: "FIXTURE-REDACTED" hardcoded in docs'}), f)
            with open(df, "w", encoding="utf-8") as f:
                f.write(DIFF)
            rc = postprocess.main(["--ocr-json", oj, "--diff", df, "--rules",
                                   os.path.normpath(RULES), "--out", out, "--stats-file", st, "--ocr-rc", "0"])
            self.assertEqual(rc, 0)
            with open(st, encoding="utf-8") as f:
                stats = json.load(f)
            self.assertEqual(stats["kept"], 1)  # README.md:1 落 diff + hardcoded-secret 命中
            # fail-closed：输入损坏 exit 2（而非静默计 0）
            with open(oj, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(postprocess.main(["--ocr-json", oj, "--diff", df, "--rules",
                                               os.path.normpath(RULES), "--out", out, "--stats-file", st]), 2)


if __name__ == "__main__":
    unittest.main()
