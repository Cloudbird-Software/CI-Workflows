#!/usr/bin/env python3
# test_audit.py —— 泄漏审计自测（W4-C3 AC-2 / ADR-0068 决策 5）
#   static：带测试名的 echo 无注记 → 报警红（卡面 fixture 要求）；注记/安全模式 → 绿
#   scan：banned 词/marker/节点 ID 命中 → 红；干净日志 → 绿；报警文本自身脱敏（不回显泄漏内容）
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent / "audit_outputs.py"
PY = sys.executable
FULL_MARKER = "CLOUDBIRD-HOLDOUT-CANARY-54c579316b6b9ec4"

LEAKY_WF = """name: t
on: [push]
permissions: {contents: read}
jobs:
  bad:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: leak
        run: |
          echo "FAILED $FAILED_TESTS"      # 带测试名的输出行——必须被静态审计报警
"""
CLEAN_WF = """name: t
on: [push]
permissions: {contents: read}
jobs:
  ok:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: counts-only
        run: |
          set -euo pipefail
          RESULT=$(python3 x.py)
          echo "holdout: 42/45 通过"      # audit-ok: 仅计数（unseal_gate stdout 契约）
          exit 0
"""


def run_audit(*argv):
    return subprocess.run([PY, str(AUDIT), *map(str, argv)],
                          capture_output=True, text=True, encoding="utf-8")


class TestStatic(unittest.TestCase):
    def test_leaky_workflow_alarms(self):
        """卡面 fixture：带测试名的输出行喂给审计器 → 报警（exit 1）。"""
        with tempfile.TemporaryDirectory() as td:
            wf = Path(td) / "leaky.yml"
            wf.write_text(LEAKY_WF, encoding="utf-8", newline="\n")
            p = run_audit("static", "--workflow", wf)
            self.assertEqual(p.returncode, 1)
            self.assertIn("未注记", p.stdout)
            self.assertIn("echo", p.stdout)

    def test_clean_workflow_passes(self):
        """注记 + 内建安全模式（set-/纯赋值/exit）→ 绿。"""
        with tempfile.TemporaryDirectory() as td:
            wf = Path(td) / "clean.yml"
            wf.write_text(CLEAN_WF, encoding="utf-8", newline="\n")
            p = run_audit("static", "--workflow", wf)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("静态审计干净", p.stdout)


class TestScan(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="audit-scan-test-"))
        self.banned = self.td / "banned.txt"
        self.banned.write_text("test_hgate_leak_probe_fail\ntest_hgate_a.py\n",
                               encoding="utf-8", newline="\n")

    def scan(self, log_text, registry=None):
        log = self.td / "run.log"
        log.write_text(log_text, encoding="utf-8", newline="\n")
        argv = ["scan", "--input", log, "--banned", self.banned]
        if registry:
            argv += ["--registry", registry]
        return run_audit(*argv)

    def test_clean_log_passes(self):
        p = self.scan("holdout: 3/4 通过\n通过率: 主 75.0% vs holdout 75.0%\n")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("审计干净", p.stdout)

    def test_banned_word_hit_redacted(self):
        """banned 命中 → exit 1，且审计自身输出不回显该词（防二次泄漏）。"""
        p = self.scan("holdout: 3/4 通过\nFAILED test_hgate_leak_probe_fail - assert\n")
        self.assertEqual(p.returncode, 1)
        self.assertIn("LEAK", p.stdout)
        self.assertNotIn("test_hgate_leak_probe_fail", p.stdout)  # 只报位置不报内容

    def test_node_id_regex_hit(self):
        """词表外的节点 ID 形态（.py::xxx）也命中（正则黑名单兜底）。"""
        p = self.scan("some line with mystery.py::test_hidden_case\n")
        self.assertEqual(p.returncode, 1)
        self.assertIn("node-id", p.stdout)
        self.assertNotIn("mystery.py::test_hidden_case", p.stdout)

    def test_canary_marker_hit_masked(self):
        """泄漏诱饵联动（宪法 §6）：marker 出现在日志 → 报警且掩码显示。"""
        reg = self.td / "registry.yaml"
        reg.write_text(f"version: 1\nmarkers:\n- id: HO-0004\n  marker: {FULL_MARKER}\n  drill: false\n",
                       encoding="utf-8", newline="\n")
        p = self.scan(f"garbage {FULL_MARKER} trail\n", registry=reg)
        self.assertEqual(p.returncode, 1)
        self.assertIn("canary-marker", p.stdout)
        self.assertNotIn(FULL_MARKER, p.stdout + p.stderr)  # 完整 marker 绝不回显（防二次泄漏）


if __name__ == "__main__":
    unittest.main()
