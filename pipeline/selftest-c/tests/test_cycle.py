"""cycle.py 测试：换代只追加+旧代 retired、旧代必须 frozen、新代必须 candidate。"""
import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path

from tests.testlib import (SHA_A, SHA_B, SHA_C, T1, T2, load_registry_file,
                           make_entry, registry_text)
from oracle import cycle


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "registry.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def write_registry(self, entries):
        self.path.write_text(registry_text(entries), encoding="utf-8", newline="\n")

    def run_main(self, old, new, extra=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cycle.main(["--registry", str(self.path), "--old", old, "--new", new] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def test_cycle_promotes_candidate_append_only(self):
        old_entry = make_entry(sha=SHA_A, status="frozen")
        snapshot = copy.deepcopy(old_entry["generations"])
        self.write_registry([old_entry, make_entry(sha=SHA_B, status="candidate")])
        code, out, _ = self.run_main(SHA_A, SHA_B, ["--note", "gen2 换代"])
        self.assertEqual(code, 0)
        entries = load_registry_file(self.path)["entries"]
        by_sha = {e["frozen_sha"]: e for e in entries}
        self.assertEqual(by_sha[SHA_A]["status"], "retired")       # 旧代退役
        self.assertEqual(by_sha[SHA_A]["generations"], snapshot)   # 旧代历史一字不动（只追加）
        new = by_sha[SHA_B]
        self.assertEqual(new["status"], "frozen")
        self.assertEqual(len(new["generations"]), 1)               # 新代追加 generations
        self.assertEqual(new["generations"][0]["sha"], SHA_B)
        self.assertEqual(new["generations"][0]["note"], "gen2 换代")
        self.assertTrue(new["generations"][0]["frozen_at"])

    def test_cycle_old_must_be_frozen(self):
        self.write_registry([make_entry(sha=SHA_A, status="candidate"),
                             make_entry(sha=SHA_B, status="candidate")])
        before = self.path.read_bytes()
        code, _, err = self.run_main(SHA_A, SHA_B)
        self.assertEqual(code, 1)
        self.assertIn("非 frozen", err)
        self.assertEqual(self.path.read_bytes(), before)  # 失败不落盘

    def test_cycle_unknown_old_exit_1(self):
        self.write_registry([make_entry(sha=SHA_A, status="frozen")])
        code, _, err = self.run_main(SHA_C, SHA_B)
        self.assertEqual(code, 1)
        self.assertIn("不唯一或 status 非 frozen", err)

    def test_cycle_new_must_be_candidate(self):
        self.write_registry([make_entry(sha=SHA_A, status="frozen"),
                             make_entry(sha=SHA_B, status="frozen")])
        code, _, err = self.run_main(SHA_A, SHA_B)
        self.assertEqual(code, 1)
        self.assertIn("非 candidate", err)

    def test_cycle_new_sha_in_history_middle_rejected(self):
        weird = make_entry(sha=SHA_C, status="candidate")
        weird["generations"] = [
            {"sha": SHA_C, "frozen_at": T1, "note": "was here"},
            {"sha": SHA_A, "frozen_at": T2, "note": "later"},
        ]
        self.write_registry([make_entry(sha=SHA_A, status="frozen"), weird])
        code, _, err = self.run_main(SHA_A, SHA_C)
        self.assertEqual(code, 1)
        self.assertIn("只追加，不回滚", err)

    def test_cycle_creates_entry_when_no_candidate(self):
        self.write_registry([make_entry(sha=SHA_A, status="frozen")])
        code, out, _ = self.run_main(SHA_A, SHA_C)
        self.assertEqual(code, 0)
        entries = load_registry_file(self.path)["entries"]
        by_sha = {e["frozen_sha"]: e for e in entries}
        self.assertEqual(by_sha[SHA_A]["status"], "retired")
        new = by_sha[SHA_C]
        self.assertEqual(new["status"], "frozen")
        self.assertEqual(new["name"], by_sha[SHA_A]["name"])  # 克隆旧代元数据
        self.assertEqual([g["sha"] for g in new["generations"]], [SHA_C])

    def test_cycle_bad_sha_shape_exit_1(self):
        self.write_registry([make_entry(sha=SHA_A, status="frozen")])
        code, _, err = self.run_main("ZZZ", SHA_C)
        self.assertEqual(code, 1)
        self.assertIn("形状非法", err)


if __name__ == "__main__":
    unittest.main()
