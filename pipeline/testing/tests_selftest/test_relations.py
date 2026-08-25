"""relations：三条 implemented 蜕变关系正例 + 反例必须被检出（AC-4）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline.testing.metamorphic import relations

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "pipeline/testing/metamorphic/catalog.yaml"
FIXTURES = ROOT / "tests" / "fixtures"


class TestRelations(unittest.TestCase):
    def test_catalog_contract(self):
        rels = relations.load_catalog(CATALOG)
        self.assertGreaterEqual(len(rels), 15)
        implemented = relations.implemented_ids(rels)
        self.assertGreaterEqual(len(implemented), 3)
        # catalog 标记 implemented 的每条在 CHECKS 有实现（契约一致）
        self.assertEqual(set(implemented), set(relations.CHECKS))

    def test_good_case_all_pass(self):
        case = json.loads((FIXTURES / "relations-case-good.json").read_text(encoding="utf-8"))
        report = relations.run_case(relations.load_catalog(CATALOG), case)
        self.assertEqual(report["summary"], {"pass": 3, "fail": 0, "error": 0, "skipped": 0})

    def test_counterexamples_detected(self):
        case = json.loads((FIXTURES / "relations-case-bad.json").read_text(encoding="utf-8"))
        report = relations.run_case(relations.load_catalog(CATALOG), case)
        by_id = {r["id"]: r for r in report["results"]}
        self.assertEqual(by_id["MR-001"]["status"], "fail")   # 结果集不等价
        self.assertEqual(by_id["MR-002"]["status"], "fail")   # 重试不幂等
        self.assertEqual(by_id["MR-003"]["status"], "fail")   # 40+59 != 100
        self.assertEqual(report["summary"]["fail"], 3)

    def test_precondition_violation_is_error(self):
        rels = relations.load_catalog(CATALOG)
        report = relations.run_case(
            rels,
            {"MR-001": {"input_before": [1, 2], "input_after": [1, 3],  # 非重排
                        "output_before": [1], "output_after": [1]}},
        )
        self.assertEqual(report["results"][0]["status"], "error")

    def test_cli_exit_codes(self):
        good = FIXTURES / "relations-case-good.json"
        bad = FIXTURES / "relations-case-bad.json"
        with tempfile.TemporaryDirectory() as tmp:
            bad_copy = Path(tmp) / "bad.json"
            bad_copy.write_text(bad.read_text(encoding="utf-8"), encoding="utf-8")
            ok_proc = subprocess.run(
                [sys.executable, str(ROOT / "pipeline/testing/metamorphic/relations.py"),
                 "--catalog", str(CATALOG), "--case", str(good)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(ok_proc.returncode, 0, ok_proc.stderr)
            bad_proc = subprocess.run(
                [sys.executable, str(ROOT / "pipeline/testing/metamorphic/relations.py"),
                 "--catalog", str(CATALOG), "--case", str(bad_copy)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(bad_proc.returncode, 1)   # 反例 → 非零退出码


if __name__ == "__main__":
    unittest.main()
