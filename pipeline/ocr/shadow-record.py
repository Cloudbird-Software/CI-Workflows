#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shadow-record.py —— shadow 建议落 JSONL 记录（W2-C4 .github#217 / ADR-0063 决策 3/4）

后处理保留的建议 → JSONL（artifact 路径 ocr-shadow/<date>.jsonl），是 precision.py
（post-fix 基准）的唯一数据源。每行一条 JSON：
  record=suggestion —— 一条保留建议（含 repo/pr/head_sha/run_id 定位元数据）
  record=summary   —— 本次 run 的过滤统计（total/kept/dropped-by-reason——过滤率指标本体）
N/A 诚实降级（无凭据跳过）：postprocess 对 status=skipped 输出全零统计，本脚本
照常写 summary 行（skipped 不伪装成运行过的评审——AC 与 ADR「诚实降级不伪装运行」）。

零网络零推理；输入缺失/损坏 exit 2（fail-closed）。
用法：
  python3 shadow-record.py --kept F --stats F --out-dir D \
      --repo R --pr N --head-sha S --run-id S --model M --event E \
      [--ocr-version V] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

SCHEMA = "ocr-shadow/v1"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="OCR shadow JSONL 记录（ADR-0063）")
    for req in ("kept", "stats", "out-dir", "repo", "pr", "head-sha", "run-id", "model", "event"):
        ap.add_argument(f"--{req}", required=True)
    ap.add_argument("--ocr-version", default="")
    ap.add_argument("--date", default=None, help="记录归属日期（缺省 UTC 今日，测试可钉死）")
    args = ap.parse_args(argv)

    try:
        with open(args.kept, encoding="utf-8") as f:
            kept = json.load(f)
        with open(args.stats, encoding="utf-8") as f:
            stats = json.load(f)
        if not isinstance(kept, list) or not isinstance(stats, dict):
            raise ValueError("kept 须为数组、stats 须为对象")
        if not str(args.pr).isdigit():
            raise ValueError("--pr 须为数字（PR 号）")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"::error::输入不可用（fail-closed）: {e}", file=sys.stderr)
        return 2

    date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = {"schema": SCHEMA, "ts": ts, "repo": args.repo, "pr": int(args.pr),  # 已验证为数字
            "head_sha": args.head_sha, "run_id": args.run_id, "event": args.event}
    if args.ocr_version:
        base["ocr"] = {"version": args.ocr_version}
    base["model"] = args.model

    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(args.out_dir, f"{date}.jsonl")
    n = 0
    with open(out, "a", encoding="utf-8", newline="\n") as f:
        for s in kept:
            f.write(json.dumps({**base, "record": "suggestion", "suggestion": s},
                               ensure_ascii=False) + "\n")
            n += 1
        f.write(json.dumps({**base, "record": "summary", "stats": stats},
                           ensure_ascii=False) + "\n")
    print(f"shadow 记录 → {out}（suggestion×{n} + summary×1，schema {SCHEMA}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
