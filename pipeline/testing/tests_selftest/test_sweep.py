"""sweep：CodeQL 告警 vs 台账比对（AC-6）——未处置非零退出 + 演示 fixture。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline.testing.sast import ledger, sweep

ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / "pipeline/testing/sast/fixtures/codeql-alerts.json"
DEMO_LEDGER = ROOT / "pipeline/testing/sast/fixtures/ledger-demo.yaml"
SWEEP_SCRIPT = ROOT / "pipeline/testing/sast/sweep.py"


class TestSweep(unittest.TestCase):
    def test_demo_fixture_one_undispositioned_exit_1(self):
        proc = subprocess.run(
            [sys.executable, str(SWEEP_SCRIPT), "--alerts", str(ALERTS), "--ledger", str(DEMO_LEDGER)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 1)   # 未处置 → 需开 issue 信号
        report = json.loads(proc.stdout)
        self.assertEqual(report["alerts_total"], 4)
        self.assertEqual(report["accounted"], 3)
        self.assertEqual(report["undispositioned"], 1)
        self.assertEqual(report["undispositioned_alerts"][0]["rule"], "py/polynomial-regex")

    def test_clean_ledger_exit_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.yaml"
            ledger.init_ledger(path)
            for alert in json.loads(ALERTS.read_text(encoding="utf-8"))["alerts"]:
                ledger.append_entry(
                    path,
                    {
                        "alert_fingerprint": alert["fingerprint"],
                        "alert_repo": "cloudbird/api-gateway",
                        "rule": alert["rule"],
                        "severity": alert["severity"],
                        "disposition": "fixed",
                        "adr": "",
                        "reason": "",
                        "resolved_sha": "a1b2c3d4e5a1b2c3d4e5a1b2c3d4e5a1b2c3d4e5",
                        "date": "2026-08-23",
                    },
                )
            rc = sweep.main(["--alerts", str(ALERTS), "--ledger", str(path)])
            self.assertEqual(rc, 0)

    def test_tampered_ledger_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.yaml"
            text = DEMO_LEDGER.read_text(encoding="utf-8").replace("py/sql-injection", "py/tampered")
            path.write_text(text, encoding="utf-8", newline="\n")
            rc = sweep.main(["--alerts", str(ALERTS), "--ledger", str(path)])
            self.assertEqual(rc, 2)

    def test_codeql_rest_shape_accepted(self):
        rest = [
            {
                "number": 7,
                "state": "open",
                "rule": {"id": "py/sql-injection", "security_severity_level": "error"},
                "most_recent_instance": {"location": {"path": "src/db.py"}},
                "fingerprint": "sha256:" + "1" * 64,
            },
            {"number": 8, "state": "fixed", "rule": {"id": "py/gone"}, "fingerprint": "sha256:" + "2" * 64},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            alerts_path = Path(tmp) / "alerts.json"
            alerts_path.write_text(json.dumps(rest), encoding="utf-8")
            ledger_path = Path(tmp) / "ledger.yaml"
            ledger.init_ledger(ledger_path)
            rc = sweep.main(["--alerts", str(alerts_path), "--ledger", str(ledger_path)])
            self.assertEqual(rc, 1)   # open 告警未处置（fixed 状态的被滤掉）


if __name__ == "__main__":
    unittest.main()
