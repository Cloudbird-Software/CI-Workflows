#!/usr/bin/env python3
"""corpus —— fuzz 语料库管理（IR-0004 AC-3）：add / list / growth。

语料库目录结构：
  corpus/
    s-<sha256前12位>.json    种子/输入文件（内容寻址命名，天然去重）
    manifest.json            当前在库清单（file/sha256/bytes/generator/added_at）
    ledger.jsonl             append-only 事件台账（add / skip_duplicate / note）

growth 子命令从台账推导增长曲线（JSONL，按日聚合的累计序列）：
  {"date": "2026-08-24", "added": 34, "skipped": 0, "cumulative_seeds": 34, ...}

CLI：
  python corpus.py add    --corpus-dir corpus/ --file seed.json [--generator seedgen]
  python corpus.py add    --corpus-dir corpus/ --dir seeds/  [--generator seedgen]
  python corpus.py list   --corpus-dir corpus/ [--json]
  python corpus.py growth --corpus-dir corpus/ [--out growth.jsonl]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import sys
from pathlib import Path

try:  # 双模式导入：包内 / 独立脚本
    from pipeline.testing import _yamlmini  # noqa: F401  (确保包路径可用)
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

MANIFEST_NAME = "manifest.json"
SEEDGEN_MANIFEST_NAME = "seedgen-manifest.json"
LEDGER_NAME = "ledger.jsonl"


def _utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def load_manifest(corpus_dir):
    path = Path(corpus_dir) / MANIFEST_NAME
    if not path.exists():
        return {"version": 1, "seeds": []}
    return json.loads(path.read_text(encoding="utf-8"))


def load_ledger(corpus_dir):
    path = Path(corpus_dir) / LEDGER_NAME
    events = []
    if path.exists():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError("台账第 %d 行非法 JSON: %s" % (lineno, exc)) from exc
    return events


def _append_event(corpus_dir, event):
    path = Path(corpus_dir) / LEDGER_NAME
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _write_manifest(corpus_dir, manifest):
    path = Path(corpus_dir) / MANIFEST_NAME
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def add_file(corpus_dir, file_path, generator="unknown"):
    """加入单个 JSON 文件；sha256 重复则跳过并记账。返回事件名。"""
    corpus = Path(corpus_dir)
    corpus.mkdir(parents=True, exist_ok=True)
    src = Path(file_path)
    data = src.read_bytes()
    digest = _sha256_bytes(data)
    manifest = load_manifest(corpus)
    if any(seed["sha256"] == digest for seed in manifest["seeds"]):
        event = {
            "ts": _utcnow_iso(),
            "event": "skip_duplicate",
            "file": src.name,
            "sha256": digest,
            "generator": generator,
        }
        _append_event(corpus, event)
        return "skip_duplicate"
    dest_name = "s-%s.json" % digest[:12]
    shutil.copyfile(src, corpus / dest_name)
    manifest["seeds"].append(
        {
            "file": dest_name,
            "sha256": digest,
            "bytes": len(data),
            "generator": generator,
            "added_at": _utcnow_iso(),
        }
    )
    _write_manifest(corpus, manifest)
    _append_event(
        corpus,
        {
            "ts": _utcnow_iso(),
            "event": "add",
            "file": dest_name,
            "sha256": digest,
            "bytes": len(data),
            "generator": generator,
        },
    )
    return "add"


def add_paths(corpus_dir, file_or_dir, generator="unknown"):
    path = Path(file_or_dir)
    if path.is_dir():
        files = sorted(
            p for p in path.glob("*.json")
            if p.name not in (MANIFEST_NAME, SEEDGEN_MANIFEST_NAME)
        )
    else:
        files = [path]
    results = {"add": 0, "skip_duplicate": 0, "files": []}
    for f in files:
        event = add_file(corpus_dir, f, generator=generator)
        results[event] += 1
        results["files"].append({"file": str(f), "event": event})
    return results


def growth_curve(corpus_dir):
    """从台账推导按日增长曲线（累计序列，JSONL 行数组）。"""
    events = load_ledger(corpus_dir)
    by_date = {}
    order = []
    for event in events:
        date = str(event.get("ts", ""))[:10] or "unknown"
        if date not in by_date:
            by_date[date] = {"added": 0, "skipped": 0}
            order.append(date)
        kind = event.get("event")
        if kind == "add":
            by_date[date]["added"] += 1
        elif kind == "skip_duplicate":
            by_date[date]["skipped"] += 1
    rows = []
    cumulative = 0
    for date in sorted(order):
        day = by_date[date]
        cumulative += day["added"]
        rows.append(
            {
                "date": date,
                "added": day["added"],
                "skipped": day["skipped"],
                "cumulative_seeds": cumulative,
            }
        )
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="fuzz 语料库管理（AC-3）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="加入文件/目录到语料库")
    p_add.add_argument("--corpus-dir", required=True)
    p_add.add_argument("--file", help="单个 JSON 文件")
    p_add.add_argument("--dir", dest="dir_", help="目录（批量 *.json）")
    p_add.add_argument("--generator", default="unknown", help="来源生成器标记")

    p_list = sub.add_parser("list", help="列出在库种子")
    p_list.add_argument("--corpus-dir", required=True)
    p_list.add_argument("--json", action="store_true")

    p_growth = sub.add_parser("growth", help="输出增长曲线 JSONL")
    p_growth.add_argument("--corpus-dir", required=True)
    p_growth.add_argument("--out", default=None, help="写出文件（缺省打印 stdout）")

    args = parser.parse_args(argv)

    if args.command == "add":
        if not args.file and not args.dir_:
            parser.error("add 需要 --file 或 --dir")
        target = args.file or args.dir_
        try:
            results = add_paths(args.corpus_dir, target, generator=args.generator)
        except (OSError, ValueError) as exc:
            print("FATAL: add 失败: %s" % exc, file=sys.stderr)
            return 2
        print(json.dumps(results, ensure_ascii=False))
        return 0

    if args.command == "list":
        manifest = load_manifest(args.corpus_dir)
        if args.json:
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
        else:
            print("corpus: %s  seeds: %d" % (args.corpus_dir, len(manifest.get("seeds", []))))
            for seed in manifest.get("seeds", []):
                print("  %-20s %8d B  %-10s %s" % (seed["file"], seed.get("bytes", 0), seed.get("generator", "?"), seed.get("sha256", "")[:16]))
        return 0

    if args.command == "growth":
        try:
            rows = growth_curve(args.corpus_dir)
        except ValueError as exc:
            print("FATAL: growth 失败: %s" % exc, file=sys.stderr)
            return 2
        text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8", newline="\n")
            print(json.dumps({"out": args.out, "rows": len(rows)}, ensure_ascii=False))
        else:
            sys.stdout.write(text)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
