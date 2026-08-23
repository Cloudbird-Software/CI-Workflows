#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""calibrate.py —— criteria 变更重标定 + SHA 强一致校验 + 标定记录留档（W5-C1 .github#286 / AC-8）

职责：
  1. 检测 criteria 文件是否变更（对比标定记录中的 criteria_sha256 与当前文件 SHA）；
  2. 变更时重新标定：重跑 golden set 全量回放，确认已知不合格样本仍不合格；
  3. SHA 强一致：标定记录必须与 criteria 文件 SHA 严格一致，不一致即判红（fail-closed）；
  4. 标定记录落盘（calibration/ 目录），含 criteria SHA、golden 版本、定标时间、回放结果摘要。

与 golden_set.py 接口兼容：标定过程调用 golden_set.py regress（native 模式，免 LLM）。

用法:
  python3 calibrate.py <command> [options]
    status     查看当前 criteria SHA 与最近标定记录是否一致
    run        执行标定（变更时重跑 golden；未变更时校验 SHA 一致）
    verify     仅校验标定记录与 criteria SHA 一致（CI 常驻断言）

退出码：0=一致/标定通过 | 1=标定失败（回归失败） | 2=不一致/配置错误（fail-closed）
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN_SET_PY = os.path.join(HERE, "golden_set.py")
DEFAULT_SAMPLES_DIR = os.path.join(HERE, "fixtures", "golden", "samples")
DEFAULT_CALIBRATION_DIR = os.path.join(HERE, "fixtures", "golden", "calibration")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return "sha256:" + hashlib.sha256(f.read()).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def die(code: int, msg: str) -> None:
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)
    sys.exit(code)


def golden_version(samples_dir: str) -> str:
    """golden set 版本 = 样本内容整体 SHA（任意样本变更 → 版本变 → 需重标定）。"""
    h = hashlib.sha256()
    for p in sorted(Path(samples_dir).glob("*.json")):
        h.update(p.name.encode("utf-8"))
        h.update(p.read_bytes())
    return "sha256:" + h.hexdigest()


def latest_calibration(calibration_dir: str) -> dict | None:
    d = Path(calibration_dir)
    if not d.is_dir():
        return None
    recs = sorted(d.glob("calibration-*.json"), reverse=True)
    if not recs:
        return None
    try:
        return json.loads(recs[0].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _extract_json_block(text: str) -> dict:
    """从混合输出中提取首个完整 JSON 对象块（支持跨行）。"""
    stripped = text.strip()
    # 优先：整体就是 JSON
    try:
        return json.loads(stripped)
    except Exception:  # noqa: BLE001
        pass
    # 从首个 '{' 起，用括号深度匹配到闭合
    start = stripped.find("{")
    if start < 0:
        return {}
    depth = 0
    for i in range(start, len(stripped)):
        c = stripped[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(stripped[start:i + 1])
                except Exception:  # noqa: BLE001
                    break
    return {}


def run_golden_regress(samples_dir: str, run_id: str) -> tuple[int, dict]:
    """调用 golden_set.py regress（native），返回 (rc, summary_dict)。"""
    cmd = [sys.executable, GOLDEN_SET_PY, "regress",
           "--samples-dir", samples_dir,
           "--run-id", run_id,
           "--mode", "native"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    summary = _extract_json_block(proc.stdout)
    return proc.returncode, summary


def write_calibration_record(calibration_dir: str, criteria_path: str, criteria_sha: str,
                             golden_ver: str, run_id: str, regress_summary: dict) -> str:
    Path(calibration_dir).mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc)
    rec = {
        "schema": "golden-calibration/v1",
        "calibrated_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id,
        "criteria_file": os.path.abspath(criteria_path),
        "criteria_sha256": criteria_sha,
        "golden_version": golden_ver,
        "golden_samples_dir": os.path.abspath(DEFAULT_SAMPLES_DIR),
        "regress": {
            "mode": "native",
            "total": regress_summary.get("total", 0),
            "passed": regress_summary.get("passed", 0),
            "failed": regress_summary.get("failed", 0),
            "regress_ok": regress_summary.get("regress_ok", False),
        },
    }
    fname = f"calibration-{ts.strftime('%Y%m%dT%H%M%SZ')}.json"
    fpath = os.path.join(calibration_dir, fname)
    Path(fpath).write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                           encoding="utf-8", newline="\n")
    return fpath


def cmd_status(args) -> int:
    if not os.path.isfile(args.criteria):
        die(2, f"criteria 文件不存在: {args.criteria}")
    cur_sha = sha256_file(args.criteria)
    g_ver = golden_version(args.samples_dir)
    rec = latest_calibration(args.calibration_dir)
    print(f"criteria: {args.criteria}")
    print(f"  current_sha256 : {cur_sha}")
    print(f"  golden_version : {g_ver}")
    if rec is None:
        print("  calibration    : 无标定记录（需先运行 calibrate.py run）")
        return 2
    print(f"  last_calibrated: {rec.get('calibrated_at')}")
    print(f"  recorded_sha256: {rec.get('criteria_sha256')}")
    print(f"  recorded_golden: {rec.get('golden_version')}")
    sha_ok = rec.get("criteria_sha256") == cur_sha
    golden_ok = rec.get("golden_version") == g_ver
    print(f"  sha_consistent : {'YES' if sha_ok else 'NO  ← 需重标定'}")
    print(f"  golden_uptodate: {'YES' if golden_ok else 'NO  ← 需重标定'}")
    return 0 if (sha_ok and golden_ok) else 2


def cmd_run(args) -> int:
    if not os.path.isfile(args.criteria):
        die(2, f"criteria 文件不存在: {args.criteria}")
    cur_sha = sha256_file(args.criteria)
    g_ver = golden_version(args.samples_dir)
    rec = latest_calibration(args.calibration_dir)

    need_run = True
    if rec and rec.get("criteria_sha256") == cur_sha and rec.get("golden_version") == g_ver:
        need_run = False
        print(f"标定未变更（criteria SHA + golden 版本均一致），跳过重跑 @ {rec.get('calibrated_at')}")

    regress_summary: dict = rec.get("regress", {}) if rec else {}
    if need_run:
        print(f"执行 golden set 重标定（criteria={cur_sha} golden={g_ver}）...")
        rc, regress_summary = run_golden_regress(args.samples_dir, args.run_id)
        if rc != 0:
            die(1, f"标定失败：golden set 回归未通过（rc={rc}）—— criteria 变更导致 gate 失效或样本损坏")
        print(f"回归通过：{regress_summary.get('passed')}/{regress_summary.get('total')} 条断言成立")
        fpath = write_calibration_record(args.calibration_dir, args.criteria, cur_sha,
                                        g_ver, args.run_id, regress_summary)
        print(f"标定记录落盘: {fpath}")
    else:
        # 未变更也校验回归结果仍在
        if not regress_summary.get("regress_ok"):
            die(2, "标定记录存在但历史回归未通过（regress_ok=false）—— 需人工复核")
    return 0


def cmd_verify(args) -> int:
    """CI 常驻断言：标定记录必须与当前 criteria SHA + golden 版本强一致。"""
    if not os.path.isfile(args.criteria):
        die(2, f"criteria 文件不存在: {args.criteria}")
    cur_sha = sha256_file(args.criteria)
    g_ver = golden_version(args.samples_dir)
    rec = latest_calibration(args.calibration_dir)
    if rec is None:
        die(2, "无标定记录（calibration/ 为空）—— criteria 变更未重标定，fail-closed")
    sha_ok = rec.get("criteria_sha256") == cur_sha
    golden_ok = rec.get("golden_version") == g_ver
    regress_ok = rec.get("regress", {}).get("regress_ok", False)
    if not sha_ok:
        die(2, f"标定 SHA 不一致：记录={rec.get('criteria_sha256')} 当前={cur_sha} "
                f"—— criteria 变更未重标定（AC-8 fail-closed）")
    if not golden_ok:
        die(2, f"标定 golden 版本不一致：记录={rec.get('golden_version')} 当前={g_ver} "
                f"—— golden set 变更未重标定（AC-8 fail-closed）")
    if not regress_ok:
        die(2, "标定记录中历史回归未通过（regress_ok=false）—— fail-closed")
    print(f"标定校验通过：criteria={cur_sha} golden={g_ver} "
          f"calibrated_at={rec.get('calibrated_at')}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="calibrate.py", description="criteria 变更重标定 + SHA 强一致校验")
    ap.add_argument("cmd", choices=["status", "run", "verify"])
    ap.add_argument("--criteria", required=True, help="被标定的 criteria YAML 文件路径")
    ap.add_argument("--samples-dir", default=DEFAULT_SAMPLES_DIR, help="golden 样本目录")
    ap.add_argument("--calibration-dir", default=DEFAULT_CALIBRATION_DIR, help="标定记录目录")
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", now_iso()))
    args = ap.parse_args(argv)

    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
