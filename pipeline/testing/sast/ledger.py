#!/usr/bin/env python3
"""ledger —— SAST 分诊台账（IR-0004 AC-6）：append-only + sha256 链校验。

条目字段：
  alert_fingerprint  告警指纹（CodeQL alerts API 的 fingerprint，必填）
  alert_repo         告警所在仓（owner/name，必填）
  rule               规则 id（如 py/sql-injection，必填）
  severity           error | warning | note（必填）
  disposition        fixed | waived | false_positive（必填）
  adr                决策记录引用（disposition=waived 时必填）
  reason             false_positive 时必填（理由文本）
  resolved_sha       处置所在 commit SHA（disposition=fixed 时必填，40 hex）
  date               YYYY-MM-DD（缺省今天）
  chain_sha          链哈希（工具维护，append 时计算）

链规则（build-a1 风格，自实现，不 import 仓外）：
  genesis = sha256("cloudbird-sast-ledger-genesis-v1")
  chain_sha(i) = sha256(chain_sha(i-1) + "\\n" + canonical_json(entry_i 去掉 chain_sha))
  canonical_json = json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)
任何条目被篡改/删除/重排都会使 verify 链断裂（退出码 2）。

CLI：
  python ledger.py init   --ledger ledger.yaml
  python ledger.py append --ledger ledger.yaml --entry entry.json
  python ledger.py append --ledger ledger.yaml --fingerprint F --repo O/N --rule R \
      --severity error --disposition fixed --resolved-sha <40hex> [--date 2026-08-24]
  python ledger.py verify --ledger ledger.yaml
  python ledger.py show   --ledger ledger.yaml [--json]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

try:  # 双模式导入：包内 / 独立脚本
    from pipeline.testing import _yamlmini
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pipeline.testing import _yamlmini

GENESIS_SEED = "cloudbird-sast-ledger-genesis-v1"
FIELD_ORDER = [
    "alert_fingerprint",
    "alert_repo",
    "rule",
    "severity",
    "disposition",
    "adr",
    "reason",
    "resolved_sha",
    "date",
    "chain_sha",
]
DISPOSITIONS = ("fixed", "waived", "false_positive")
SEVERITIES = ("error", "warning", "note")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def genesis():
    return hashlib.sha256(GENESIS_SEED.encode("utf-8")).hexdigest()


def canonical_entry(entry):
    payload = {k: entry.get(k, "") for k in FIELD_ORDER if k != "chain_sha"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chain_of(prev_chain, entry):
    return hashlib.sha256((prev_chain + "\n" + canonical_entry(entry)).encode("utf-8")).hexdigest()


class LedgerError(Exception):
    pass


def load_ledger(path):
    doc = _yamlmini.load(str(path))
    if not isinstance(doc, dict) or "entries" not in doc:
        raise LedgerError("台账结构非法：缺少 entries")
    if not isinstance(doc["entries"], list):
        raise LedgerError("台账结构非法：entries 必须是列表")
    return doc


def validate_entry(entry, index):
    for field in ("alert_fingerprint", "alert_repo", "rule"):
        if not str(entry.get(field, "")).strip():
            raise LedgerError("条目 %d 缺少必填字段 %s" % (index, field))
    if entry.get("severity") not in SEVERITIES:
        raise LedgerError("条目 %d severity 非法: %r" % (index, entry.get("severity")))
    disposition = entry.get("disposition")
    if disposition not in DISPOSITIONS:
        raise LedgerError("条目 %d disposition 非法: %r" % (index, disposition))
    if disposition == "waived" and not str(entry.get("adr", "")).strip():
        raise LedgerError("条目 %d disposition=waived 必须带 adr" % index)
    if disposition == "false_positive" and not str(entry.get("reason", "")).strip():
        raise LedgerError("条目 %d disposition=false_positive 必须带 reason" % index)
    if disposition == "fixed" and not SHA_RE.match(str(entry.get("resolved_sha", ""))):
        raise LedgerError("条目 %d disposition=fixed 必须带 40 位 hex resolved_sha" % index)
    if not DATE_RE.match(str(entry.get("date", ""))):
        raise LedgerError("条目 %d date 非法: %r（需 YYYY-MM-DD）" % (index, entry.get("date")))
    extra = set(entry) - set(FIELD_ORDER)
    if extra:
        raise LedgerError("条目 %d 含未知字段: %s" % (index, sorted(extra)))


def verify_ledger(path):
    """全量校验：结构 + 逐条 disposition 规则 + sha256 链。返回 (entries, ok, detail)。"""
    doc = load_ledger(path)
    prev = genesis()
    for index, entry in enumerate(doc["entries"], start=1):
        validate_entry(entry, index)
        expected = chain_of(prev, entry)
        if entry.get("chain_sha") != expected:
            return (
                doc["entries"],
                False,
                "链断裂于条目 %d（fingerprint=%s）：期望 chain_sha=%s 实际=%s"
                % (index, entry.get("alert_fingerprint", "?"), expected, entry.get("chain_sha", "?")),
            )
        prev = expected
    return doc["entries"], True, "entries=%d chain=OK genesis=%s" % (len(doc["entries"]), prev[:16])


def write_ledger(path, entries):
    doc = {"version": 1, "entries": entries}
    text = _yamlmini.dump(doc)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def append_entry(path, entry):
    """append-only：先全量 verify 旧链，再计算新链追加。返回新条目。"""
    ledger = Path(path)
    if not ledger.exists():
        raise LedgerError("台账不存在，先运行 init: %s" % path)
    entries, ok, detail = verify_ledger(path)
    if not ok:
        raise LedgerError("追加前校验失败（拒绝在坏链上 append）: %s" % detail)
    record = {k: entry.get(k, "") for k in FIELD_ORDER if k != "chain_sha"}
    if not record.get("date"):
        record["date"] = datetime.date.today().isoformat()
    record = {k: record.get(k, "") for k in FIELD_ORDER[:-1]}
    validate_entry(record, len(entries) + 1)
    prev = entries[-1]["chain_sha"] if entries else genesis()
    record["chain_sha"] = chain_of(prev, record)
    entries.append(record)
    write_ledger(path, entries)
    return record


def init_ledger(path):
    ledger = Path(path)
    if ledger.exists():
        raise LedgerError("台账已存在: %s" % path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    write_ledger(path, [])


def main(argv=None):
    parser = argparse.ArgumentParser(description="SAST 分诊台账（AC-6）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化空台账")
    p_init.add_argument("--ledger", required=True)

    p_append = sub.add_parser("append", help="追加一条分诊（append-only）")
    p_append.add_argument("--ledger", required=True)
    p_append.add_argument("--entry", help="条目 JSON 文件（字段见模块 docstring）")
    p_append.add_argument("--fingerprint")
    p_append.add_argument("--repo")
    p_append.add_argument("--rule")
    p_append.add_argument("--severity", choices=SEVERITIES)
    p_append.add_argument("--disposition", choices=DISPOSITIONS)
    p_append.add_argument("--adr", default="")
    p_append.add_argument("--reason", default="")
    p_append.add_argument("--resolved-sha", default="")
    p_append.add_argument("--date", default="")

    p_verify = sub.add_parser("verify", help="校验链条完整性（退出码 2=篡改/非法）")
    p_verify.add_argument("--ledger", required=True)

    p_show = sub.add_parser("show", help="列出条目")
    p_show.add_argument("--ledger", required=True)
    p_show.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            init_ledger(args.ledger)
            print(json.dumps({"ledger": args.ledger, "entries": 0}, ensure_ascii=False))
            return 0

        if args.command == "append":
            if args.entry:
                entry = json.loads(Path(args.entry).read_text(encoding="utf-8"))
            else:
                entry = {
                    "alert_fingerprint": args.fingerprint,
                    "alert_repo": args.repo,
                    "rule": args.rule,
                    "severity": args.severity,
                    "disposition": args.disposition,
                    "adr": args.adr,
                    "reason": args.reason,
                    "resolved_sha": args.resolved_sha,
                    "date": args.date,
                }
            record = append_entry(args.ledger, entry)
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
            return 0

        if args.command == "verify":
            entries, ok, detail = verify_ledger(args.ledger)
            print("VERIFY %s: %s" % ("OK" if ok else "FAIL", detail))
            return 0 if ok else 2

        if args.command == "show":
            entries, _, _ = verify_ledger(args.ledger)
            if args.json:
                print(json.dumps(entries, ensure_ascii=False, indent=2))
            else:
                for entry in entries:
                    print(
                        "%s %s %s %s %s adr=%s reason=%s"
                        % (
                            entry["alert_fingerprint"][:20],
                            entry["alert_repo"],
                            entry["rule"],
                            entry["severity"],
                            entry["disposition"],
                            entry.get("adr", "") or "-",
                            (entry.get("reason", "")[:40] or "-"),
                        )
                    )
                print("entries: %d" % len(entries))
            return 0
    except (LedgerError, OSError, ValueError, json.JSONDecodeError) as exc:
        print("FATAL: %s" % exc, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
