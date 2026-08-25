"""ledger：SAST 分诊台账 append-only sha256 链（AC-6）——篡改必须被检出。"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline.testing import _yamlmini
from pipeline.testing.sast import ledger

ROOT = Path(__file__).resolve().parents[1]
LEDGER_SCRIPT = ROOT / "pipeline/testing/sast/ledger.py"
DEMO = ROOT / "pipeline/testing/sast/fixtures/ledger-demo.yaml"


def make_entry(fingerprint, disposition="fixed", **extra):
    entry = {
        "alert_fingerprint": fingerprint,
        "alert_repo": "cloudbird/api-gateway",
        "rule": "py/test-rule",
        "severity": "error",
        "disposition": disposition,
        "adr": "",
        "reason": "",
        "resolved_sha": "a1b2c3d4e5a1b2c3d4e5a1b2c3d4e5a1b2c3d4e5" if disposition == "fixed" else "",
        "date": "2026-08-24",
    }
    entry.update(extra)
    return entry


class TestLedger(unittest.TestCase):
    def test_init_append_verify_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.yaml"
            ledger.init_ledger(path)
            record = ledger.append_entry(path, make_entry("sha256:" + "a" * 64))
            self.assertEqual(len(record["chain_sha"]), 64)
            ledger.append_entry(path, make_entry("sha256:" + "b" * 64, disposition="waived", adr="ADR-0099"))
            entries, ok, detail = ledger.verify_ledger(path)
            self.assertTrue(ok, detail)
            self.assertEqual(len(entries), 2)

    def test_tamper_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.yaml"
            shutil.copyfile(DEMO, path)
            _, ok_before, _ = ledger.verify_ledger(path)
            self.assertTrue(ok_before)
            doc = _yamlmini.load(str(path))
            doc["entries"][1]["rule"] = "py/tampered-rule"   # 事后篡改
            path.write_text(_yamlmini.dump(doc), encoding="utf-8", newline="\n")
            entries, ok, detail = ledger.verify_ledger(path)
            self.assertFalse(ok)
            self.assertIn("链断裂", detail)
            # CLI 退出码 2
            proc = subprocess.run(
                [sys.executable, str(LEDGER_SCRIPT), "verify", "--ledger", str(path)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 2)

    def test_reject_append_on_bad_chain_and_invalid_dispositions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.yaml"
            shutil.copyfile(DEMO, path)
            doc = _yamlmini.load(str(path))
            doc["entries"][0]["severity"] = "catastrophic"
            path.write_text(_yamlmini.dump(doc), encoding="utf-8", newline="\n")
            with self.assertRaises(ledger.LedgerError):
                ledger.append_entry(path, make_entry("sha256:" + "c" * 64))

            path2 = Path(tmp) / "l2.yaml"
            ledger.init_ledger(path2)
            with self.assertRaises(ledger.LedgerError):   # waived 无 adr
                ledger.append_entry(path2, make_entry("sha256:" + "d" * 64, disposition="waived"))
            with self.assertRaises(ledger.LedgerError):   # false_positive 无 reason
                ledger.append_entry(path2, make_entry("sha256:" + "e" * 64, disposition="false_positive"))
            with self.assertRaises(ledger.LedgerError):   # fixed 无 40hex resolved_sha
                ledger.append_entry(path2, make_entry("sha256:" + "f" * 64, resolved_sha="short"))


if __name__ == "__main__":
    unittest.main()
