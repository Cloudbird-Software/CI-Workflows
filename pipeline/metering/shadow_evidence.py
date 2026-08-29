#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shadow_evidence.py —— metering 侧证据账本 schema v1 影子双写（IR-0006 W1-B2 / ADR-0103）

BEH-03 三源渐进对齐：metering 新事件在落 records-*.jsonl（原格式，只增不改、
冻结只读过渡）的同时，按统一证据 schema v1（.github standards/evidence/
record.schema.yaml，cloudbird/evidence-standard/record@1）双写影子账本
shadow-evidence-<ISO 周>.jsonl——每周片一条独立链（与 metering 周片同构）。

影子记录字段（kind=cost，OTel gen_ai.* 对齐）：
  ts/kind/action/verdict/subject{card,tenant}/actor{identity,role,model}/
  cost{tokens,wall_sec}/inputs_digest/seq/prev_hash/hash

子命令：
  append  事件文件 → 校验（tenant/card 必填、payload ≤4096B、链字段独占）+
          链式追加当周影子片（metering.py emit 双写调用）
  verify  影子片验链：seq 连续 / prev_hash 链 / hash 重算 / tenant 复检
  relink  本地影子片续接远端基链（ledger-sync 合并用；两侧先各自验链）

card 约定：卡绑定用 owner/repo#issue（join key，AC-4）；无卡上下文的基建调用
用哨兵 <repo>#0（如 Cloudbird-Software/CI-Workflows#0）——#0 即"未绑定卡"，
不参与卡聚合。
退出码：0=成功 | 2=参数/环境 | 3=记录/链无效（fail-closed，不落链）
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys

PAYLOAD_LIMIT = 4096
KINDS = {"gate", "cost", "approval", "decision"}
ROLES = {"owner", "agent", "bot", "human"}
CARD_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$")


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(rec):
    return hashlib.sha256(
        canonical({k: v for k, v in rec.items() if k != "hash"}).encode("utf-8")).hexdigest()


def week_shard(ts: str) -> str:
    d = dt.datetime.strptime(ts[:10], "%Y-%m-%d").date()
    return "shadow-evidence-{}-W{:02d}.jsonl".format(*d.isocalendar()[:2])


def validate_event(ev: dict):
    if {"seq", "prev_hash", "hash"} & set(ev):
        die(3, "影子事件不得自带 seq/prev_hash/hash（链字段由写入器独占）")
    if ev.get("kind") not in KINDS:
        die(3, f"kind 非法: {ev.get('kind')!r}（合法 {sorted(KINDS)}）")
    for k in ("ts", "action", "verdict"):
        if not str(ev.get(k) or "").strip():
            die(3, f"{k} 必填")
    subject = ev.get("subject")
    if not isinstance(subject, dict):
        die(3, "subject 必填（对象）")
    if not str(subject.get("tenant") or "").strip():
        die(3, "subject.tenant 必填（AC-4c：多租户计量分离）")
    if not CARD_RE.fullmatch(str(subject.get("card") or "")):
        die(3, "subject.card 必填且形如 owner/repo#issue（AC-4 join key）")
    actor = ev.get("actor")
    if not isinstance(actor, dict) or not str(actor.get("identity") or "").strip() \
            or actor.get("role") not in ROLES:
        die(3, "actor 四元不齐（identity/role 必填，role ∈ owner/agent/bot/human）")
    payload = ev.get("payload", None)
    if payload is not None:
        if not isinstance(payload, str):
            die(3, "payload 须为字符串或 null")
        if len(payload.encode("utf-8")) > PAYLOAD_LIMIT:
            die(3, f"payload {len(payload.encode('utf-8'))}B > {PAYLOAD_LIMIT}B（INV-06：超限拒写）")


def read_lines(path: str):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [ln for ln in (l.strip() for l in f) if ln]


def append(dir_: str, ev: dict) -> dict:
    validate_event(ev)
    shard = os.path.join(dir_, week_shard(ev["ts"]))
    lines = read_lines(shard)
    rec = {k: v for k, v in ev.items() if v is not None}
    rec["seq"] = len(lines) + 1
    rec["prev_hash"] = json.loads(lines[-1])["hash"] if lines else None
    rec["hash"] = content_hash(rec)
    os.makedirs(dir_, exist_ok=True)
    with open(shard, "a", encoding="utf-8", newline="\n") as f:
        f.write(canonical(rec) + "\n")
    return rec


def verify_file(path: str) -> list:
    errs, prev = [], None
    for i, ln in enumerate(read_lines(path), 1):
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError as e:
            errs.append(f"{os.path.basename(path)} 第 {i} 行 JSON 畸形: {e}")
            break
        if rec.get("seq") != i:
            errs.append(f"{path} 第 {i} 行 seq={rec.get('seq')} 断号")
        if rec.get("prev_hash") != prev:
            errs.append(f"{path} 第 {i} 行 prev_hash 断链")
        if rec.get("hash") != content_hash(rec):
            errs.append(f"{path} 第 {i} 行 hash 重算不符（篡改）")
        if not str((rec.get("subject") or {}).get("tenant") or "").strip():
            errs.append(f"{path} 第 {i} 行 tenant 缺失（AC-4c）")
        if not CARD_RE.fullmatch(str((rec.get("subject") or {}).get("card") or "")):
            errs.append(f"{path} 第 {i} 行 card 缺失/非法（AC-4）")
        payload = rec.get("payload", None)
        if isinstance(payload, str) and len(payload.encode("utf-8")) > PAYLOAD_LIMIT:
            errs.append(f"{path} 第 {i} 行 payload 超限（执法缺口）")
        prev = rec.get("hash")
    return errs


def shadow_files(dir_: str):
    if not os.path.isdir(dir_):
        return []
    return sorted(f for f in os.listdir(dir_) if f.startswith("shadow-evidence-") and f.endswith(".jsonl"))


def main():
    ap = argparse.ArgumentParser(prog="shadow_evidence.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("append")
    p.add_argument("--dir", required=True, help="GATE_METERING_DIR（影子片同目录）")
    p.add_argument("--event-file", required=True)

    p = sub.add_parser("verify")
    p.add_argument("--dir", default=None)
    p.add_argument("--file", default=None)

    p = sub.add_parser("relink")
    p.add_argument("--base", required=True, help="远端基链片（无则从本地片首建）")
    p.add_argument("--local", required=True)
    p.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.cmd == "append":
        with open(args.event_file, encoding="utf-8") as f:
            ev = json.load(f)
        rec = append(args.dir, ev)
        print(canonical(rec))

    elif args.cmd == "verify":
        files = [args.file] if args.file else shadow_files(args.dir or ".")
        if not files:
            die(2, "无影子片可验")
        errs = []
        for fn in files:
            errs.extend(verify_file(fn))
        if errs:
            for e in errs:
                print(f"CHAIN {e}", file=sys.stderr)
            die(3, f"影子验链失败：{len(errs)} 处")
        for fn in files:
            print(f"OK {fn}: {len(read_lines(fn))} 条，链完整")

    elif args.cmd == "relink":
        errs = []
        base_recs = []
        if os.path.isfile(args.base):
            errs.extend(verify_file(args.base))
            base_recs = [json.loads(l) for l in read_lines(args.base)]
        errs.extend(verify_file(args.local))
        if errs:
            for e in errs[:10]:
                print(f"CHAIN {e}", file=sys.stderr)
            die(3, "基链或本地影子片验链失败——拒绝合并（防覆盖掩盖篡改）")
        merged, prev = list(base_recs), None
        for rec in [json.loads(l) for l in read_lines(args.local)]:
            rec = dict(rec)
            rec["seq"] = len(merged) + 1
            rec["prev_hash"] = prev
            rec["hash"] = content_hash(rec)
            merged.append(rec)
            prev = rec["hash"]
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            for rec in merged:
                f.write(canonical(rec) + "\n")
        print(f"relink → {args.out}（base {len(base_recs)} + local {len(merged) - len(base_recs)} 条）")


if __name__ == "__main__":
    main()
