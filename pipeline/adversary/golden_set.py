#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""golden_set.py —— verifier golden set 加载/盲化注入/全量重放/回归断言（W5-C1 .github#286，AC-8 / AC-2）

golden set = 一组已知 verdict 的 verifier 报告样本（含已知不合格样本），用于：
  - 证明阈值 gate 不是特判（已知低分样本确实触发失败）；
  - 盲化注入（剥 id/来源元数据，与正常样本同批混排）后对未见过的随机捏造引用泛化作废；
  - 每次 run 全量重放 + 回归断言（不合格样本仍不合格）；
  - criteria 文件每次变更必须重新标定（SHA 强一致），标定记录留档。

与 W3-C3 llm_verifier.py、W3-C5 evidence_check.py 接口兼容：
  - 样本以 llm-verifier-report/v1 或 verified-report/v1 形态内嵌（或引用外部报告文件）；
  - verdict 判定复用同一阈值比较语义（aggregated >= threshold → survived，否则 insufficient）；
  - 盲化注入后的混排集供下游 evidence_check --self-test 或 CI 常驻 required check 消费。

不合格样本构造方法独立于判定脚本代码：
  - 来源：手工构造的低分 criteria_scores / 含已知捏造引用的 citations；
  - 这些样本 fixtures 是纯数据（JSON），不包含任何判定逻辑——
    golden_set.py 的判定逻辑（阈值比较）与样本数据分离，符合 AC-8 "独立于判定脚本"。

用法:
  python3 pipeline/adversary/golden_set.py replay --golden fixtures/golden/golden-set.json
  python3 pipeline/adversary/golden_set.py blind  --golden fixtures/golden/golden-set.json --out /tmp/blinded.json
  python3 pipeline/adversary/golden_set.py verify --golden fixtures/golden/golden-set.json --criteria criteria/X.yaml

退出码：0=全部回归断言通过 | 1=至少一个样本 verdict 偏离预期 | 2=配置/加载/infra 错误
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import random
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # noqa: BLE001
    print("FATAL: 需要 PyYAML（pip install pyyaml==6.0.3）", file=sys.stderr)
    sys.exit(2)

DEFAULT_GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "golden", "golden-set.json")
CALIBRATION_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "golden", "calibration")


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


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 判定语义（与 llm_verifier.py build_report 同源：aggregated >= threshold → survived）
# ---------------------------------------------------------------------------

def verdict_from_report(report: dict) -> str:
    """从 verifier/evidence 报告推导 verdict。

    支持两种形态：
      - llm-verifier-report/v1：读 criteria_scores[].aggregated 与 threshold 比较；
      - verified-report/v1：直接读 verdict 字段（已由 evidence_check 强制转 insufficient）。
    """
    schema = report.get("schema", "")
    if schema == "verified-report/v1":
        return report.get("verdict", "insufficient")
    # llm-verifier-report/v1 或兼容形态
    scores = report.get("criteria_scores") or []
    if not scores:
        return report.get("verdict", "insufficient")
    threshold_global = report.get("threshold_global", 0.7)
    for s in scores:
        thr = s.get("threshold", threshold_global)
        if float(s.get("aggregated", 0)) < float(thr):
            return "insufficient"
    # token 账作废 → void（同 llm_verifier.py build_report 语义）
    ta = report.get("token_account") or {}
    if ta and not ta.get("ok", True):
        return "void"
    return "survived"


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def load_golden(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        die(2, f"golden set 不可读或 JSON 非法 {path}: {e}")
    if not isinstance(data, dict):
        die(2, f"golden set 根不是 mapping: {path}")
    if "samples" not in data:
        die(2, f"golden set 缺少 samples 数组: {path}")
    return data


def resolve_report(sample: dict, suite_dir: str) -> dict:
    """解析单条样本的内联报告或外部报告引用。"""
    if "report_inline" in sample:
        return copy.deepcopy(sample["report_inline"])
    rel = sample.get("report")
    if not rel:
        die(2, f"样本 {sample.get('id', '?')} 既无 report_inline 也无 report 引用")
    report_path = os.path.join(suite_dir, rel)
    try:
        with open(report_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        die(2, f"样本 {sample.get('id')} 外部报告不可读 {report_path}: {e}")


# ---------------------------------------------------------------------------
# 盲化注入（AC-8：剥 id/来源元数据，同批混排）
# ---------------------------------------------------------------------------

def blind_sample(sample: dict, report: dict) -> tuple[dict, dict]:
    """剥离样本的来源元数据，返回 (blinded_sample, blinded_report)。

    被剥离/替换的字段：
      - sample.id → 替换为 hash（不可逆，仅用于去重/计数）；
      - sample.tags → 清空（防止判定脚本据此走特判分支）；
      - sample.source / sample.origin → 删除；
      - report 中可能暴露来源的字段（card_id、issue 等）→ 泛化为占位。
    """
    blinded_sample = copy.deepcopy(sample)
    orig_id = blinded_sample.get("id", "")
    blinded_sample["id"] = "blinded-" + sha256_text(orig_id + now_iso())[:16]
    for k in ("tags", "source", "origin", "constructed_by", "erratum_ref"):
        blinded_sample.pop(k, None)

    blinded_report = copy.deepcopy(report)
    for k in ("card_id", "issue", "run_id"):
        if k in blinded_report:
            blinded_report[k] = "<redacted>"
    return blinded_sample, blinded_report


def build_blinded_set(golden: dict, seed: int = 42) -> list[dict]:
    """构建盲化后的混排集（已知不合格 + 已知合格 混排）。"""
    suite_dir = os.path.dirname(os.path.abspath(golden.get("__path", DEFAULT_GOLDEN)))
    blinded = []
    for s in golden.get("samples", []):
        report = resolve_report(s, suite_dir)
        bs, br = blind_sample(s, report)
        bs["report_inline"] = br
        bs.pop("report", None)
        blinded.append(bs)
    rng = random.Random(seed)
    rng.shuffle(blinded)
    return blinded


# ---------------------------------------------------------------------------
# 全量重放 + 回归断言
# ---------------------------------------------------------------------------

def replay(golden: dict, criteria_path: str | None = None) -> dict:
    """全量重放 golden set，对每条样本推导 verdict 并与 expected 比较。"""
    suite_dir = os.path.dirname(os.path.abspath(golden.get("__path", DEFAULT_GOLDEN)))

    # criteria SHA（用于标定一致性校验）
    criteria_sha = ""
    if criteria_path and os.path.isfile(criteria_path):
        criteria_sha = sha256_file(criteria_path)

    results = []
    failures = 0
    for s in golden.get("samples", []):
        sid = s.get("id", "(无 id)")
        report = resolve_report(s, suite_dir)
        actual = verdict_from_report(report)
        expected = s.get("expected_verdict", "insufficient")
        ok = actual == expected
        if not ok:
            failures += 1
        results.append({
            "id": sid,
            "expected": expected,
            "actual": actual,
            "ok": ok,
            "tags": s.get("tags", []),
        })

    return {
        "schema": "golden-replay-result/v1",
        "ts": now_iso(),
        "criteria_sha256": criteria_sha,
        "criteria_path": os.path.abspath(criteria_path) if criteria_path else None,
        "total": len(results),
        "passed": len(results) - failures,
        "failed": failures,
        "samples": results,
    }


def print_replay_summary(res: dict) -> None:
    print(f"== golden replay: total={res['total']} passed={res['passed']} failed={res['failed']} ==")
    if res.get("criteria_sha256"):
        print(f"criteria_sha256: {res['criteria_sha256']}")
    for s in res["samples"]:
        mark = "✓" if s["ok"] else "✗"
        print(f"  {mark} [{s['id']}] expected={s['expected']} actual={s['actual']} tags={s['tags']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="golden_set.py", description="verifier golden set 加载/盲化/重放")
    ap.add_argument("cmd", choices=["replay", "blind", "verify"])
    ap.add_argument("--golden", default=DEFAULT_GOLDEN, help="golden set JSON 路径")
    ap.add_argument("--criteria", default="", help="criteria YAML 路径（用于标定校验）")
    ap.add_argument("--out", default="", help="blind 命令的输出路径")
    ap.add_argument("--seed", type=int, default=42, help="盲化混排随机种子（默认 42）")
    args = ap.parse_args(argv)

    golden = load_golden(args.golden)
    golden["__path"] = args.golden

    if args.cmd == "blind":
        blinded = build_blinded_set(golden, seed=args.seed)
        out_path = args.out or os.path.join(
            os.path.dirname(args.golden), "blinded-set.json")
        payload = {
            "schema": "blinded-golden-set/v1",
            "generated_at": now_iso(),
            "seed": args.seed,
            "source_golden": os.path.abspath(args.golden),
            "samples": blinded,
        }
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"盲化混排集已写入 {out_path}（{len(blinded)} 条）")
        return 0

    # replay / verify
    res = replay(golden, args.criteria)
    print_replay_summary(res)

    if args.cmd == "verify" and args.criteria:
        # verify 模式：对标定记录做 SHA 强一致校验
        calib_dir = Path(CALIBRATION_DIR)
        records = sorted(calib_dir.glob("*.json")) if calib_dir.is_dir() else []
        if records:
            last = json.loads(records[-1].read_text(encoding="utf-8"))
            recorded_sha = last.get("criteria_sha256", "")
            if recorded_sha and recorded_sha != res["criteria_sha256"]:
                err(f"criteria SHA 变更未重新标定（标定记录 {recorded_sha[:11]} vs 当前 {res['criteria_sha256'][:11]}）——AC-2 fail-closed")
                return 2
            print(f"标定 SHA 一致（{res['criteria_sha256'][:11]}）")
        else:
            err("无标定记录 — criteria 变更后须先运行 calibrate 重新标定（AC-8）")
            return 2

    return 1 if res["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
