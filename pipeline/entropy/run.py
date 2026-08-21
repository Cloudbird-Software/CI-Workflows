#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run.py —— 语义熵分歧度量 e2e 编排（W4-C1 .github#220，ADR-0066）。

派生（LLM，经计量 wrapper）→ 聚簇（零 LLM）→ 判定（零 LLM）→ [归因]
交叉质询（LLM）→ report.json（对 report.schema.json 断言后落盘）。
LLM 只出现在派生与质询两端；判定链路零 LLM（AC-4），报告 schema 见
report.schema.json（消费方：红队报告归档/波次收口，ADR-0066 决策 6）。

用法:
  python3 run.py --spec spec.md --out-dir out/ [--replay-dir r/] [--engine heuristic]
等价 bash 入口: run.sh（参数同上）。
"""
import argparse
import json
import os
import sys

import cluster  # noqa: E402
import derive   # noqa: E402
import examine  # noqa: E402
import judge    # noqa: E402
import policy   # noqa: E402

REPORT_SCHEMA = "entropy-report/v1"


def validate_report(report):
    """落盘前内置断言（jsonschema 不入 CI 依赖：必填面在此单点检查；
    report.schema.json 供外部消费方完整校验）。fail-closed。"""
    req_top = ["schema", "mode", "spec_path", "spec_sha256", "k", "lanes",
               "entailment_engine", "noise", "clusters", "verdict", "cross_examination"]
    miss = [k for k in req_top if k not in report]
    assert not miss, f"report 缺字段 {miss}"
    assert report["schema"] == REPORT_SCHEMA
    assert len(report["lanes"]) == policy.K
    for lane in report["lanes"]:
        assert lane.get("family") and lane.get("family_marker") is True
    assert isinstance(report["noise"]["B"], int) and report["noise"]["B"] >= 0
    v = report["verdict"]
    assert isinstance(v["attributed"], bool) and v["noise_margin"] == policy.NOISE_MARGIN
    assert bool(v["hotspots"]) == v["attributed"]   # 热点非空 ⇔ 归因（双向一致）
    for s in report["cross_examination"]["statements"]:
        assert s["spec_quote"] and s["clause_coordinate"]["id"]
        assert 1 <= s["rounds"] <= report["cross_examination"]["max_rounds"]
    if v["attributed"]:
        assert {s["clause"] for s in report["cross_examination"]["statements"]} \
            == set(v["hotspots"]), "热点条款与质询陈述不一致"


def run(spec_path, out_dir, replay_dir=None, engine="heuristic"):
    mode = "replay" if replay_dir else "live"
    spec_text = open(spec_path, encoding="utf-8").read()
    clause_ids = [c["id"] for c in derive.parse_spec_clauses(spec_text)]
    if not clause_ids:
        raise SystemExit("spec 无条款（`- ID: 文本` 形态）——无条款坐标可定位")

    derivations = derive.derive_all(spec_text, clause_ids, replay_dir)
    noise = derive.resample_noise(spec_text, clause_ids, replay_dir)
    clusters = cluster.build_clusters(derivations, noise, engine)
    clusters["lane_families"] = {f"lane{l['lane']}": l["family"] for l in derivations["lanes"]}
    verdict = judge.build_verdict(clusters)

    exam_path = os.path.join(out_dir, "examination.json")
    if not verdict["attributed"]:
        examination = {"schema": "entropy-examination/v1",
                       "max_rounds": policy.CROSS_EXAM_MAX_ROUNDS,
                       "rounds_used": 0, "statements": [],
                       "note": "未过底噪扣减门槛，不触发质询（AC-2/AC-3 门控）"}
    else:
        caller = (examine.ReplayCaller(replay_dir) if replay_dir
                  else examine.LiveCaller())
        rounds, statements = 0, []
        cmap = derive.spec_clause_map(spec_text)
        for clause in verdict["hotspots"]:
            ss = examine.run_examination(clause, cmap[clause],
                                         examine.representatives(clusters, clause), caller)
            rounds = max(rounds, max(s["rounds"] for s in ss))
            statements.extend(ss)
        examination = {"schema": "entropy-examination/v1",
                       "max_rounds": policy.CROSS_EXAM_MAX_ROUNDS,
                       "rounds_used": rounds, "statements": statements}

    report = {
        "schema": REPORT_SCHEMA, "mode": mode,
        "spec_path": os.path.basename(spec_path),
        "spec_sha256": derivations["spec_sha256"],
        "k": derivations["k"],
        "lanes": [{kk: l[kk] for kk in ("lane", "family", "model", "context", "family_marker")}
                  for l in derivations["lanes"]],
        "entailment_engine": engine,
        "noise": {"family": noise["family"], "resample_m": noise["resample_m"],
                  "B": clusters["noise"]["B"],
                  "self_clusters_per_clause": clusters["noise"]["self_clusters_per_clause"]},
        "clusters": {
            "global_lane_clusters": clusters["global_lane_clusters"],
            "global_members": clusters["global_members"],
            "per_clause": {c: {"clusters": v["clusters"], "members": v["members"],
                               "entropy_nats": v["entropy_nats"]}
                           for c, v in clusters["per_clause"].items()}},
        "verdict": verdict,
        "cross_examination": examination,
    }
    validate_report(report)

    os.makedirs(out_dir, exist_ok=True)
    for name, data in (("derivations.json", derivations), ("noise.json", noise),
                       ("clusters.json", clusters), ("verdict.json", verdict),
                       ("examination.json", examination), ("report.json", report)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    state = "归因 spec 歧义" if verdict["attributed"] else "不归因"
    print(f"[entropy] {state}：全局 lane 簇={clusters['global_lane_clusters']} "
          f"B={clusters['noise']['B']} 规则={verdict['rule']} "
          f"热点={verdict['hotspots'] or '无'} 质询轮次={examination['rounds_used']}"
          f"/{examination['max_rounds']}（模式 {mode}）")
    print(f"[entropy] 报告 → {os.path.join(out_dir, 'report.json')}")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="语义熵分歧度量 e2e（k=5 跨族+聚簇+底噪扣减+交叉质询）")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--replay-dir", help="回放目录（零凭据零 LLM 全链路）")
    ap.add_argument("--engine", default="heuristic", choices=["heuristic", "deberta-mnli"])
    args = ap.parse_args(argv)
    run(args.spec, args.out_dir, args.replay_dir, args.engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
