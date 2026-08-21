#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校准集自动回流 + 出分即校准（AC-3；ADR-0072 决策 3/4；宪法 §4C"一定是静默完成的"）。

- collect：owner 的 merge/reject 动作（GitHub PR 事件）自动成为校准样本——零额外
  操作。在线：gh api 拉已 merged / closed-with-review 的 PR；离线：--events-file
  fixture（事件文件格式与 gh api pulls 输出同构）。语义：
    merged                    → owner_action=approve（merge=owner 接受）
    closed 且有 CHANGES_REQUESTED 评审 → owner_action=reject
    closed 无评审证据          → 不入样本（ambiguous，无地面真值——宁缺勿滥）
  按 dedup_key 去重（幂等，重复事件零副作用）。
- score：敏感度/特异度（Wilson 置信区间）+ 校正分±CI；**CI 下界低于及格线 →
  needs_human=true（自动升人类信号）**——只记录不阻断（exit 0）。
  owner 确认率 ≥20% 为指标位（首版从 0 起累积，ADR-0072 决策 3）。

口径（敏感度/特异度校准，arXiv:2511.21140，ICML'26；Wilson CI，1927）：
  verifier 是 veto-only——judge_verdict=negative（否决）为阳性。
    TP=judge 负∧owner 拒；FN=judge 正∧owner 拒（漏报）；TN=judge 正∧owner 收；FP=judge 负∧owner 收。
  校正分 = 原始分 × sensitivity（judge 只能抓到 sens 比例的真问题，出分下行校正）；
  CI = [raw×sens_lo, raw×sens_hi]；校准对数不足（<min_joined_pairs）时 CI 取满宽
  [0,1]（不确定=不确定，不伪装）。

用法：
  python3 pipeline/verifier-exam/calibrate.py collect --repo ORG/REPO --since <ISO> \
      --out calibration/samples.jsonl [--events-file events.json]
  python3 pipeline/verifier-exam/calibrate.py score --samples calibration/samples.jsonl \
      --raw-score 0.85 --pass-line 0.80 [--out calibration/report.json]
"""
import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

Z = 1.96  # Wilson 95%

SCHEMA_SAMPLE = "verifier-calibration/sample/v1"


class CalibError(Exception):
    """配置/输入失败（exit 2）"""


def utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- Wilson 置信区间 ----

def wilson_ci(p_hat: float, n: int, z: float = Z):
    """比例的 Wilson 置信区间；n=0 时返回满宽 [0,1]（无数据=不确定）。"""
    if n <= 0:
        return 0.0, 1.0
    p = min(max(p_hat, 0.0), 1.0)
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5 / denom
    return max(0.0, center - half), min(1.0, center + half)


# ---- collect：owner 动作 → 校准样本（静默回流）----

def classify_pr(pr: dict) -> str:
    """GitHub PR 对象（gh api pulls 形态）→ owner_action（'approve'/'reject'/''不入样本）。"""
    if pr.get("merged_at"):
        return "approve"
    if pr.get("state") == "closed" and any(
            (r or {}).get("state") == "CHANGES_REQUESTED" for r in (pr.get("_reviews") or [])):
        return "reject"
    return ""   # closed 无评审证据：ambiguous，宁缺勿滥


def sample_from_pr(pr: dict, repo: str) -> dict:
    ts = pr.get("merged_at") or pr.get("closed_at") or utcnow_iso()
    action = classify_pr(pr)
    dk = f"{repo}#{pr['number']}@{ts}"
    return {"schema": SCHEMA_SAMPLE, "sample_id": "cal-" + hashlib.sha256(dk.encode()).hexdigest()[:10],
            "dedup_key": dk, "ts": ts, "source": "owner-action", "repo": repo,
            "pr": pr["number"], "owner_action": action, "judge_verdict": None}


def append_samples(out: Path, samples: list) -> int:
    """追加去重（对既有文件与本次批内都幂等）；返回新增条数。"""
    seen = set()
    if out.is_file():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                seen.add(json.loads(line)["dedup_key"])
    added = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        for s in samples:
            if s["owner_action"] and s["dedup_key"] not in seen:
                f.write(json.dumps(s, ensure_ascii=False, sort_keys=True) + "\n")
                seen.add(s["dedup_key"])
                added += 1
    return added


def fetch_events_online(repo: str, since: str) -> list:
    """gh api 拉 PR 事件（fail-closed：API 失败抛 CalibError，静默回流≠静默失败）。"""
    args = ["gh", "api", f"repos/{repo}/pulls",
            "-f", "state=closed", "-f", "sort=updated", "-f", "direction=desc",
            "-f", "per_page=30"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise CalibError(f"gh api pulls 失败（rc={r.returncode}）: {r.stderr[:200]}")
    prs = json.loads(r.stdout)
    out = []
    for pr in prs:
        ts = pr.get("merged_at") or pr.get("closed_at") or ""
        if not (ts and ts >= since):
            continue
        # 未合并且已关闭的 PR 需要 reviews 证据才能判 reject（merged 无需额外证据）
        if pr.get("merged_at") is None and pr.get("state") == "closed":
            rv = subprocess.run(["gh", "api", f"repos/{repo}/pulls/{pr['number']}/reviews",
                                 "-f", "per_page=30"], capture_output=True, text=True, timeout=60)
            if rv.returncode == 0:
                pr["_reviews"] = json.loads(rv.stdout)
        out.append(pr)
    return out


# ---- score：出分即校准 ----

def load_samples(path: Path) -> list:
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def score(samples: list, raw_score: float, pass_line: float, min_join: int) -> dict:
    joined = [s for s in samples if s.get("judge_verdict") in ("negative", "positive")]
    tp = sum(1 for s in joined if s["judge_verdict"] == "negative" and s["owner_action"] == "reject")
    fn = sum(1 for s in joined if s["judge_verdict"] == "positive" and s["owner_action"] == "reject")
    tn = sum(1 for s in joined if s["judge_verdict"] == "positive" and s["owner_action"] == "approve")
    fp = sum(1 for s in joined if s["judge_verdict"] == "negative" and s["owner_action"] == "approve")
    n_pos, n_neg = tp + fn, tn + fp
    sens = tp / n_pos if n_pos else 0.0
    spec = tn / n_neg if n_neg else 0.0
    sens_lo, sens_hi = wilson_ci(sens, n_pos)
    spec_lo, spec_hi = wilson_ci(spec, n_neg)
    if len(joined) >= min_join and n_pos and n_neg:
        corrected, ci_lo, ci_hi = raw_score * sens, max(0.0, raw_score * sens_lo), min(1.0, raw_score * sens_hi)
        status = "calibrated"
    else:
        corrected, ci_lo, ci_hi = raw_score, 0.0, 1.0   # 校准不足=满宽 CI（不伪装确定）
        status = "insufficient-calibration"
    needs_human = ci_lo < pass_line   # CI 下界低于及格线 → 自动升人类（记录信号，不阻断）
    total = len(samples)
    confirmed = len(joined)
    return {
        "schema": "verifier-calibration/report/v1",
        "ts": utcnow_iso(),
        "raw_score": raw_score,
        "corrected_score": corrected,
        "ci": [ci_lo, ci_hi],
        "confidence": "wilson-95",
        "pass_line": pass_line,
        "needs_human": needs_human,
        "escalation": "human" if needs_human else "none",
        "calibration_status": status,
        "confusion": {"tp": tp, "fn": fn, "tn": tn, "fp": fp, "joined_pairs": len(joined)},
        "sensitivity": {"hat": sens, "ci": [sens_lo, sens_hi]},
        "specificity": {"hat": spec, "ci": [spec_lo, spec_hi]},
        "owner_confirmation_rate": {   # ≥20% 指标位（首版从 0 起累积——样本静默回流即增长）
            "value": (confirmed / total) if total else 0.0,
            "target_min": 0.20,
            "samples": total,
            "status": "accumulating" if total < 100 else "steady",
        },
        "blocking": False,   # 校准信号不阻断记录（宪法 §4C：出分带校正值±CI，升人类≠拦路）
    }


def cmd_collect(args) -> int:
    since = args.since or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
                           ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.events_file:
        prs = json.loads(Path(args.events_file).read_text(encoding="utf-8"))
        prs = [p for p in prs if (p.get("merged_at") or p.get("closed_at") or "") >= since]
    else:
        prs = fetch_events_online(args.repo, since)
    samples = [sample_from_pr(p, args.repo) for p in prs]
    added = append_samples(Path(args.out), samples)
    stats = {"collected": len(samples), "appended": added,
             "skipped_ambiguous": sum(1 for s in samples if not s["owner_action"]),
             "since": since, "out": args.out}
    print(json.dumps(stats, ensure_ascii=False))
    return 0


def cmd_score(args) -> int:
    report = score(load_samples(Path(args.samples)), args.raw_score, args.pass_line, args.min_join)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0   # needs_human 只是信号——记录不阻断（AC-3）


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="校准集回流 + 出分即校准（ADR-0072）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect")
    c.add_argument("--repo", default="Cloudbird-Software/CI-Workflows")
    c.add_argument("--since", default=None, help="ISO 时间戳，缺省=回看 7 天")
    c.add_argument("--out", default="calibration/samples.jsonl")
    c.add_argument("--events-file", default=None, help="离线 fixture（gh api pulls 同构）")
    c.set_defaults(fn=cmd_collect)

    s = sub.add_parser("score")
    s.add_argument("--samples", default="calibration/samples.jsonl")
    s.add_argument("--raw-score", type=float, required=True, help="被校准的原始分（如最近考试 accuracy）")
    s.add_argument("--pass-line", type=float, default=0.80)
    s.add_argument("--min-join", type=int, default=30)
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_score)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except CalibError as e:
        print(f"::error::校准回流失败（fail-closed）: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
