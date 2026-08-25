"""pilot：符号执行试点评估器（AC-5）——报告字段齐全 + reject 带 revisit_when。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline.testing.symbolic import pilot

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests" / "fixtures" / "symbolic_target.py"


class TestPilot(unittest.TestCase):
    def test_static_metrics_mechanical_counts(self):
        report = pilot.build_report(TARGET, force_proxy=True)
        m = report["metrics"]
        self.assertEqual(m["functions"], 3)
        self.assertEqual(m["branches"], 7)       # clamp 2 + batch_total 2 + pick 3
        self.assertEqual(m["max_loop_depth"], 1)
        self.assertEqual(m["path_upper_bound"], 2 ** 2 * 2 ** 2 * 2 ** 3)

    def test_proxy_report_fields_complete_and_reject(self):
        report = pilot.build_report(TARGET, force_proxy=True)
        self.assertTrue(report["proxy"])
        self.assertEqual(report["tool"], "static-approx")
        self.assertIsNone(report["metrics"]["solver_timeout_rate"])   # proxy 模式显式不可测
        decision = report["conclusion"]["decision"]
        self.assertIn(decision, ("adopt", "reject"))                  # 二值约束
        self.assertEqual(decision, "reject")
        self.assertTrue(report["conclusion"]["revisit_when"])         # reject 必带
        self.assertIn("pilot.py", report["recompute_command"])        # ADR-0085 复算锚点
        self.assertIn("--target", report["recompute_command"])

    def test_risky_function_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 12 个判定节点 → 4096 条路径 > 200 预算 → risky
            path = Path(tmp) / "wide.py"
            path.write_text(
                "def wide(a):\n"
                + "".join("    if a > %d:\n        a += 1\n" % i for i in range(12))
                + "    return a\n",
                encoding="utf-8",
            )
            report = pilot.build_report(path, force_proxy=True)
            self.assertEqual(report["metrics"]["risky_functions_gt_budget"], 1)
            self.assertLess(report["metrics"]["path_coverage_estimate"], 1.0)
            self.assertGreater(report["metrics"]["findings_per_second"], 0.0)

    def test_cli_writes_markdown_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "pipeline/testing/symbolic/pilot.py"),
                 "--target", str(TARGET), "--out-dir", tmp, "--force-proxy"],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            json_reports = list(Path(tmp).glob("pilot-report-*.json"))
            md_reports = list(Path(tmp).glob("pilot-report-*.md"))
            self.assertEqual(len(json_reports), 1)
            self.assertEqual(len(md_reports), 1)
            doc = json.loads(json_reports[0].read_text(encoding="utf-8"))
            self.assertIn(doc["conclusion"]["decision"], ("adopt", "reject"))
            md = md_reports[0].read_text(encoding="utf-8")
            for section in ("1. 证据", "2. 指标", "3. 结论", "复算命令"):
                self.assertIn(section, md)

    def test_pynguin_probe_never_raises(self):
        ok, _ = pilot.probe_pynguin(timeout=5)
        self.assertIsInstance(ok, bool)


if __name__ == "__main__":
    unittest.main()
