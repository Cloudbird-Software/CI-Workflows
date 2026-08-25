"""cnb-drill.py 测试：静态模式对 fixture 仓树（脏树报红、净树报绿、缺 REMOVAL 报红）。"""
import contextlib
import importlib.util
import io
import json
import unittest

from tests.testlib import BUILD_C

_spec = importlib.util.spec_from_file_location(
    "cnb_drill", str(BUILD_C / "drill" / "cnb-drill.py"))
cnb_drill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cnb_drill)

FIXTURES = BUILD_C / "tests" / "fixtures"


class DrillStaticTests(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cnb_drill.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_clean_tree_green(self):
        code, out, err = self.run_main("--mode", "static",
                                       "--repo-root", str(FIXTURES / "drill-clean"))
        report = json.loads(out)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(report["verdict"], "green")
        self.assertEqual(report["violations"], [])
        self.assertTrue(report["removal_md_present"])
        for seam in ("cnb-dispatch.yml", "cnb-audit.yml", "expected-state.json",
                     "automation-limits.yaml", "providers.yaml", "REMOVAL.md"):
            self.assertIn(seam, report["impacted_files"])
        self.assertTrue(report["dry_run"])
        self.assertIn("duration_ms", report)

    def test_dirty_tree_red(self):
        code, out, _ = self.run_main("--mode", "static",
                                     "--repo-root", str(FIXTURES / "drill-dirty"))
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(report["verdict"], "red")
        files = {v["file"] for v in report["violations"]}
        self.assertIn("src/leak.py", files)       # 三接缝外操作性引用
        self.assertIn("GOVERNANCE.yaml", files)   # EX-1 节外的引用
        gov = next(v for v in report["violations"] if v["file"] == "GOVERNANCE.yaml")
        self.assertIn("EX-1", gov["reason"])

    def test_missing_removal_md_red(self):
        code, out, _ = self.run_main("--mode", "static",
                                     "--repo-root", str(FIXTURES / "drill-noremoval"))
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(report["removal_md_present"])
        self.assertEqual(report["violations"], [])  # 引用干净，仅缺 REMOVAL.md

    def test_static_requires_repo_root(self):
        code, _, err = self.run_main("--mode", "static")
        self.assertEqual(code, 1)
        self.assertIn("--repo-root", err)

    def test_functional_outputs_runbook_without_executing(self):
        code, out, err = self.run_main("--mode", "functional")
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertFalse(report["executed"])
        self.assertGreaterEqual(len(report["runbook"]), 5)
        self.assertIn("CNB_DISABLED", json.dumps(report, ensure_ascii=False))
        self.assertIn("runbook:", err)


if __name__ == "__main__":
    unittest.main()
