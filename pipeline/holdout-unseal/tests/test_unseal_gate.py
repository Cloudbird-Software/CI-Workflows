#!/usr/bin/env python3
# test_unseal_gate.py —— 揭封 gate 自测（W4-C3 .github#222 / ADR-0068；卡 AC 红绿对照）
#   hash：绿（合规解封执行）/红（篡改→exit 3 拒揭）；计数化：stdout 只有计数/百分比，
#   测试名/文件名/marker 零出现（AC-1）；通过率差 >5% exit 1 / ≤5% exit 0（AC-4）；
#   无凭据 fail-closed exit 2；记录字段齐全（AC-3）。fixture 临时目录，零真实试卷。
import json
import os
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE = HERE.parent / "unseal_gate.py"
CONFIG = HERE.parent / "config.json"
PY = sys.executable
MARKER = "CLOUDBIRD-HOLDOUT-CANARY-deadbeefdeadbeef"

FILE_A = "def test_hgate_ok1():\n    assert True\n\n\ndef test_hgate_ok2():\n    assert True\n"
FILE_B = "def test_hgate_ok3():\n    assert True\n\n\ndef test_hgate_leak_probe_fail():\n    assert 1 == 2\n"
BANNED = ["test_hgate_ok1", "test_hgate_ok2", "test_hgate_ok3", "test_hgate_leak_probe_fail",
          "test_hgate_a.py", "test_hgate_b.py"]


def canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def b64(text):
    from base64 import b64encode
    return b64encode(text.encode()).decode()


def make_entry(eid, files):
    payload = {"kind": "sealed-test-set", "schema": "holdout-unseal/1", "runner": "pytest",
               "files": [{"name": n, "sha256": sha256(t.encode()).hexdigest(), "content_b64": b64(t)}
                         for n, t in files]}
    return {"id": eid, "type": "e2e-scenario", "ac_ref": "IR-0003/W4-C3/AC-1", "payload": payload,
            "sealed_sha256": sha256(canon(payload).encode()).hexdigest(),
            "created_at": "2026-08-22T00:00:00Z", "sealed_by": "fixture"}


def build_root(tamper=False):
    root = Path(tempfile.mkdtemp(prefix="unseal-gate-test-"))
    (root / "entries").mkdir()
    e1 = make_entry("HO-0001", [("test_hgate_a.py", FILE_A), ("test_hgate_b.py", FILE_B)])
    if tamper:  # 改 payload 不改 sealed_sha256 = 试卷被篡改
        e1["payload"]["files"][0]["content_b64"] = b64("def test_evil():\n    assert False\n")
    (root / "entries" / "HO-0001.json").write_text(json.dumps(e1, ensure_ascii=False, indent=1),
                                                   encoding="utf-8", newline="\n")
    # canary 诱饵条目：kind 不符 → gate 永不解封；其 marker 绝不该出现在任何输出
    cp = {"marker": MARKER, "bait": "诱饵内容绝不出现在任何日志"}
    canary = {"id": "HO-0002", "type": "canary", "ac_ref": "IR-0003/W1-C4/AC-3", "payload": cp,
              "sealed_sha256": sha256(canon(cp).encode()).hexdigest(),
              "created_at": "2026-08-22T00:00:00Z", "sealed_by": "fixture"}
    (root / "entries" / "HO-0002.json").write_text(json.dumps(canary, ensure_ascii=False, indent=1),
                                                   encoding="utf-8", newline="\n")
    return root


def run_gate(root, rate, *extra, with_token=False):
    out = Path(tempfile.mkdtemp(prefix="unseal-gate-out-"))
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    if with_token:
        env["GH_TOKEN"] = "present"  # 只验证"有凭据→放行"分支，token 值不被 gate 使用
    argv = [PY, str(GATE), "--holdout-root", str(root), "--config", str(CONFIG),
            "--main-pass-rate", rate, "--pr", "222", "--run-id", "test-run",
            "--record-out", str(out / "record.json"), "--detail-out", str(out / "detail.md"),
            "--banned-out", str(out / "banned.txt"), *extra]
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", env=env)
    return p, out


class TestUnsealGate(unittest.TestCase):
    def test_hash_green_pass(self):
        """绿：合规条目 → 解封执行 → 计数输出 → exit 0（gap=0，strict+凭据在场）。"""
        p, out = run_gate(build_root(), "0.75", "--detail-mode", "strict", with_token=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("holdout: 3/4 通过", p.stdout)

    def test_hash_red_tamper_exit3(self):
        """红：payload 篡改而哈希未重算 → exit 3 拒揭封（AC-1 前置）。"""
        p, out = run_gate(build_root(tamper=True), "0.75")
        self.assertEqual(p.returncode, 3)
        self.assertIn("sealed_sha256 校验失败", p.stdout + p.stderr)
        rec = json.loads((out / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["verdict"], "tamper")
        self.assertFalse(rec["entries"][0]["verify"])  # AC-3：校验结果入账

    def test_counts_only_no_leak(self):
        """AC-1：stdout 零明细——测试名/文件名/marker 全不出现；词表文件却含测试名。"""
        p, out = run_gate(build_root(), "0.75", "--always-detail", "--detail-mode", "artifact")
        self.assertEqual(p.returncode, 0, p.stdout)
        for banned in BANNED + [MARKER, "诱饵"]:
            self.assertNotIn(banned, p.stdout, f"PR check 输出泄漏: {banned}")
        self.assertNotRegex(p.stdout, r"FAILED\s+\S+::")  # 无 pytest 失败明细行
        banned_words = (out / "banned.txt").read_text(encoding="utf-8")
        self.assertIn("test_hgate_leak_probe_fail", banned_words)  # 已知而不出

    def test_gap_escalation_exit1(self):
        """AC-4：主 100% vs holdout 75% → 差 25% > 5% → exit 1 + needs-human 注记。"""
        p, out = run_gate(build_root(), "1.0", "--always-detail", "--detail-mode", "artifact")
        self.assertEqual(p.returncode, 1)
        self.assertIn("needs-human", p.stdout + p.stderr)
        self.assertIn("25.0%", p.stdout + p.stderr)
        rec = json.loads((out / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["verdict"], "gap-escalated")
        self.assertTrue(rec["escalated"])

    def test_gap_within_threshold_exit0(self):
        """AC-4 边界内：78% vs 75% → 差 3% ≤ 5% → exit 0。"""
        p, _ = run_gate(build_root(), "0.78", "--always-detail", "--detail-mode", "artifact")
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_no_credential_fail_closed_exit2(self):
        """无凭据 fail-closed：需写回+strict+GH_TOKEN 缺席 → exit 2（注明）。"""
        p, out = run_gate(build_root(), "0.75", "--detail-mode", "strict")
        self.assertEqual(p.returncode, 2)
        self.assertIn("fail-closed", p.stdout + p.stderr)
        self.assertIn("HOLDOUT_UNSEAL_TOKEN", p.stdout + p.stderr)  # 注明原因（卡 AC）

    def test_detail_artifact_mode(self):
        """artifact 模式：明细落文件（测试名只去 holdout 仓，不进 PR 日志）。"""
        p, out = run_gate(build_root(), "0.76", "--always-detail", "--detail-mode", "artifact")
        self.assertEqual(p.returncode, 0, p.stdout)
        detail = (out / "detail.md").read_text(encoding="utf-8")
        self.assertIn("holdout-unseal-detail/1", detail)
        self.assertIn("test_hgate_leak_probe_fail", detail.split("```json")[1])

    def test_record_fields(self):
        """AC-3 数据源：记录含 sealed_sha256 校验结果/计数/ts/pr/run_id/阈值。"""
        p, out = run_gate(build_root(), "0.75", "--always-detail", "--detail-mode", "artifact")
        rec = json.loads((out / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["schema"], "holdout-unseal-record/1")
        self.assertEqual(rec["entries"], [{"id": "HO-0001", "sha8": rec["entries"][0]["sha8"],
                                           "verify": True}])
        self.assertEqual((rec["passed"], rec["total"]), (3, 4))
        for k in ("ts", "pr", "run_id", "threshold_pct", "gap_pct", "escalated"):
            self.assertIn(k, rec)


if __name__ == "__main__":
    unittest.main()
