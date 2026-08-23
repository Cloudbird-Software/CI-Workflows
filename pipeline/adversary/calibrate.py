#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""calibrate.py —— criteria 变更时 verifier golden set 重新标定（W5-C1 .github#286，AC-8）

职责：
  - 检测 criteria 文件是否变更（SHA 比对）；
  - 变更时重新运行 golden replay，建立新的 baseline；
  - 标定记录落盘（含 criteria SHA、golden 版本、标定时间、replay 结果摘要）；
  - SHA 强一致校验：回放时若 criteria SHA 与最近标定记录不符，fail-closed（AC-2）；
  - 标定记录 append-only 留档，供 drift-check / 审计重放消费。

与 golden_set.py 关系：calibrate 是"写端"（建立标定），golden_set.py verify 是"读端"
（校验当前 criteria SHA 与标定记录一致）。两者共享 CALIBRATION_DIR。

用法:
  python3 pipeline/adversary/calibrate.py run --criteria criteria/X.yaml --golden fixtures/golden/golden-set.json
  python3 pipeline/adversary/calibrate.py status [--criteria criteria/X.yaml]
  python3 pipeline/adversary/calibrate.py list

退出码：0=标定成功 / 无需重标定（SHA 未变） | 1=replay 回归失败 | 2=infra/配置错误
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_DIR = os.path.join(HERE, "fixtures", "golden", "calibration")
RECORD_PREFIX = "calib-"

try:
    import yaml  # noqa: F401
except ImportError:  # noqa: BLE001
    print("FATAL: 需要 PyYAML（pip install pyyaml==6.0.3）", file=sys.stderr)
    sys.exit(2)


def err(msg: str) -> None:
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)


def die(code: int, msg: str) -> None:
    err(msg)
    sys.exit(code)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return "sha256:" + hashlib.sha256(f.read()).hexdigest()


def latest_record(calib_dir: str) -> dict | None:
    paths = sorted(glob.glob(os.path.join(calib_dir, f"{RECORD_PREFIX}*.json")))
    if not paths:
        return None
    with open(paths[-1], encoding="utf-8") as f:
        return json.load(f)


def list_records(calib_dir: str) -> list[dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(calib_dir, f"{RECORD_PREFIX}*.json"))):
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
            d["__file"] = os.path.basename(p)
            out.append(d)
    return out


def run_replay(golden_path: str, criteria_path: str | None) -> dict:
    """调用 golden_set.replay 执行全量重放。"""
    from golden_set import load_golden, replay  # local import 避免循环
    golden = load_golden(golden_path)
    golden["__path"] = golden_path
    return replay(golden, criteria_path)


def write_record(calib_dir: str, criteria_sha: str, golden_sha: str, replay_res: dict,
                 criteria_path: str, golden_path: str) -> str:
    Path(calib_dir).mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc)
    # 文件名中剥除 ':'（Windows 文件名非法字符）——保留 SHA 可逆性
    sha_tag = criteria_sha.replace(":", "_")[:12]
    fname = f"{RECORD_PREFIX}{ts.strftime('%Y%m%dT%H%M%SZ')}-{sha_tag}.json"
    path = os.path.join(calib_dir, fname)
    record = {
        "schema": "verifier-calibration-record/v1",
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "criteria_path": os.path.abspath(criteria_path) if criteria_path else None,
        "criteria_sha256": criteria_sha,
        "golden_path": os.path.abspath(golden_path),
        "golden_sha256": golden_sha,
        "result": {
            "total": replay_res["total"],
            "passed": replay_res["passed"],
            "failed": replay_res["failed"],
        },
        "sample_verdicts": [
            {"id": s["id"], "expected": s["expected"], "actual": s["actual"], "ok": s["ok"]}
            for s in replay_res["samples"]
        ],
    }
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return path


def cmd_run(args) -> int:
    if not args.criteria or not os.path.isfile(args.criteria):
        die(2, f"需要 --criteria（文件不存在：{args.criteria}）")
    if not args.golden or not os.path.isfile(args.golden):
        die(2, f"需要 --golden（文件不存在：{args.golden}）")

    criteria_sha = sha256_file(args.criteria)
    golden_sha = sha256_file(args.golden)

    # SHA 未变 → 无需重标定（幂等）
    prev = latest_record(CALIBRATION_DIR)
    if prev and prev.get("criteria_sha256") == criteria_sha and prev.get("golden_sha256") == golden_sha:
        print(f"标定未变（criteria {criteria_sha[:11]} / golden {golden_sha[:11]}）— 无需重标定")
        print(f"最近标定记录：{prev.get('__file', '(已落盘)')}")
        return 0

    print(f"criteria SHA {'变更' if prev else '首次标定'}："
          f"{prev['criteria_sha256'][:11] if prev else '无'} -> {criteria_sha[:11]}")

    # 全量重放
    res = run_replay(args.golden, args.criteria)
    from golden_set import print_replay_summary
    print_replay_summary(res)

    if res["failed"]:
        err(f"golden replay 回归失败 {res['failed']}/{res['total']} — 标定拒绝落盘（fail-closed）")
        return 1

    path = write_record(CALIBRATION_DIR, criteria_sha, golden_sha, res, args.criteria, args.golden)
    print(f"标定记录已落盘：{path}")
    return 0


def cmd_status(args) -> int:
    rec = latest_record(CALIBRATION_DIR)
    if not rec:
        print("尚无标定记录")
        return 0
    print(f"最近标定：{rec.get('ts')}  criteria_sha256={rec.get('criteria_sha256','')[:11]}  "
          f"golden_sha256={rec.get('golden_sha256','')[:11]}  "
          f"replay={rec['result']['passed']}/{rec['result']['total']}")
    if args.criteria and os.path.isfile(args.criteria):
        cur = sha256_file(args.criteria)
        match = cur == rec.get("criteria_sha256")
        print(f"当前 criteria SHA：{cur[:11]}  {'一致' if match else '【变更未标定】'}")
        return 0 if match else 2
    return 0


def cmd_list(args) -> int:
    records = list_records(CALIBRATION_DIR)
    if not records:
        print("尚无标定记录")
        return 0
    for r in records:
        print(f"{r.get('__file','')}  {r.get('ts')}  criteria={r.get('criteria_sha256','')[:11]}  "
              f"golden={r.get('golden_sha256','')[:11]}  replay={r['result']['passed']}/{r['result']['total']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="calibrate.py", description="verifier golden set 标定")
    ap.add_argument("cmd", choices=["run", "status", "list"])
    ap.add_argument("--criteria", default="", help="criteria YAML 路径")
    ap.add_argument("--golden", default="", help="golden set JSON 路径")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "status":
        return cmd_status(args)
    return cmd_list(args)


if __name__ == "__main__":
    sys.exit(main())
