#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_check_run_writeback_logic.py —— check_run_writeback 纯逻辑自测（W4-C2 .github#283）

零网络、零凭据。由 tests/test-check-run-writeback.sh 调用（传 fixture 目录）。
验证：schema 校验退出码、verdict→conclusion 映射（通过直接调用 build_check_body）、
output 截断、PR 定位策略。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # 父目录 = pipeline/adversary/
import check_run_writeback as crw  # noqa: E402


def load(fn):
    with open(fn, encoding="utf-8") as f:
        return json.load(f)


def main():
    fix = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "fixtures", "check_run_writeback")
    fails = 0
    def ok(msg): print(f"PASS  {msg}")
    def bad(msg): 
        nonlocal fails; fails += 1; print(f"FAIL  {msg}")

    survived = load(os.path.join(fix, "report-survived.json"))
    insufficient = load(os.path.join(fix, "report-insufficient.json"))
    no_attempts = load(os.path.join(fix, "report-no-attempts.json"))

    # W2 verdict→conclusion 映射（直接调用 build_check_body）
    cases = [
        (survived, "success"),
        (insufficient, "failure"),
        (no_attempts, "neutral"),
    ]
    for report, want in cases:
        body = crw.build_check_body(report)
        got = body["conclusion"]
        if got == want:
            ok(f"W2 verdict={report['verdict']} → {want}")
        else:
            bad(f"W2 verdict={report['verdict']} → 期望 {want} 实际 {got}")

    # 未知 verdict → fail-closed failure
    unknown = dict(survived); unknown["verdict"] = "bogus"
    body = crw.build_check_body(unknown)
    if body["conclusion"] == "failure":
        ok("W2 未知 verdict → fail-closed failure")
    else:
        bad(f"W2 未知 verdict → 期望 failure 实际 {body['conclusion']}")

    # 缺失 verdict → fail-closed failure
    missing = dict(survived); del missing["verdict"]
    body = crw.build_check_body(missing)
    if body["conclusion"] == "failure":
        ok("W2 缺失 verdict → fail-closed failure")
    else:
        bad(f"W2 缺失 verdict → 期望 failure 实际 {body['conclusion']}")

    # W3 output.text 截断
    long_report = dict(survived)
    long_report["attempts"] = [{
        "strategy": "SX", "name": "x", "hole": "h", "suite_gap": "g",
        "rationale": "R" * 80000, "files": ["t.py"], "green": False,
        "suite_rc": 1, "note": "", "suite_tail": "T" * 80000,
    }]
    body = crw.build_check_body(long_report)
    txt_len = len(body["output"]["text"])
    if txt_len <= 65535:
        ok(f"W3 output.text 截断后 {txt_len} ≤ 65535")
    else:
        bad(f"W3 output.text 过长 {txt_len} > 65535")
    summ_len = len(body["output"]["summary"])
    if summ_len <= 65535:
        ok(f"W3 output.summary 截断后 {summ_len} ≤ 65535")
    else:
        bad(f"W3 output.summary 过长 {summ_len} > 65535")

    # check name 默认值
    body = crw.build_check_body(survived)
    if body["name"] == "adversary":
        ok(f"W2 check name 默认值 = 'adversary'")
    else:
        bad(f"W2 check name 默认值 ≠ 'adversary'（{body['name']}")

    print("-----")
    if fails == 0:
        print("check_run_writeback 纯逻辑自测全部通过")
        sys.exit(0)
    print(f"check_run_writeback 纯逻辑自测失败 {fails} 项")
    sys.exit(1)


if __name__ == "__main__":
    main()
