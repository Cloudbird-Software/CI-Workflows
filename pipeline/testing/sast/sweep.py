#!/usr/bin/env python3
"""sweep —— SAST 全量 sweep：CodeQL 告警 vs 分诊台账（IR-0004 AC-6）。

输入 CodeQL alerts 输出 JSON（本仓约定形状，fixture 见 fixtures/）：
  {"tool": "codeql", "repo": "owner/name",
   "alerts": [{"fingerprint": "...", "rule": "...", "severity": "error",
               "location": "src/db.py:42"}]}

比对台账（ledger.py 管理的 append-only sha256 链）：
  - 台账校验失败 → 退出码 2（先验链，坏链上的比对无意义）；
  - 未处置告警（台账无对应 fingerprint+repo 条目）→ 输出清单并退出码 1
    （机械信号：需要开 issue 跟进）；--no-enforce 时仅报告不拦截；
  - 全部处置 → 退出码 0。

CLI：
  python sweep.py --alerts alerts.json --ledger ledger.yaml [--out report.json] [--no-enforce]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:  # 双模式导入：包内 / 独立脚本
    from pipeline.testing import _yamlmini  # noqa: F401
    from pipeline.testing.sast import ledger as ledger_mod
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pipeline.testing.sast import ledger as ledger_mod


def load_alerts(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(doc, list):
        # CodeQL REST /repos/{repo}/code-scanning/alerts 原始形状 → 约定形状（机械转换）
        doc = _from_codeql_rest(doc, "unknown/unknown")
    alerts = doc.get("alerts")
    if not isinstance(alerts, list):
        raise ValueError("alerts 输出缺少 alerts 列表（约定形状见模块 docstring）")
    repo_default = doc.get("repo", "unknown/unknown")
    normalized = []
    for index, alert in enumerate(alerts, start=1):
        fingerprint = str(alert.get("fingerprint", "")).strip()
        if not fingerprint:
            raise ValueError("告警 %d 缺少 fingerprint" % index)
        normalized.append(
            {
                "fingerprint": fingerprint,
                "repo": str(alert.get("repo", repo_default)),
                "rule": alert.get("rule", "?"),
                "severity": alert.get("severity", "?"),
                "location": alert.get("location", "?"),
            }
        )
    return {"tool": doc.get("tool", "codeql"), "alerts": normalized}


def _from_codeql_rest(items, repo_default):
    """CodeQL code-scanning alerts REST 响应 → 约定形状（只取 open 告警，机械映射）。"""
    alerts = []
    for alert in items:
        if alert.get("state") != "open":
            continue
        rule = alert.get("rule") or {}
        instance = alert.get("most_recent_instance") or {}
        fingerprint = alert.get("fingerprint") or str(alert.get("number", "?"))
        severity = {"error": "error", "warning": "warning"}.get(
            rule.get("security_severity_level"), "note"
        )
        alerts.append(
            {
                "fingerprint": fingerprint,
                "repo": repo_default,
                "rule": rule.get("id", "?"),
                "severity": severity,
                "location": (instance.get("location") or {}).get("path", "?"),
            }
        )
    return {"tool": "codeql", "repo": repo_default, "alerts": alerts}


def sweep(alerts_path, ledger_path, enforce=True):
    alerts_doc = load_alerts(alerts_path)
    entries, ok, detail = ledger_mod.verify_ledger(ledger_path)
    if not ok:
        raise ledger_mod.LedgerError("台账校验失败: %s" % detail)
    dispositioned = {(e["alert_fingerprint"], e["alert_repo"]): e for e in entries}
    accounted, undispositioned = [], []
    for alert in alerts_doc["alerts"]:
        key = (alert["fingerprint"], alert["repo"])
        if key in dispositioned:
            accounted.append({**alert, "disposition": dispositioned[key]["disposition"]})
        else:
            undispositioned.append(alert)
    return {
        "tool": alerts_doc["tool"],
        "alerts_total": len(alerts_doc["alerts"]),
        "accounted": len(accounted),
        "undispositioned": len(undispositioned),
        "undispositioned_alerts": undispositioned,
        "enforce": enforce,
        "ledger": str(ledger_path),
        "verdict": "needs-issue" if undispositioned else "clean",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="SAST 全量 sweep（AC-6）")
    parser.add_argument("--alerts", required=True, help="CodeQL alerts JSON")
    parser.add_argument("--ledger", required=True, help="分诊台账 YAML")
    parser.add_argument("--out", help="报告 JSON 输出路径")
    parser.add_argument("--no-enforce", action="store_true", help="只报告未处置，不改退出码")
    args = parser.parse_args(argv)

    try:
        report = sweep(args.alerts, args.ledger, enforce=not args.no_enforce)
    except (OSError, ValueError, ledger_mod.LedgerError, json.JSONDecodeError) as exc:
        print("FATAL: %s" % exc, file=sys.stderr)
        return 2

    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8", newline="\n")
    print(payload, end="")
    if report["undispositioned"]:
        print(
            "SWEEP: %d 条告警未处置，需要开 issue 跟进（%s）"
            % (report["undispositioned"], ", ".join(a["fingerprint"][:16] for a in report["undispositioned_alerts"])),
            file=sys.stderr,
        )
        return 1 if not args.no_enforce else 0
    print("SWEEP: 全部告警已处置（clean）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
