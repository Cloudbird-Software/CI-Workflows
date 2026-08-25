"""diffbench.py 测试：硬区红（exit 2）/软区绿（exit 0 带警告）/输入缺失 exit 1。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.testlib import SHA_A, make_entry, registry_text
from oracle import diffbench as db


class DiffbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.regp = root / "registry.yaml"
        self.champ = root / "champion.jsonl"
        self.oracle = root / "oracle.jsonl"
        self.ledger = root / "ledger.json"
        self.regp.write_text(registry_text([make_entry(sha=SHA_A)]),
                             encoding="utf-8", newline="\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, extra=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = db.main(["--registry", str(self.regp),
                            "--champion-out", str(self.champ),
                            "--oracle-out", str(self.oracle)] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def write_cases(self, path, pairs):
        path.write_text("".join(
            json.dumps({"case": c, "output": o}, ensure_ascii=False) + "\n"
            for c, o in pairs), encoding="utf-8", newline="\n")

    def test_hard_zone_divergence_exit_2(self):
        self.write_cases(self.champ, [("case/hard/a", "1"), ("case/soft/x", "s")])
        self.write_cases(self.oracle, [("case/hard/a", "2"), ("case/soft/x", "s")])
        code, out, err = self.run_main(["--zones"])
        self.assertEqual(code, 2)
        self.assertIn(db.HARD_ZONE_MARKER, err)
        self.assertIn("路由裁决", err)
        report = json.loads(out)
        self.assertEqual(report["verdicts"]["overall"], "hard_divergence")
        self.assertEqual(report["verdicts"]["hard_zone"], "divergent")

    def test_hard_zone_missing_case_exit_2(self):
        self.write_cases(self.champ, [("case/hard/a", "1")])
        self.write_cases(self.oracle, [])  # oracle 未覆盖硬区案例
        self.champ.write_text(self.champ.read_text(encoding="utf-8"), encoding="utf-8")  # 保持非空
        self.oracle.write_text('{"case": "case/soft/x", "output": "s"}\n',
                               encoding="utf-8", newline="\n")
        code, _, err = self.run_main(["--zones"])
        self.assertEqual(code, 2)
        self.assertIn("missing-in-oracle", err)

    def test_soft_zone_divergence_exit_0_with_warnings(self):
        self.write_cases(self.champ, [("case/hard/a", "1"), ("case/soft/x", "s1")])
        self.write_cases(self.oracle, [("case/hard/a", "1"), ("case/soft/x", "s2")])
        code, out, err = self.run_main(["--zones", "--ledger", str(self.ledger)])
        self.assertEqual(code, 0)
        self.assertIn("软区警告", err)
        report = json.loads(out)
        self.assertEqual(report["verdicts"]["overall"], "soft_divergence")
        self.assertEqual(report["verdicts"]["hard_zone"], "clean")
        self.assertTrue(report["warnings"])
        for key in ("date", "entries", "verdicts"):
            self.assertIn(key, report)  # 台账 {date, entries, verdicts}
        on_disk = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["verdicts"]["overall"], "soft_divergence")

    def test_all_equal_exit_0(self):
        self.write_cases(self.champ, [("case/hard/a", 1), ("case/soft/x", "s")])
        self.write_cases(self.oracle, [("case/hard/a", 1), ("case/soft/x", "s")])
        code, out, _ = self.run_main(["--zones"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["verdicts"]["overall"], "equivalent")
        self.assertTrue(all(r["verdict"] == "equal" for r in report["entries"]))

    def test_missing_input_exit_1(self):
        self.write_cases(self.oracle, [("case/hard/a", "1")])
        code, _, err = self.run_main(["--zones"])  # champion 文件不存在
        self.assertEqual(code, 1)
        self.assertIn("对拍未运行或输入缺失", err)

    def test_bad_timeout_exit_1(self):
        self.write_cases(self.champ, [("case/hard/a", "1")])
        self.write_cases(self.oracle, [("case/hard/a", "1")])
        code, _, err = self.run_main(["--zones", "--timeout", "0"])
        self.assertEqual(code, 1)
        self.assertIn("timeout", err)

    def test_invalid_registry_exit_1(self):
        self.regp.write_text("not: [valid, registry", encoding="utf-8", newline="\n")
        self.write_cases(self.champ, [("case/hard/a", "1")])
        self.write_cases(self.oracle, [("case/hard/a", "1")])
        code, _, err = self.run_main(["--zones"])
        self.assertEqual(code, 1)
        self.assertIn("YAML 解析失败", err)  # 畸形注册表：解析失败也必须 exit 1

    def test_zones_mode_no_classified_cases_exit_1(self):
        self.champ.write_text("plain text output\n", encoding="utf-8", newline="\n")
        self.oracle.write_text("plain text output2\n", encoding="utf-8", newline="\n")
        code, _, err = self.run_main(["--zones"])
        self.assertEqual(code, 1)
        self.assertIn("没有任何案例匹配", err)


if __name__ == "__main__":
    unittest.main()
