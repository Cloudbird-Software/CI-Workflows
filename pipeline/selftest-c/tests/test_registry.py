"""registry.py 测试：校验（畸形形状/非法枚举/篡改历史代检出）、register 幂等、retire 约束。"""
import contextlib
import copy
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.testlib import (BUILD_C, RAW_REGISTRY_YAML, SHA_A, SHA_B, T1, T2,
                           load_registry_file, make_entry, registry_text)
from oracle import registry as reg


class RegistryValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "registry.yaml"
        self.path.write_text(RAW_REGISTRY_YAML, encoding="utf-8", newline="\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = reg.main(["--registry", str(self.path)] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def write_registry(self, data):
        entries = data["entries"] if isinstance(data, dict) else data
        self.path.write_text(registry_text(entries), encoding="utf-8", newline="\n")

    def test_validate_ok_raw_yaml(self):
        code, out, _ = self.run_main("validate")
        self.assertEqual(code, 0)
        self.assertIn("OK", out)

    def test_missing_required_field(self):
        data = {"entries": [make_entry()]}
        del data["entries"][0]["decorrelation_reason"]
        self.write_registry(data)
        code, _, err = self.run_main("validate")
        self.assertEqual(code, 1)
        self.assertIn("必填字段缺失", err)

    def test_bad_status_enum(self):
        self.write_registry({"entries": [make_entry(status="ghost")]})
        code, _, err = self.run_main("validate")
        self.assertEqual(code, 1)
        self.assertIn("非法枚举", err)

    def test_bad_sha_shape(self):
        self.write_registry({"entries": [make_entry(sha="XYZ-not-a-sha-at-all")]})
        code, _, err = self.run_main("validate")
        self.assertEqual(code, 1)
        self.assertIn("frozen_sha", err)
        self.assertIn("形状非法", err)

    def test_wrong_type_zones(self):
        entry = make_entry()
        entry["hard_zone"] = "case/hard/*"
        self.write_registry({"entries": [entry]})
        code, _, err = self.run_main("validate")
        self.assertEqual(code, 1)
        self.assertIn("glob 列表", err)

    def test_tampered_last_generation_detected(self):
        entry = make_entry(sha=SHA_A, status="frozen")
        entry["generations"][0]["sha"] = SHA_B  # 篡改历史代的 sha
        self.write_registry({"entries": [entry]})
        code, _, err = self.run_main("validate")
        self.assertEqual(code, 1)
        self.assertIn("换代不变量破坏", err)

    def test_tampered_frozen_at_regression_detected(self):
        entry = make_entry(sha=SHA_B, status="frozen")
        entry["generations"] = [
            {"sha": SHA_A, "frozen_at": T1, "note": "gen1"},
            {"sha": SHA_B, "frozen_at": T2, "note": "gen2"},
        ]
        entry["generations"][0]["frozen_at"] = "2026-06-01T00:00:00Z"  # 倒序=改写历史
        self.write_registry({"entries": [entry]})
        code, _, err = self.run_main("validate")
        self.assertEqual(code, 1)
        self.assertIn("时间倒序", err)

    def test_duplicate_generation_sha_detected(self):
        entry = make_entry(sha=SHA_A, status="frozen")
        entry["generations"] = [
            {"sha": SHA_A, "frozen_at": T1, "note": "gen1"},
            {"sha": SHA_A, "frozen_at": T2, "note": "dup"},
        ]
        self.write_registry({"entries": [entry]})
        code, _, err = self.run_main("validate")
        self.assertEqual(code, 1)
        self.assertIn("sha 重复", err)

    def test_not_a_mapping(self):
        self.path.write_text("! ! !", encoding="utf-8", newline="\n")
        code, _, err = self.run_main("validate")
        self.assertEqual(code, 1)
        self.assertIn("YAML 解析失败", err)


class RegisterRetireTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "registry.yaml"
        self.path.write_text(registry_text([]), encoding="utf-8", newline="\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = reg.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def register_args(self, sha=SHA_A, extra=()):
        return ["--registry", str(self.path), "register",
                "--name", "parser-core", "--host-repo", "cloudbird/hero",
                "--target-surface", "parse", "--frozen-sha", sha,
                "--cluster", "c1", "--decorrelation-reason", "独立实现+不同语料",
                "--hard-zone", "case/hard/*", "--soft-zone", "case/soft/*"] + list(extra)

    def test_register_and_idempotent(self):
        code, out, _ = self.run_main(*self.register_args())
        self.assertEqual(code, 0)
        data = load_registry_file(self.path)
        self.assertEqual(len(data["entries"]), 1)
        entry = data["entries"][0]
        self.assertEqual(entry["status"], "candidate")
        self.assertEqual(entry["generations"], [])
        before = self.path.read_bytes()
        code, out, _ = self.run_main(*self.register_args())
        self.assertEqual(code, 0)
        self.assertIn("幂等命中", out)
        self.assertEqual(self.path.read_bytes(), before)  # 幂等：不重复写入
        self.assertEqual(len(load_registry_file(self.path)["entries"]), 1)

    def test_register_frozen_appends_initial_generation(self):
        code, _, _ = self.run_main(*self.register_args(extra=["--status", "frozen"]))
        self.assertEqual(code, 0)
        entry = load_registry_file(self.path)["entries"][0]
        self.assertEqual(entry["status"], "frozen")
        self.assertEqual(entry["generations"][-1]["sha"], SHA_A)

    def test_retire_from_frozen_ok(self):
        self.run_main(*self.register_args(extra=["--status", "frozen"]))
        code, _, _ = self.run_main("--registry", str(self.path), "retire",
                                   "--frozen-sha", SHA_A)
        self.assertEqual(code, 0)
        entry = load_registry_file(self.path)["entries"][0]
        self.assertEqual(entry["status"], "retired")
        self.assertEqual(entry["generations"][-1]["sha"], SHA_A)  # 历史不动

    def test_retire_from_candidate_rejected(self):
        self.run_main(*self.register_args())
        code, _, err = self.run_main("--registry", str(self.path), "retire",
                                     "--frozen-sha", SHA_A)
        self.assertEqual(code, 1)
        self.assertIn("只允许从 frozen", err)

    def test_script_subprocess_mode(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, str(BUILD_C / "oracle" / "registry.py"),
             "--registry", str(self.path), "validate"],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(BUILD_C))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
