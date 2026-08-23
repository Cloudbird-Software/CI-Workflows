#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""golden_set.py —— golden set 加载 / 盲化注入 / 全量重放 / 回归断言（W5-C1 .github#286 / AC-8）

golden set 是对 gate 逻辑的离线回归测试集：含已知不合格样本（构造独立于判定脚本），
盲化注入后与正常样本同批混排，每次 run 全量重放并做回归断言。

样本形态（与 llm_verifier.py / verify-evidence.py 的报告 schema 同构）：
  - 每条样本 = 一份合成报告 JSON，含 criteria_scores + token_account；
  - expected_verdict 由样本作者独立标注（不引用判定脚本代码）；
  - 盲化时剥除 id / source / type / expected_verdict，仅留 gate 入参；
  - 回放时由纯函数 compute_verdict 重新判定，与预期比对。

回放模式：
  native（默认，确定性、免 LLM）：直接用样本内嵌的聚合分计算 verdict，
    作为 gate 逻辑的常驻回归断言（CI required check，逐 run）。
  llm（可选）：以样本对应的 criteria YAML + 预录响应走 llm_verifier.py 子进程，
    用于验证 golden 样本在真实评分路径下仍不合格（非默认，需凭据）。

用法:
  python3 golden_set.py <command> [options]
    load      列示样本
    blind     盲化混排输出到 --out（剥离元数据，仅留 gate 入参）
    replay    全量重放（native/llm）
    regress   回放 + 回归断言（不合格样本仍不合格；不合格变合格 = gate 失效 = 红）

退出码：0=通过 | 1=回归失败（gate 失效） | 2=配置/环境/样本格式错误（fail-closed）
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SAMPLES_DIR = os.path.join(HERE, "fixtures", "golden", "samples")
LLM_VERIFIER_PY = os.path.join(HERE, "llm_verifier.py")

# gate 判定阈值：与 llm_verifier.py / calibrate.py 共用常量
GATE_PASS = "survived"
GATE_FAIL = "insufficient"
GATE_VOID = "void"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def die(code: int, msg: str) -> None:
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)
    sys.exit(code)


def load_samples(samples_dir: str) -> list[dict]:
    """加载目录下全部 golden 样本 JSON。"""
    d = Path(samples_dir)
    if not d.is_dir():
        die(2, f"样本目录不存在: {samples_dir}")
    samples = []
    for p in sorted(d.glob("*.json")):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            die(2, f"样本 JSON 非法 {p.name}: {e}")
        if not isinstance(obj, dict):
            die(2, f"样本根不是 mapping: {p.name}")
        obj.setdefault("_file", p.name)
        samples.append(obj)
    if not samples:
        die(2, f"样本目录为空: {samples_dir} —— golden set 不可空跑（fail-closed）")
    return samples


def compute_verdict(scores: list[dict], token_account_ok: bool, threshold_global: float = 0.7) -> str:
    """纯函数：由聚合分 + token 账判定 verdict（与 llm_verifier.build_report 同语义）。

    - token 账异常 → void（作废，阻断）；
    - 任一 criterion 聚合分 < 阈值 → insufficient；
    - 全部达标 → survived。
    """
    if not token_account_ok:
        return GATE_VOID
    for s in scores:
        thr = float(s.get("threshold", threshold_global))
        agg = float(s.get("aggregated", 0.0))
        if agg < thr - 1e-9:  # 浮点容差
            return GATE_FAIL
    return GATE_PASS


def compute_blocking(verdict: str) -> bool:
    return verdict != GATE_PASS


def blind_sample(sample: dict) -> dict:
    """盲化单条样本：剥除来源元数据，仅保留 gate 判定入参。

    剥除字段: id, source, type, expected_verdict, _file。
    保留: criteria_scores, token_account_ok, threshold_global 等纯输入。
    返回的样本无法被位置或 id 识别为「已知不合格」。
    """
    stripped = {k: v for k, v in sample.items()
                if k not in ("id", "source", "type", "expected_verdict", "_file")}
    return stripped


def blind_batch(samples: dict) -> dict:
    """盲化 + 混排整批样本，返回 blinded 集合与 shuffle 种子（可复现审计）。"""
    seed = int.from_bytes(hashlib.sha256(
        (os.environ.get("GITHUB_RUN_ID", now_iso())).encode()).digest()[:8], "big")
    blinded = [blind_sample(s) for s in samples["samples"]]
    rng = random.Random(seed)
    rng.shuffle(blinded)
    return {
        "schema": "golden-blind-batch/v1",
        "blinded_at": now_iso(),
        "seed_src": "sha256(run_id)[:8]" if os.environ.get("GITHUB_RUN_ID") else "timestamp",
        "count": len(blinded),
        "samples": blinded,
    }


def replay_native(samples: dict, run_id: str) -> dict:
    """native 模式回放：纯函数计算 verdict（免 LLM）。"""
    results = []
    for s in samples["samples"]:
        scores = s.get("criteria_scores") or []
        token_ok = bool(s.get("token_account_ok", True))
        threshold = float(s.get("threshold_global", 0.7))
        verdict = compute_verdict(scores, token_ok, threshold)
        blocking = compute_blocking(verdict)
        results.append({
            "id": s.get("id", "?"),
            "source": s.get("source", "?"),
            "expected_verdict": s.get("expected_verdict"),
            "actual_verdict": verdict,
            "actual_blocking": blocking,
            "criteria_count": len(scores),
            "token_account_ok": token_ok,
        })
    return {
        "schema": "golden-replay-native/v1",
        "run_id": run_id,
        "mode": "native",
        "ts": now_iso(),
        "count": len(results),
        "results": results,
    }


def replay_llm(samples: dict, run_id: str, base_url: str, api_key: str, model: str,
               ledger_dir: str) -> dict:
    """llm 模式回放：走 llm_verifier.py 子进程（需凭据 + 预录响应）。

    样本需含 criteria_file / issue_body / replay_file 字段；缺失该字段的样本跳过。
    """
    results = []
    for s in samples["samples"]:
        criteria = s.get("criteria_file")
        issue = s.get("issue_body", "")
        replay = s.get("replay_file", "")
        if not criteria or not os.path.isfile(criteria):
            results.append({"id": s.get("id", "?"), "skipped": True,
                            "reason": "无 criteria_file（native 样本跳过）"})
            continue
        cmd = [sys.executable, LLM_VERIFIER_PY, "verify",
               "--criteria", criteria,
               "--card-id", s.get("id", "golden"),
               "--run-id", f"{run_id}-{s.get('id', 'x')}",
               "--repo-dir", os.path.dirname(HERE),
               "--ledger-dir", ledger_dir,
               "--base-url", base_url, "--api-key", api_key, "--model", model,
               "--report-out", os.path.join(ledger_dir, f"golden-{s.get('id','x')}.json")]
        if issue:
            cmd += ["--issue-body", issue]
        if replay and os.path.isfile(replay):
            cmd += ["--replay-file", replay]
        rc = subprocess.call(cmd)
        results.append({"id": s.get("id", "?"), "rc": rc,
                        "expected_verdict": s.get("expected_verdict")})
    return {
        "schema": "golden-replay-llm/v1",
        "run_id": run_id,
        "mode": "llm",
        "ts": now_iso(),
        "count": len(results),
        "results": results,
    }


def regress(replay: dict) -> dict:
    """回归断言：已知不合格样本仍须不合格。

    - expected=insufficient/void 的样本 actual 仍为 insufficient/void → 通过；
    - expected=insufficient/void 的样本 actual 变 survived → 回归失败（gate 被削弱）；
    - expected=survived 的样本（如有）actual 仍为 survived → 通过。
    返回 summary；调用方按 failed>0 退出 1。
    """
    failed = []
    passed = []
    for r in replay["results"]:
        if r.get("skipped"):
            continue
        exp = r.get("expected_verdict")
        act = r.get("actual_verdict")
        bad = exp in (GATE_FAIL, GATE_VOID)
        if bad and act == GATE_PASS:
            failed.append({"id": r.get("id"), "expected": exp, "actual": act})
        else:
            passed.append(r.get("id"))
    return {
        "schema": "golden-regress/v1",
        "ts": now_iso(),
        "run_id": replay.get("run_id"),
        "mode": replay.get("mode"),
        "total": len(replay["results"]),
        "passed": len(passed),
        "failed": len(failed),
        "failed_details": failed,
        "regress_ok": len(failed) == 0,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="golden_set.py", description="golden set 加载/盲化/回放/回归")
    ap.add_argument("cmd", choices=["load", "blind", "replay", "regress"])
    ap.add_argument("--samples-dir", default=DEFAULT_SAMPLES_DIR, help="样本目录")
    ap.add_argument("--out", default=None, help="blind 输出路径")
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", now_iso()))
    ap.add_argument("--mode", choices=["native", "llm"], default="native", help="回放模式")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "glm-4.6"))
    ap.add_argument("--ledger-dir", default=os.environ.get("GATE_METERING_DIR", ".metering-golden"))
    ap.add_argument("--report-out", default=None, help="blind/replay/regress 结果落盘")
    args = ap.parse_args(argv)

    if args.cmd == "load":
        samples = load_samples(args.samples_dir)
        print(f"golden set: {len(samples)} 样本 @ {args.samples_dir}")
        for s in samples:
            print(f"  - {s.get('id'):20s} expected={s.get('expected_verdict'):12s} "
                  f"scores={len(s.get('criteria_scores', []))} src={s.get('source', '?')}")
        return 0

    # 其余命令都需要先加载
    samples_list = load_samples(args.samples_dir)
    batch = {
        "schema": "golden-batch/v1",
        "loaded_at": now_iso(),
        "count": len(samples_list),
        "samples": samples_list,
    }

    if args.cmd == "blind":
        blinded = blind_batch(batch)
        out_path = args.out or os.path.join(args.samples_dir, "..", "golden-blind.json")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(blinded, ensure_ascii=False, indent=2),
                                  encoding="utf-8", newline="\n")
        print(f"盲化混排 -> {out_path}（{blinded['count']} 条，seed_src={blinded['seed_src']}）")
        return 0

    if args.cmd in ("replay", "regress"):
        if args.mode == "llm":
            if not args.api_key:
                die(2, "llm 模式需要 --api-key / LLM_API_KEY（免凭据请用 native 模式）")
            replay = replay_llm(batch, args.run_id, args.base_url, args.api_key,
                                args.model, args.ledger_dir)
        else:
            replay = replay_native(batch, args.run_id)

        if args.report_out:
            Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report_out).write_text(json.dumps(replay, ensure_ascii=False, indent=2),
                                             encoding="utf-8", newline="\n")

        if args.cmd == "replay":
            print(json.dumps(replay, ensure_ascii=False, indent=2))
            return 0

        # regress
        summary = regress(replay)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if not summary["regress_ok"]:
            ids = ", ".join(f["id"] for f in summary["failed_details"])
            die(1, f"回归失败：{len(summary['failed'])} 条已知不合格样本变合格（gate 失效）: {ids}")
        print(f"回归通过：{summary['passed']}/{summary['total']} 条断言成立")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
