#!/usr/bin/env python3
# test_ledger_append.py —— 揭封记录 append-only 台账断言（W4-C3 AC-3 / ADR-0068 决策 4）
#   卡面要求：重放揭封 → 两行记录且第一行不变。用 holdout unseal-log.py 镜像
#   （tests/fixtures/unseal_log_mirror.py，同步纪律见其头注释）逐字节验证：
#   gate 两次出记录 → 台账两行、首行字节不变、链式 prev_hash 相接、verify 全绿；
#   改写历史 → 断链红（负控制）。e2e 真仓脚本路径由 workflow demo job 覆盖。
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_unseal_gate import build_root, run_gate

HERE = Path(__file__).resolve().parent
MIRROR = HERE / "fixtures" / "unseal_log_mirror.py"
PY = sys.executable


def mirror(*argv, stdin=None):
    return subprocess.run([PY, str(MIRROR), *map(str, argv)], input=stdin,
                          capture_output=True, text=True, encoding="utf-8")


class TestLedgerAppendOnly(unittest.TestCase):
    def setUp(self):
        self.ledger = Path(tempfile.mkdtemp(prefix="unseal-ledger-test-")) / "unseal.jsonl"

    def append_record(self, rec_path):
        p = mirror("append", "--record", rec_path, "--ledger", self.ledger)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return p

    def test_replay_two_lines_first_unchanged(self):
        """AC-3：重放揭封 → 两行记录且第一行不变（append-only，字节级）。"""
        root = build_root()
        p1, o1 = run_gate(root, "0.76", "--run-id", "run-1", "--always-detail",
                          "--detail-mode", "artifact")
        self.assertEqual(p1.returncode, 0, p1.stdout)
        self.append_record(o1 / "record.json")
        first = self.ledger.read_bytes()
        p2, o2 = run_gate(root, "1.0", "--run-id", "run-2", "--always-detail",
                          "--detail-mode", "artifact")  # 第二次揭封=通过率差升级演示
        self.assertEqual(p2.returncode, 1)
        self.append_record(o2 / "record.json")
        lines = self.ledger.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].encode("utf-8"), first.rstrip(b"\n"))  # 第一行不变
        r1, r2 = json.loads(lines[0]), json.loads(lines[1])
        self.assertEqual(r2["prev_hash"], r1["record_hash"])             # 链式相接
        self.assertEqual([r1["verdict"], r2["verdict"]], ["pass", "gap-escalated"])
        self.assertTrue(all(e["verify"] for e in r1["entries"] + r2["entries"]))  # AC-3 字段

    def test_verify_and_rewrite_negative(self):
        """verify 全绿 + 负控制：改写首行计数 → record_hash 断链 → verify 红。"""
        root = build_root()
        _, o = run_gate(root, "0.76", "--run-id", "run-9", "--always-detail",
                        "--detail-mode", "artifact")
        self.append_record(o / "record.json")
        p = mirror("verify", "--ledger", self.ledger)
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = self.ledger.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[0]); tampered["passed"] = 999
        lines[0] = json.dumps(tampered, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.ledger.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        p = mirror("verify", "--ledger", self.ledger)
        self.assertEqual(p.returncode, 1)
        self.assertIn("record_hash 不符", p.stderr)


if __name__ == "__main__":
    unittest.main()
