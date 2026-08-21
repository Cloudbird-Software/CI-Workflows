#!/usr/bin/env python3
# unseal_log_mirror.py —— holdout scripts/unseal-log.py 的逐字节镜像（测试 fixture）
# 来源：Cloudbird-Software/holdout@w4c3-seal-scripts scripts/unseal-log.py
# 同步纪律：holdout 仓该文件变更时须同步本镜像；e2e 演示（holdout-unseal.yml
# demo job）用 holdout 真仓脚本，本镜像只服务单元级 append-only 断言（零网络）。
# 以下为镜像正文（勿手改）：
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SCHEMA_ID = "holdout-unseal-record/1"
VERDICTS = {"pass", "gap-escalated", "tamper", "env-fail"}
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ID_RE, SHA8_RE = re.compile(r"^HO-[0-9]{4}$"), re.compile(r"^[0-9a-f]{8}$")
REQUIRED = ["schema", "ts", "repo", "pr", "run_id", "verdict", "entries", "passed", "total"]
GENESIS = "sha256:GENESIS"


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def line_hash(rec: dict) -> str:
    return "sha256:" + hashlib.sha256(
        canon({k: v for k, v in rec.items() if k != "record_hash"}).encode("utf-8")).hexdigest()


def check_record(rec, where: str, errs: list):
    """字段齐全性判据（append 收件与 verify 巡检共用）。"""
    if not isinstance(rec, dict):
        errs.append(f"{where}: 记录非 JSON 对象"); return
    for k in REQUIRED:
        if k not in rec:
            errs.append(f"{where}: 缺字段 {k}")
    if any(f.startswith(f"{where}: 缺字段") for f in errs):
        return  # 必填已缺则后续判据连锁误报
    if rec.get("schema") != SCHEMA_ID:
        errs.append(f"{where}: schema={rec.get('schema')!r} 应为 {SCHEMA_ID}")
    if not (isinstance(rec.get("ts"), str) and TS_RE.match(rec["ts"])):
        errs.append(f"{where}: ts 非 RFC3339(Z): {rec.get('ts')!r}")
    if not (isinstance(rec.get("repo"), str) and rec["repo"]):
        errs.append(f"{where}: repo 非法")
    if not (isinstance(rec.get("pr"), int) and not isinstance(rec.get("pr"), bool) and rec["pr"] >= 0):
        errs.append(f"{where}: pr 应为非负整数: {rec.get('pr')!r}")
    if not (isinstance(rec.get("run_id"), str) and rec["run_id"]):
        errs.append(f"{where}: run_id 非法")
    if rec.get("verdict") not in VERDICTS:
        errs.append(f"{where}: verdict={rec.get('verdict')!r} 不在 {sorted(VERDICTS)}")
    ents = rec.get("entries")
    if not (isinstance(ents, list) and ents):
        errs.append(f"{where}: entries 须为非空数组（sealed_sha256 校验结果必在，AC-3）")
    else:
        for e in ents:
            if not (isinstance(e, dict) and ID_RE.match(str(e.get("id", "")))
                    and SHA8_RE.match(str(e.get("sha8", ""))) and isinstance(e.get("verify"), bool)):
                errs.append(f"{where}: entries 元素形状非法（须 id/sha8/verify）"); break
    for k in ("passed", "total"):
        if not (isinstance(rec.get(k), int) and not isinstance(rec.get(k), bool) and rec[k] >= 0):
            errs.append(f"{where}: {k} 应为非负整数: {rec.get(k)!r}")
    if rec.get("passed", 0) > rec.get("total", 0):
        errs.append(f"{where}: passed={rec.get('passed')} > total={rec.get('total')} 不可能")
    if not isinstance(rec.get("escalated", False), bool):
        errs.append(f"{where}: escalated 应为布尔")
    elif rec.get("escalated") != (rec.get("verdict") == "gap-escalated"):
        errs.append(f"{where}: escalated 与 verdict=gap-escalated 不一致")


def read_lines(ledger: Path):
    if not ledger.exists():
        return []
    return [ln for ln in ledger.read_text(encoding="utf-8").split("\n") if ln.strip()]


def cmd_append(args) -> int:
    raw = sys.stdin.read() if args.record == "-" else Path(args.record).read_text(encoding="utf-8")
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"FAIL  记录 JSON 解析失败（fail-closed 拒收）: {exc}", file=sys.stderr); return 2
    errs: list[str] = []
    check_record(rec, "record", errs)
    if errs:
        print("\n".join(f"FAIL  {e}" for e in errs), file=sys.stderr); return 2
    lines = read_lines(args.ledger)
    prev = GENESIS
    if lines:
        try:
            last = json.loads(lines[-1])
            prev = last.get("record_hash", "")
            if not str(prev).startswith("sha256:"):
                raise ValueError("尾行无 record_hash")
        except (json.JSONDecodeError, ValueError) as exc:
            # 尾行坏=台账已损——绝不盲接（盲接=新记录挂不明链，历史不可审）
            print(f"FAIL  台账尾行不可解析，拒绝追加（先 verify 定位）: {exc}", file=sys.stderr); return 1
    rec = dict(rec)
    rec["prev_hash"] = prev
    rec["record_hash"] = line_hash(rec)
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    # append-only：只以追加模式打开；newline="\n" 防 Windows CRLF（字节级回放稳定）
    with args.ledger.open("a", encoding="utf-8", newline="\n") as f:
        f.write(canon(rec) + "\n")
    print(f"OK    台账追加 1 行（run_id={rec['run_id']} verdict={rec['verdict']} "
          f"passed={rec['passed']}/{rec['total']} prev={prev[:14]}… 共 {len(lines) + 1} 行）")
    return 0


def cmd_verify(args) -> int:
    lines = read_lines(args.ledger)
    if not lines:
        print("FAIL  台账为空或不存在（缺席即停，不视为通过——宪法 §6）", file=sys.stderr); return 1
    errs, prev = [], GENESIS
    for i, ln in enumerate(lines, 1):
        where = f"第{i}行"
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError as exc:
            errs.append(f"{where}: JSON 坏行: {exc}"); continue
        check_record(rec, where, errs)
        if rec.get("prev_hash") != prev:
            errs.append(f"{where}: prev_hash 断链（期望 {prev[:14]}… 实得 {str(rec.get('prev_hash'))[:14]}…）")
        if rec.get("record_hash") != line_hash(rec):
            errs.append(f"{where}: record_hash 不符（行被改写？append-only 禁改历史）")
        prev = rec.get("record_hash", "?")
    if errs:
        print("\n".join(f"FAIL  {e}" for e in errs) + f"\n结果: 台账 {len(lines)} 行，{len(errs)} 项违规",
              file=sys.stderr)
        return 1
    t_ok = sum(1 for ln in lines if json.loads(ln).get("verdict") == "pass")
    print(f"OK    台账验链全绿：{len(lines)} 行（pass={t_ok}），hash 链完整、历史未改写")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="揭封 append-only 台账（W4-C3 / ADR-0068 决策 4）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    default_ledger = str(Path(__file__).resolve().parent.parent / "ledger" / "unseal.jsonl")
    pa = sub.add_parser("append", help="追加一行揭封记录（改历史=断链=verify 红）")
    pa.add_argument("--record", required=True, help="记录 JSON 文件路径，或 - 读 stdin")
    pa.add_argument("--ledger", default=default_ledger)
    pv = sub.add_parser("verify", help="验链+字段巡检")
    pv.add_argument("--ledger", default=default_ledger)
    args = ap.parse_args()
    if hasattr(args, "record") and args.record != "-":
        args.record = Path(args.record)
    args.ledger = Path(args.ledger)
    return cmd_append(args) if args.cmd == "append" else cmd_verify(args)


if __name__ == "__main__":
    sys.exit(main())
