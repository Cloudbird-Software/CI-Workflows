#!/usr/bin/env python3
"""Append-only mutation score ledger with a sha256 hash chain (IR-0004 AC-2).

JSONL, one record per line:
    {"date", "repo", "score_pct", "engine", "survived", "total_mutants",
     "prev_hash", "hash"}

Chain rule: hash_i = sha256(canonical(record_i without "hash")); the
canonical form includes prev_hash, which links to hash_{i-1}. The first
record uses prev_hash = GENESIS ("0" * 64). verify() recomputes the chain
and reports the exact line where tampering is detected.

CLI:
    python ledger.py append --ledger P --repo R --score-pct N --engine E \
        --survived N [--total-mutants N] [--date YYYY-MM-DD]
    python ledger.py verify --ledger P
    python ledger.py show   --ledger P
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64
REQUIRED_FIELDS = ("date", "repo", "score_pct", "engine", "survived")


class LedgerError(Exception):
    """Raised when the ledger is unusable (corrupt tail, bad input)."""


def _entry_hash(record: dict) -> str:
    payload = {k: v for k, v in record.items() if k != "hash"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_records(path: Path) -> list[dict]:
    records = []
    if not path.exists():
        return records
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"line {lineno}: invalid JSON ({exc})") from exc
        if not isinstance(record, dict):
            raise LedgerError(f"line {lineno}: record is not an object")
        records.append(record)
    return records


def append_entry(ledger_path: Path, repo: str, score_pct: float, engine: str,
                 survived: int, total_mutants: int | None = None,
                 date: str | None = None) -> dict:
    """Append one record; refuses to extend a structurally corrupt ledger."""
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = _read_records(path)
    prev_hash = records[-1]["hash"] if records else GENESIS
    record = {
        "date": date or datetime.now(timezone.utc).date().isoformat(),
        "repo": str(repo),
        "score_pct": round(float(score_pct), 2),
        "engine": str(engine),
        "survived": int(survived),
    }
    if total_mutants is not None:
        record["total_mutants"] = int(total_mutants)
    record["prev_hash"] = prev_hash
    record["hash"] = _entry_hash(record)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def verify(ledger_path: Path) -> tuple[bool, list[str]]:
    """Recompute the whole chain; returns (ok, errors with line numbers)."""
    path = Path(ledger_path)
    errors: list[str] = []
    if not path.exists():
        return False, ["ledger file not found"]
    prev_hash = GENESIS
    count = 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        count += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {lineno}: invalid JSON ({exc})")
            return False, errors
        if not isinstance(record, dict):
            errors.append(f"line {lineno}: record is not an object")
            return False, errors
        missing = [f for f in REQUIRED_FIELDS if f not in record]
        if missing:
            errors.append(f"line {lineno}: missing fields {missing}")
            return False, errors
        stored_hash = record.get("hash")
        recomputed = _entry_hash(record)
        if stored_hash != recomputed:
            errors.append(
                f"line {lineno}: hash mismatch (stored {stored_hash}, recomputed {recomputed})"
            )
        if record.get("prev_hash") != prev_hash:
            errors.append(
                f"line {lineno}: prev_hash {record.get('prev_hash')} != "
                f"expected {prev_hash} (chain broken)"
            )
        prev_hash = stored_hash if stored_hash else ""
    if count == 0:
        errors.append("ledger is empty")
    return (not errors), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append-only mutation score ledger")
    sub = parser.add_subparsers(dest="command", required=True)

    p_append = sub.add_parser("append", help="append one score record")
    p_append.add_argument("--ledger", required=True)
    p_append.add_argument("--repo", required=True)
    p_append.add_argument("--score-pct", type=float, required=True)
    p_append.add_argument("--engine", required=True)
    p_append.add_argument("--survived", type=int, required=True)
    p_append.add_argument("--total-mutants", type=int, default=None)
    p_append.add_argument("--date", default=None, help="override date (testing)")

    p_verify = sub.add_parser("verify", help="verify the hash chain")
    p_verify.add_argument("--ledger", required=True)

    p_show = sub.add_parser("show", help="print records")
    p_show.add_argument("--ledger", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "append":
            record = append_entry(Path(args.ledger), args.repo, args.score_pct,
                                  args.engine, args.survived,
                                  total_mutants=args.total_mutants, date=args.date)
            print(json.dumps(record, ensure_ascii=False))
            return 0
        if args.command == "verify":
            ok, errors = verify(Path(args.ledger))
            if ok:
                print(f"OK: ledger chain verified ({Path(args.ledger)})")
                return 0
            for err in errors:
                print(f"FAIL: {err}", file=sys.stderr)
            return 1
        if args.command == "show":
            for record in _read_records(Path(args.ledger)):
                print(json.dumps(record, ensure_ascii=False))
            return 0
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
