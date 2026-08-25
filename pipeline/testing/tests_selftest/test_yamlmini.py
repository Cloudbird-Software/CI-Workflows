"""_yamlmini：YAML 子集解析/回写（自检项「YAML 解析过」的执法测试）。"""
import unittest
from pathlib import Path

from pipeline.testing import _yamlmini as y

ROOT = Path(__file__).resolve().parents[1]


class TestYamlMiniDataFiles(unittest.TestCase):
    def test_catalog_yaml_parses(self):
        doc = y.load(ROOT / "pipeline/testing/metamorphic/catalog.yaml")
        self.assertEqual(len(doc["relations"]), 15)
        statuses = {r["status"] for r in doc["relations"]}
        self.assertEqual(statuses, {"candidate", "implemented", "rejected"})
        implemented = [r for r in doc["relations"] if r["status"] == "implemented"]
        self.assertGreaterEqual(len(implemented), 3)
        for entry in doc["relations"]:
            for field in ("id", "relation", "verify", "applies_to", "status"):
                self.assertIn(field, entry, "条目 %s 缺 %s" % (entry.get("id"), field))

    def test_checklist_yaml_parses(self):
        doc = y.load(ROOT / "pipeline/testing/formal/checklist.yaml")
        items = doc["items"]
        self.assertEqual(len(items), 8)
        self.assertEqual(sum(1 for i in items if i["kind"] == "positive"), 4)
        self.assertEqual(sum(1 for i in items if i["kind"] == "negative"), 4)
        self.assertEqual(doc["risk_gate"]["field"], "risk_level")

    def test_ledger_fixture_yaml_parses(self):
        doc = y.load(ROOT / "pipeline/testing/sast/fixtures/ledger-demo.yaml")
        self.assertEqual(len(doc["entries"]), 3)
        self.assertTrue(all(e["chain_sha"] for e in doc["entries"]))

    def test_github_workflow_yaml_parses(self):
        doc = y.load(ROOT / ".github/workflows/quality-instruments.yml")
        self.assertEqual(doc["on"]["workflow_call"]["inputs"]["instrument"]["type"], "string")
        self.assertEqual(doc["on"]["schedule"][0]["cron"], "43 6 * * 1")
        self.assertIn("dispatch", doc["jobs"])
        self.assertIn("weekly-sast-sweep", doc["jobs"])
        self.assertEqual(doc["permissions"]["contents"], "read")
        self.assertEqual(doc["jobs"]["dispatch"]["timeout-minutes"], 20)


class TestYamlMiniSemantics(unittest.TestCase):
    def test_scalars_and_nested_containers(self):
        doc = y.loads(
            "a: 1\n"
            'b: "x: y"\n'
            "c: true\n"
            "d: null\n"
            "e:\n  - 1\n  - two\n"
            "f: [x, y]\n"
            "g: {k: 1, j: false}\n"
            "h: 'it''s'\n"
        )
        self.assertEqual(doc["a"], 1)
        self.assertEqual(doc["b"], "x: y")
        self.assertIs(doc["c"], True)
        self.assertIs(doc["d"], None)
        self.assertEqual(doc["e"], [1, "two"])
        self.assertEqual(doc["f"], ["x", "y"])
        self.assertEqual(doc["g"], {"k": 1, "j": False})
        self.assertEqual(doc["h"], "it's")

    def test_block_scalar(self):
        text = "run: |\n    echo hi\n    echo bye\n"
        self.assertEqual(y.loads(text)["run"], "echo hi\necho bye\n")

    def test_round_trip_dump_load(self):
        data = {"version": 1, "entries": [], "nested": {"k": "va: l#ue", "n": None}}
        self.assertEqual(y.loads(y.dump(data)), data)


if __name__ == "__main__":
    unittest.main()
