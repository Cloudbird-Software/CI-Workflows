"""trigger：形式化条件触发（AC-7）——fail-closed + 机械字段优先。"""
import subprocess
import sys
import unittest
from pathlib import Path

from pipeline.testing import _yamlmini
from pipeline.testing.formal import trigger

ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = ROOT / "pipeline/testing/formal/checklist.yaml"
FIXTURES = ROOT / "tests" / "fixtures"
TRIGGER_SCRIPT = ROOT / "pipeline/testing/formal/trigger.py"


class TestTrigger(unittest.TestCase):
    def test_positive_sample_applicable(self):
        meta = _yamlmini.load(FIXTURES / "meta-applicable.yaml")
        verdict = trigger.judge(meta, checklist_path=CHECKLIST)
        self.assertEqual(verdict["final"], "applicable")
        self.assertEqual(verdict["risk_level"], "high")
        matched = {r["id"] for r in verdict["items"] if r["matched"]}
        self.assertIn("F-POS-1", matched)   # spec.math_definition == true
        self.assertIn("F-POS-4", matched)   # high + loc<=500 + commits<=5
        self.assertNotIn("F-NEG-1", matched)
        # 语义填充位留痕：一个已填、一个待填
        statuses = {t["field"]: t["status"] for t in verdict["fill_trace"]}
        self.assertEqual(statuses["spec.algorithm_definition_ref"], "filled")
        self.assertEqual(statuses["spec.state_machine_diagram_ref"], "pending")

    def test_missing_risk_level_fail_closed(self):
        meta = _yamlmini.load(FIXTURES / "meta-norisk.yaml")
        verdict = trigger.judge(meta, checklist_path=CHECKLIST)
        # 即使有 math_definition=true 正证据，risk_level 缺失仍 fail-closed
        self.assertEqual(verdict["final"], "needs_risk_level")
        self.assertIsNone(verdict["risk_level"])
        proc = subprocess.run(
            [sys.executable, str(TRIGGER_SCRIPT), "--meta", str(FIXTURES / "meta-norisk.yaml")],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 3)   # fail-closed 信号

    def test_glue_not_applicable(self):
        meta = _yamlmini.load(FIXTURES / "meta-glue.yaml")
        verdict = trigger.judge(meta, checklist_path=CHECKLIST)
        self.assertEqual(verdict["final"], "not_applicable")
        matched = [r["id"] for r in verdict["items"] if r["matched"]]
        self.assertIn("F-NEG-1", matched)     # code_kind == glue
        self.assertNotIn("F-POS-4", matched)  # risk low

    def test_missing_source_field_not_matched_and_traced(self):
        verdict = trigger.judge({"id": "X", "risk_level": "low"}, checklist_path=CHECKLIST)
        self.assertEqual(verdict["final"], "not_applicable")
        unevaluated = [r for r in verdict["items"] if not r["evaluated"]]
        self.assertTrue(unevaluated)
        self.assertTrue(all(not r["matched"] for r in unevaluated))
        self.assertTrue(all("缺失" in r["reason"] for r in unevaluated))


if __name__ == "__main__":
    unittest.main()
