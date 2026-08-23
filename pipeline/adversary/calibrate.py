#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""calibrate.py —— criteria 变更时重新标定，SHA 强一致校验，标定记录落盘（W5-C1 .github#286，AC-8）

criteria 文件每次变更必须重新标定，且标定记录与 criteria SHA 强一致。
本模块：
  1. 计算当前 criteria SHA；
  2. 与既有标定记录比对——一致则跳过（幂等）；
  3. 不一致（criteria 变更/首次标定）→ 重新标定：
     - 加载 golden set 中已知不合格样本；
     - 逐 criterion 跑评分路径，记录连续分；
     - 校验不合格样本仍触发 gate 失败（反特判断言）；
  4. 标定记录落盘（calibrate-record.json），含 criteria_sha、golden_version、
     标定时间、逐 criterion 阈值与实测分布。

与 W3-C3 llm_verifier.py、golden_set.py 接口兼容：
  - 消费 criteria/*.yaml 与 fixtures/golden/；
  - 输出 calibrate-record.json 供 golden_set.py --verify-calibration 消费。

用法:
  python calibrate.py --criteria <criteria.yaml> --golden <golden-dir> \
      [--record-out <record.json>] [--force]
  python calibrate.py --verify --criteria <criteria.yaml> [--record <record.json>]

退出码: 0=标定完成（或幂等跳过）/ 校验通过
        1=标定不一致（不合格样本未被正确识别）
        2=infra/配置错误（fail-closed）
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GOLDEN_DIR = os.path.join(HERE, "fixtures", "golden")
DEFAULT_RECORD_PATH = os.path.join(HERE, "calibrate-record.json")


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


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_criteria(path: str) -> dict:
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        die(2, "需要 PyYAML（pip install pyyaml==6.0.3）")
    except Exception as e:  # noqa: BLE001
        die(2, f"criteria 文件不可读 {path}: {e}")


def load_golden_set(golden_dir: str) -> dict:
    golden_path = os.path.join(golden_dir, "golden-set.json")
    if not os.path.isfile(golden_path):
        die(2, f"golden set 不存在: {golden_path}")
    return load_json(golden_path)


# ---------------------------------------------------------------------------
# 标定核心
# ---------------------------------------------------------------------------

def compute_criterion_stats(golden_dir: str, golden: dict, criteria: dict) -> list[dict]:
    """逐 criterion 统计 golden set 样本的连续分分布。

    对每个 criterion，收集所有样本的 score，计算均值/最小值/最大值/标准差。
    不合格样本（expected_verdict=insufficient）的 score 必须低于阈值。
    """
    criteria_list = criteria.get("criteria") or []
    default_threshold = criteria.get("defaults", {}).get("threshold", 0.7)
    samples = golden.get("samples") or []

    stats = []
    for c in criteria_list:
        cid = c.get("id", "?")
        threshold = c.get("threshold", default_threshold)
        scores_all = []
        scores_fail = []  # 不合格样本的 score
        for s in samples:
            report_path = os.path.join(golden_dir, s.get("report", ""))
            if not os.path.isfile(report_path):
                continue
            report = load_json(report_path)
            criterion_scores = report.get("criterion_scores") or {}
            score = criterion_scores.get(cid, 0.0)
            scores_all.append(score)
            expected = s.get("expected_verdict", report.get("expected_verdict", "insufficient"))
            if expected == "insufficient":
                scores_fail.append(score)

        # 反特判断言：不合格样本的最大 score 必须低于阈值
        fail_max = max(scores_fail) if scores_fail else 0.0
        anti_spoofing_ok = fail_max < threshold

        stat = {
            "id": cid,
            "threshold": threshold,
            "n_samples": len(scores_all),
            "mean": round(statistics.mean(scores_all), 4) if scores_all else 0.0,
            "min": round(min(scores_all), 4) if scores_all else 0.0,
            "max": round(max(scores_all), 4) if scores_all else 0.0,
            "stdev": round(statistics.stdev(scores_all), 4) if len(scores_all) > 1 else 0.0,
            "fail_sample_count": len(scores_fail),
            "fail_max_score": round(fail_max, 4),
            "anti_spoofing_ok": anti_spoofing_ok,
        }
        stats.append(stat)
    return stats


def calibrate(criteria_path: str, golden_dir: str, force: bool = False) -> tuple[dict, bool]:
    """执行标定。返回 (record, is_fresh)。

    is_fresh=True 表示实际做了标定（criteria 变更/首次）；False 表示幂等跳过。
    """
    criteria = load_criteria(criteria_path)
    golden = load_golden_set(golden_dir)
    criteria_sha = sha256_file(criteria_path)

    # 检查既有标定记录
    existing = None
    if os.path.isfile(DEFAULT_RECORD_PATH) and not force:
        try:
            existing = load_json(DEFAULT_RECORD_PATH)
        except Exception:
            existing = None

    if existing and existing.get("criteria_sha") == criteria_sha:
        # 幂等：criteria 未变更，跳过
        return existing, False

    # criteria 变更或首次标定 → 重新标定
    criterion_stats = compute_criterion_stats(golden_dir, golden, criteria)

    # 反特判断言：所有 criterion 的不合格样本必须低于阈值
    all_anti_spoofing_ok = all(s["anti_spoofing_ok"] for s in criterion_stats)

    record = {
        "schema": "calibrate-record/v1",
        "criteria_sha": criteria_sha,
        "criteria_path": os.path.abspath(criteria_path),
        "criteria_card": criteria.get("card", "unknown"),
        "criteria_card_ref": criteria.get("card_ref", "unknown"),
        "criteria_spec_version": criteria.get("spec_version", "unknown"),
        "golden_dir": os.path.abspath(golden_dir),
        "golden_version": golden.get("version", "unknown"),
        "golden_description": golden.get("description", ""),
        "calibrated_at": now_iso(),
        "criterion_stats": criterion_stats,
        "anti_spoofing_pass": all_anti_spoofing_ok,
        "thresholds": {s["id"]: s["threshold"] for s in criterion_stats},
    }

    return record, True


def verify_calibration(criteria_path: str, record: dict | None) -> tuple[bool, str]:
    """校验 criteria SHA 与标定记录一致。"""
    if record is None:
        return False, "无标定记录"
    criteria_sha = sha256_file(criteria_path)
    if record.get("criteria_sha") != criteria_sha:
        return False, (f"criteria SHA 不一致: 当前={criteria_sha} "
                       f"标定={record.get('criteria_sha')}（需重新标定）")
    return True, f"criteria SHA 一致: {criteria_sha}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(prog="calibrate.py", description="criteria 变更重新标定 + SHA 强一致校验（W5-C1 / AC-8）")
    ap.add_argument("--criteria", required=True, help="criteria YAML 路径")
    ap.add_argument("--golden", default=DEFAULT_GOLDEN_DIR, help="golden set 目录")
    ap.add_argument("--record-out", default=DEFAULT_RECORD_PATH, help="标定记录输出路径")
    ap.add_argument("--force", action="store_true", help="强制重新标定（忽略既有记录）")
    ap.add_argument("--verify", action="store_true", help="仅校验标定一致性（不标定）")
    a = ap.parse_args()

    if a.verify:
        record = None
        if os.path.isfile(a.record_out):
            record = load_json(a.record_out)
        ok, reason = verify_calibration(a.criteria, record)
        print(f"标定校验: {reason}")
        return 0 if ok else 1

    record, is_fresh = calibrate(a.criteria, a.golden, force=a.force)

    if not is_fresh:
        print(f"== 标定幂等跳过（criteria 未变更）==")
        print(f"criteria SHA: {record.get('criteria_sha')}")
        print(f"上次标定: {record.get('calibrated_at')}")
        print(f"反特判断言: {'通过' if record.get('anti_spoofing_pass') else '失败'}")
        return 0

    print(f"== 重新标定（criteria 变更/首次）==")
    print(f"criteria SHA: {record['criteria_sha']}")
    print(f"criteria card: {record['criteria_card']} ({record['criteria_card_ref']})")
    print(f"golden set: {record['golden_dir']} (version={record['golden_version']})")
    print(f"标定时间: {record['calibrated_at']}")
    print(f"criterion 统计:")
    for s in record["criterion_stats"]:
        print(f"  [{s['id']}] threshold={s['threshold']} mean={s['mean']} "
              f"fail_max={s['fail_max_score']} anti_spoofing={'OK' if s['anti_spoofing_ok'] else 'FAIL'}")
    print(f"反特判断言: {'通过' if record['anti_spoofing_pass'] else '失败'}")

    # 落盘
    with open(a.record_out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    print(f"标定记录已写入: {a.record_out}")

    if not record["anti_spoofing_pass"]:
        err("反特判断言失败：不合格样本未被正确识别（criteria 阈值需调整）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
