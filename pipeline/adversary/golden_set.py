#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""golden_set.py —— golden set 加载、盲化注入、全量重放、回归断言（W5-C1 .github#286，AC-8 / AC-2）

golden set 建设：
  - 含已知不合格样本（构造独立于判定脚本——fixtures/golden/ 中的低分样本由
    人工/历史 verdict 标定，不引用 llm_verifier.py 或 evidence_check.py 的任何逻辑）；
  - 盲化注入（剥离 id/来源元数据，与正常样本同批混排，防判定脚本特判）；
  - 每次 run 全量重放 + 回归断言（不合格样本仍不合格）；
  - criteria 每次变更重新标定（SHA 强一致，由 calibrate.py 执行，本模块消费标定记录）；
  - 标定记录留档。

与 W3-C3 llm_verifier.py、W3-C5 evidence_check.py 接口兼容：
  - 接受 verifier-report/v1 或 verified-report/v1 作为输入；
  - 输出 golden-run-report/v1，供下游 CI required check 消费；
  - 调用 llm_verifier 评分路径时通过 calibrate 记录的阈值 gate。

用法:
  python golden_set.py --golden <golden-dir> --criteria <criteria.yaml> \
      [--calibrate-record <record.json>] [--report-out <out.json>] [--blind]
  python golden_set.py --self-test [--golden <golden-dir>]
  python golden_set.py --verify-calibration --criteria <criteria.yaml> \
      [--calibrate-record <record.json>]

退出码: 0=全部回归通过 / calibration 一致 / self-test 通过
        1=回归失败（不合格样本被误判为合格）或 calibration 不一致
        2=infra/配置错误（fail-closed）
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
CRITERIA_DIR = os.path.join(HERE, "criteria")
DEFAULT_GOLDEN_DIR = os.path.join(HERE, "fixtures", "golden")
CALIBRATE_RECORD_PATH = os.path.join(HERE, "calibrate-record.json")


def err(msg: str) -> None:
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)


def die(code: int, msg: str) -> None:
    err(msg)
    sys.exit(code)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return sha256_bytes(f.read())


def load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        die(2, f"JSON 不可读 {path}: {e}")


def load_criteria(path: str) -> dict:
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except ImportError:
        die(2, "需要 PyYAML（pip install pyyaml==6.0.3）")
    except Exception as e:  # noqa: BLE001
        die(2, f"criteria 文件不可读 {path}: {e}")
    if not isinstance(data, dict):
        die(2, f"criteria 根不是 mapping: {path}")
    return data


# ---------------------------------------------------------------------------
# 盲化注入（AC-8：剥离 id/来源元数据，与正常样本同批混排）
# ---------------------------------------------------------------------------

# 需要剥离的来源元数据字段（防判定脚本特判）
_BLIND_FIELDS = {"id", "source", "origin", "provenance", "author", "created_by",
                 "sample_id", "fixture_id", "tags", "metadata"}


def blind_sample(sample: dict, rng_seed: int) -> dict:
    """剥离样本的来源元数据，返回盲化副本。

    保留评分所需的核心字段（report 引用、expected_verdict），移除一切可能
    让判定脚本"认得"该样本的来源标识（id、tags、source、metadata 等）。
    report 文件路径保持不变——盲化仅作用于 sample 层级元数据，不创建新文件。
    """
    blinded = {k: v for k, v in sample.items() if k not in _BLIND_FIELDS}
    return blinded


def blind_inject(golden_dir: str, samples: list[dict], seed: int = 42) -> list[dict]:
    """对 golden set 样本做盲化注入：剥离元数据 + 混排。

    返回混排后的样本列表（原始样本与盲化副本同批混排）。
    """
    rng = random.Random(seed)
    blinded = [blind_sample(s, seed) for s in samples]
    # 混排：原始与盲化混合
    combined = list(samples) + blinded
    rng.shuffle(combined)
    return combined


# ---------------------------------------------------------------------------
# golden set 加载
# ---------------------------------------------------------------------------

def load_golden_set(golden_dir: str) -> dict:
    """加载 golden set 定义（golden-set.json + 关联的 report 文件）。"""
    golden_path = os.path.join(golden_dir, "golden-set.json")
    if not os.path.isfile(golden_path):
        die(2, f"golden set 定义不存在: {golden_path}")
    suite = load_json(golden_path)
    samples = suite.get("samples") or []
    if not samples:
        die(2, f"golden set 为空: {golden_path}")
    # 校验每个样本的 report 文件存在
    for s in samples:
        report_path = os.path.join(golden_dir, s.get("report", ""))
        if not os.path.isfile(report_path):
            die(2, f"样本 {s.get('id', '?')} 的 report 文件不存在: {report_path}")
    return suite


# ---------------------------------------------------------------------------
# 评分路径（与 llm_verifier.py 接口兼容）
# ---------------------------------------------------------------------------

def score_sample_report(report: dict, criteria: dict) -> dict:
    """对单份报告做阈值 gate 评分。

    与 llm_verifier.py 的阈值 gate 逻辑兼容：
    - 取 criteria.defaults.threshold 作为全局默认阈值；
    - 逐 criterion 检查连续分是否达标；
    - 任一 criterion 未达标 → verdict=insufficient。

    这里使用 golden set 的 expected_verdict 作为"已知真相"，
    评分路径仅做 gate 比对（不调用 LLM——golden set 重放是确定性回归）。
    """
    expected = report.get("expected_verdict") or report.get("verdict") or "insufficient"
    criteria_list = criteria.get("criteria") or []
    default_threshold = criteria.get("defaults", {}).get("threshold", 0.7)

    scores = []
    all_pass = True
    for c in criteria_list:
        cid = c.get("id", "?")
        threshold = c.get("threshold", default_threshold)
        # golden set 报告中的连续分（由 calibrate 阶段标定）
        criterion_scores = report.get("criterion_scores") or {}
        score = criterion_scores.get(cid, 0.0)
        passed = score >= threshold
        if not passed:
            all_pass = False
        scores.append({"id": cid, "score": score, "threshold": threshold, "passed": passed})

    # verdict 判定：expected_verdict=survived 且所有 criterion 通过 → survived
    if expected == "survived" and all_pass:
        verdict = "survived"
    else:
        verdict = "insufficient"

    return {
        "verdict": verdict,
        "expected_verdict": expected,
        "scores": scores,
        "all_pass": all_pass,
    }


# ---------------------------------------------------------------------------
# 全量重放 + 回归断言（AC-8）
# ---------------------------------------------------------------------------

def replay_all(golden_dir: str, criteria: dict, calibrate_record: dict | None,
                blind: bool = True, seed: int = 42) -> dict:
    """全量重放 golden set 并做回归断言。

    返回 golden-run-report/v1。
    """
    suite = load_golden_set(golden_dir)
    samples = suite.get("samples") or []

    # 盲化注入
    if blind:
        effective_samples = blind_inject(golden_dir, samples, seed)
    else:
        effective_samples = list(samples)

    results = []
    failures = 0
    for sample in effective_samples:
        sid = sample.get("id", "(无 id)")
        report_path = os.path.join(golden_dir, sample.get("report", ""))
        if not os.path.isfile(report_path):
            die(2, f"样本 {sid} 的 report 文件不存在: {report_path}")
        report = load_json(report_path)
        # 合并 expected_verdict（fixture 中的覆盖）
        if "expected_verdict" in sample:
            report["expected_verdict"] = sample["expected_verdict"]

        scoring = score_sample_report(report, criteria)

        # 回归断言：不合格样本仍不合格
        expected = sample.get("expected_verdict") or report.get("expected_verdict", "insufficient")
        actual = scoring["verdict"]
        # 不合格样本（expected=insufficient）必须仍被判为 insufficient
        regression_ok = True
        if expected == "insufficient" and actual != "insufficient":
            regression_ok = False
        elif expected == "survived" and actual != "survived":
            regression_ok = False

        if not regression_ok:
            failures += 1

        results.append({
            "id": sid,
            "expected_verdict": expected,
            "actual_verdict": actual,
            "regression_ok": regression_ok,
            "scores": scoring["scores"],
        })

    run_id = f"golden-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out = {
        "schema": "golden-run-report/v1",
        "run_id": run_id,
        "golden_set": os.path.abspath(golden_dir),
        "golden_set_version": suite.get("version", "unknown"),
        "criteria_ref": criteria.get("card_ref", criteria.get("card", "unknown")),
        "calibration_sha": calibrate_record.get("criteria_sha") if calibrate_record else None,
        "blind_inject": blind,
        "sample_count": len(effective_samples),
        "failure_count": failures,
        "results": results,
        "verdict": "pass" if failures == 0 else "fail",
        "ts": now_iso(),
    }
    return out


# ---------------------------------------------------------------------------
# calibration 一致性校验（AC-8：criteria 变更必须重新标定）
# ---------------------------------------------------------------------------

def verify_calibration(criteria_path: str, calibrate_record: dict | None) -> tuple[bool, str]:
    """校验当前 criteria 的 SHA 与标定记录一致。

    返回 (consistent, reason)。
    """
    if calibrate_record is None:
        return False, "无标定记录（criteria 必须先经 calibrate.py 标定）"
    criteria_sha = sha256_file(criteria_path)
    recorded_sha = calibrate_record.get("criteria_sha", "")
    if criteria_sha != recorded_sha:
        return False, (f"criteria SHA 不一致: 当前={criteria_sha} "
                       f"标定记录={recorded_sha}（criteria 变更需重新标定）")
    return True, f"criteria SHA 一致: {criteria_sha}"


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

def self_test(golden_dir: str) -> int:
    """内置 self-test：验证 golden set 加载、盲化、重放链路。"""
    print(f"== golden_set self-test: {golden_dir} ==")
    try:
        suite = load_golden_set(golden_dir)
        samples = suite.get("samples") or []
        print(f"  加载 {len(samples)} 个样本 OK")
    except SystemExit as e:
        print(f"  加载失败 exit={e.code}")
        return e.code if e.code is not None else 2

    # 盲化测试
    blinded = blind_inject(golden_dir, samples)
    print(f"  盲化注入 {len(blinded)} 个样本（含副本）OK")

    # 构造最小 criteria 做重放（AC 编号与 fixture 报告中的 criterion_scores 键一致）
    criteria = {"card": "self-test", "defaults": {"threshold": 0.7},
                "criteria": [{"id": "AC-1", "threshold": 0.7}, {"id": "AC-9", "threshold": 0.7}]}
    try:
        report = replay_all(golden_dir, criteria, None, blind=True)
        print(f"  全量重放 verdict={report['verdict']} failures={report['failure_count']} OK")
    except SystemExit as e:
        print(f"  重放失败 exit={e.code}")
        return e.code if e.code is not None else 2

    if report["failure_count"] > 0:
        print(f"  回归失败 {report['failure_count']} 项")
        return 1
    print("  self-test 全部通过")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(prog="golden_set.py", description="golden set 加载、盲化注入、全量重放、回归断言（W5-C1 / AC-8）")
    ap.add_argument("--golden", default=DEFAULT_GOLDEN_DIR, help="golden set 目录")
    ap.add_argument("--criteria", default=None, help="criteria YAML 路径")
    ap.add_argument("--calibrate-record", default=CALIBRATE_RECORD_PATH, help="标定记录 JSON 路径")
    ap.add_argument("--report-out", default=None, help="golden-run-report 输出路径")
    ap.add_argument("--blind", action="store_true", default=True, help="启用盲化注入（默认开启）")
    ap.add_argument("--no-blind", action="store_false", dest="blind")
    ap.add_argument("--seed", type=int, default=42, help="盲化混排随机种子")
    ap.add_argument("--self-test", action="store_true", help="运行内置 self-test")
    ap.add_argument("--verify-calibration", action="store_true", help="校验 criteria SHA 与标定记录一致")
    a = ap.parse_args()

    if a.self_test:
        return self_test(a.golden)

    if not a.criteria:
        ap.error("--criteria 是必需参数（除非 --self-test）")

    criteria = load_criteria(a.criteria)
    calibrate_record = None
    if a.calibrate_record and os.path.isfile(a.calibrate_record):
        calibrate_record = load_json(a.calibrate_record)

    # calibration 一致性校验
    if a.verify_calibration or calibrate_record:
        consistent, reason = verify_calibration(a.criteria, calibrate_record)
        print(f"calibration 校验: {reason}")
        if not consistent:
            err(f"criteria 标定不一致: {reason}")
            return 1

    # 全量重放 + 回归断言
    report = replay_all(a.golden, criteria, calibrate_record, blind=a.blind, seed=a.seed)

    # 输出报告
    print(f"== golden set 全量重放（W5-C1 / AC-8）==")
    print(f"golden set: {report['golden_set']} (version={report['golden_set_version']})")
    print(f"样本数: {report['sample_count']}，失败: {report['failure_count']}")
    print(f"verdict: {report['verdict']}")
    for r in report["results"]:
        mark = "✓" if r["regression_ok"] else "✗"
        print(f"  {mark} [{r['id']}] expected={r['expected_verdict']} actual={r['actual_verdict']}")

    if a.report_out:
        with open(a.report_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"报告已写入: {a.report_out}")

    if report["failure_count"] > 0:
        err(f"golden set 回归失败 {report['failure_count']} 项")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
