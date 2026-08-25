#!/usr/bin/env python3
"""fan-out 消费者（AC-13 rev6：消费者常在）——红队/道闸燃料适配器。

PM 优先范式：fan-out 生产者可选，消费者常在。
- products 目录为空：加 --empty-ok 即合法（输出 {consumed:0, reason:"no products
  (fan-out 未使用)"}，exit 0）；不加则 exit 1（消费者不得静默停摆）。
- 非空：逐条 JSONL 校验——
  * type ∈ {skeleton_divergence, assumption, eliminated_route, diff_divergence}
  * card_id / spec_hash / base_sha / type / prev_hash 必填
  * append-only 哈希链：首行 prev_hash 为 64 个 '0'（genesis），其后每行 prev_hash
    必须等于上一行整记录的 SHA-256（规范化 JSON：sort_keys=True、
    separators=(",", ":")、ensure_ascii=False，UTF-8 编码，见 record_hash）。
    断链 / 非法 type / 缺字段 → exit 1。
- --expect-base-sha <sha>：记录的 base_sha 不符 → 卡片作废留痕（invalidations
  台账记录 file/line/card_id/期望/实际）并 exit 1。
- type == eliminated_route：从 payload 字段（route/summary/points/routes）机械生成
  差异攻击查询文本清单，前缀固定为 "champion 是否覆盖："。

用法：
  python fanout/consumer.py --products-dir <dir> [--empty-ok] \
      [--expect-base-sha <sha>] [--ledger <json>]

退出码：0=消费完成（或授权空）；1=目录缺失/未授权空/记录非法/断链/SHA 不符。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TYPES = ("skeleton_divergence", "assumption", "eliminated_route", "diff_divergence")
REQUIRED_FIELDS = ("card_id", "spec_hash", "base_sha", "type", "prev_hash")
GENESIS = "0" * 64
QUERY_PREFIX = "champion 是否覆盖："
EMPTY_REASON = "no products (fan-out 未使用)"
PAYLOAD_KEYS = ("route", "summary", "points", "routes")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_hash(record):
    return hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()


def attack_queries(payload):
    """从 payload 字段机械生成攻击查询文本（固定顺序，不引入自由发挥）。"""
    queries = []
    for key in PAYLOAD_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        items = value if isinstance(value, list) else [value]
        for item in items:
            queries.append(QUERY_PREFIX + str(item))
    if not queries:
        queries.append(QUERY_PREFIX + canonical(payload))
    return queries


def main(argv=None):
    parser = argparse.ArgumentParser(prog="consumer.py", description="fan-out 消费者（AC-13：消费者常在）")
    parser.add_argument("--products-dir", required=True, help="fan-out 产品目录")
    parser.add_argument("--empty-ok", action="store_true", help="授权空目录（fan-out 未使用）")
    parser.add_argument("--expect-base-sha", default=None, help="期望 base_sha（不符=作废留痕 exit 1）")
    parser.add_argument("--ledger", help="消费台账 JSON 输出路径")
    args = parser.parse_args(argv)

    root = Path(args.products_dir)
    if not root.is_dir():
        print("consumer: products 目录不存在: %s" % root, file=sys.stderr)
        return 1
    files = sorted(p for p in root.glob("*.jsonl") if p.is_file())
    if not files:
        if args.empty_ok:
            print(json.dumps({"consumed": 0, "reason": EMPTY_REASON}, ensure_ascii=False))
            return 0
        print("consumer: products 目录为空且未授权 --empty-ok（消费者不得静默停摆）", file=sys.stderr)
        return 1

    consumed = 0
    queries = []
    errors = []
    invalidations = []
    warnings = []
    for path in files:
        prev = GENESIS
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            errors.append("%s: 读取失败 %s" % (path.name, exc))
            continue
        for lineno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append("%s:%d: JSON 解析失败: %s" % (path.name, lineno, exc))
                continue
            if not isinstance(record, dict):
                errors.append("%s:%d: 记录必须是 JSON 对象" % (path.name, lineno))
                continue
            missing = [f for f in REQUIRED_FIELDS if f not in record]
            if missing:
                errors.append("%s:%d: 必填字段缺失: %s" % (path.name, lineno, ",".join(missing)))
            rtype = record.get("type")
            if rtype not in TYPES:
                errors.append("%s:%d: 非法 type %r（允许 %s）"
                              % (path.name, lineno, rtype, "|".join(TYPES)))
            payload = record.get("payload", {})
            if rtype == "eliminated_route":
                if not isinstance(payload, dict) or not payload:
                    errors.append("%s:%d: eliminated_route 需要非空 payload 对象" % (path.name, lineno))
                else:
                    for q in attack_queries(payload):
                        queries.append({"card_id": record.get("card_id"), "query": q})
            if record.get("prev_hash") != prev:
                errors.append("%s:%d: append 链断裂（prev_hash 期望 %s…，实际 %s…）"
                              % (path.name, lineno, prev[:12], str(record.get("prev_hash"))[:12]))
            if args.expect_base_sha and record.get("base_sha") != args.expect_base_sha:
                invalidations.append({
                    "file": path.name,
                    "line": lineno,
                    "card_id": record.get("card_id"),
                    "reason": "base_sha 与 --expect-base-sha 不符，卡片作废留痕",
                    "expected": args.expect_base_sha,
                    "actual": record.get("base_sha"),
                })
            prev = record_hash(record)
            consumed += 1

    result = {
        "consumed": consumed,
        "files": [p.name for p in files],
        "attack_queries": queries,
        "warnings": warnings,
        "date": now_iso(),
    }
    if invalidations:
        result["invalidations"] = invalidations
    if errors:
        result["errors"] = errors
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.ledger:
        try:
            with open(args.ledger, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text + "\n")
        except OSError as exc:
            print("consumer: 台账写入失败 %s" % exc, file=sys.stderr)
            return 1
    if errors or invalidations:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
