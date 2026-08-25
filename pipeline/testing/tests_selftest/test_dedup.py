"""dedup：崩溃栈哈希去重（AC-3）——真去重（重编译/行号漂移不裂变）。"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from pipeline.testing.fuzz import dedup

ROOT = Path(__file__).resolve().parents[1]
CRASHES = ROOT / "tests" / "fixtures" / "crashes"


def read(name):
    return (CRASHES / name).read_text(encoding="utf-8")


class TestDedup(unittest.TestCase):
    def test_same_stack_different_lines_same_fingerprint(self):
        a = read("crash-a.txt")
        b = read("crash-b.txt")   # 同帧不同行号 + 不同消息
        c = read("crash-c.txt")   # 不同栈
        report = dedup.dedup_texts([a, b, c], labels=["a", "b", "c"])
        self.assertEqual(report["total_inputs"], 3)
        self.assertEqual(report["unique_fingerprints"], 2)
        top = report["groups"][0]
        self.assertEqual(top["count"], 2)
        self.assertEqual(top["inputs"], ["a", "b"])
        self.assertEqual(top["exception_type"], "ValueError")
        self.assertEqual(report["groups"][1]["count"], 1)
        self.assertEqual(report["groups"][1]["exception_type"], "IndexError")

    def test_strict_lines_split_duplicates(self):
        a = read("crash-a.txt")
        b = read("crash-b.txt")
        report = dedup.dedup_texts([a, b], labels=["a", "b"], strict_lines=True)
        self.assertEqual(report["unique_fingerprints"], 2)

    def test_no_traceback_fallback(self):
        fp1, _ = dedup.fingerprint("not a traceback at all")
        fp2, _ = dedup.fingerprint("not a traceback at all   ")
        self.assertEqual(fp1, fp2)

    def test_cli_exit_code(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "pipeline/testing/fuzz/dedup.py"), "--dir", str(CRASHES)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["unique_fingerprints"], 2)


if __name__ == "__main__":
    unittest.main()
