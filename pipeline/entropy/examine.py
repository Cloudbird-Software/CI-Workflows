#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""examine.py —— LM vs LM 交叉质询定位（W4-C1 .github#220，ADR-0066 决策 5）。

超阈值（judge 归因）后运行：对每个热点条款，各簇代表两两交叉质询——互问
"你对条款 X 的理解与依据"。输出**结构化不一致陈述**：条款 ID 坐标 + spec
原文引用由**确定性侧**从 spec 解析原文逐字附带（不信任 LLM 转述——引用
真值在构造上保证，INV-10 精神），LLM 只产出质询陈述（可含歧义类型标注，
MAST 模式归因的轻量承接）。

轮次硬上限 = policy.CROSS_EXAM_MAX_ROUNDS（防成本失控）：循环上界即上限，
单元测试以"永不收敛"桩断言封顶。LLM 调用唯走计量 wrapper（INV-06）；
回放模式按确定序消费 examine-NNN.json。

质询不是投票：输出只是分歧陈述与各方立场，不改判归因（判定在 judge.py）。

用法:
  python3 examine.py --clusters c.json --verdict v.json --spec spec.md \
      [--replay-dir r/] --out e.json
"""
import argparse
import itertools
import json
import os
import re
import sys

import derive  # noqa: E402  复用 wrapper 调用与条款解析（编排侧，非判定链）
import policy  # noqa: E402

EXAM_TMPL = """[交叉质询 · LM vs LM] 派生者族标记：{family}（lane {lane}）。
你此前对条款 {clause} 的实现读法：{reading}
另一派生者的读法：{other}
条款原文：{quote}

请针对条款 {clause}：陈述你的读法依据（引用原文关键短语），并指出与对方
读法的具体分歧点。若你承认两种读法在原文下均成立，如实说明。
输出（严格 JSON，无围栏）：{{"statement": "<分歧陈述，1-3 句>", "ambiguity_type": "<可选：分歧类型短语>", "stance_converged": <true|false>}}
"""


def parse_exam(content):
    """解析质询响应；非 JSON 容忍降级为原文陈述（raw=true），不 fail 整链。"""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(0))
            return {"statement": str(d.get("statement", "")).strip(),
                    "ambiguity_type": str(d.get("ambiguity_type") or "").strip() or None,
                    "stance_converged": bool(d.get("stance_converged")), "raw": False}
        except ValueError:
            pass
    return {"statement": content.strip()[:500], "ambiguity_type": None,
            "stance_converged": False, "raw": True}


def representatives(clusters, clause):
    """簇代表（每簇首个成员 lane 的读法）+ 簇成员清单。"""
    pc = clusters["per_clause"][clause]
    reps = []
    for gi, (members, readings) in enumerate(zip(pc["members"], pc["readings"])):
        lane = members[0]
        reps.append({"cluster": gi, "lane": lane,
                     "family": _lane_family(clusters, lane),
                     "reading": readings[0],
                     "members": members})
    return reps


def _lane_family(clusters, lane_id):
    # lane_id 形如 "lane3"；族映射来自 clusters 附带（run.py 注入），查不到 → None
    return (clusters.get("lane_families") or {}).get(lane_id)


def run_examination(clause, clause_info, reps, call_llm, max_rounds=None):
    """单条款两两交叉质询（call_llm 可注入——live/回放/测试桩同构）。

    返回 statements 列表；轮次上界 = max_rounds（policy 常量），循环即封顶。
    """
    max_rounds = policy.CROSS_EXAM_MAX_ROUNDS if max_rounds is None else max_rounds
    quote = clause_info["text"]
    statements = []
    for a, b in itertools.combinations(reps, 2):
        rounds_used, conv_a, conv_b = 0, False, False
        stmt = {"clause": clause,
                "clause_coordinate": {"id": clause, "section": clause_info.get("section"),
                                      "line": clause_info.get("line")},
                "spec_quote": quote,   # 确定性逐字引用（真值由构造保证）
                "pair_clusters": [a["cluster"], b["cluster"]],
                "position_a": {k: a[k] for k in ("cluster", "lane", "family", "reading")},
                "position_b": {k: b[k] for k in ("cluster", "lane", "family", "reading")}}
        for rnd in range(1, max_rounds + 1):   # 硬上限：循环上界
            rounds_used = rnd
            ra = parse_exam(call_llm(EXAM_TMPL.format(
                family=a["family"], lane=a["lane"], clause=clause,
                reading=a["reading"], other=b["reading"], quote=quote)))
            rb = parse_exam(call_llm(EXAM_TMPL.format(
                family=b["family"], lane=b["lane"], clause=clause,
                reading=b["reading"], other=a["reading"], quote=quote)))
            conv_a, conv_b = ra["stance_converged"], rb["stance_converged"]
            stmt.update(statement_a=ra["statement"], statement_b=rb["statement"],
                        ambiguity_type=(ra["ambiguity_type"] or rb["ambiguity_type"]))
            if conv_a and conv_b:
                break   # 双方承认两种读法均成立 → 分歧已显性化，提前停轮
        stmt.update(rounds=rounds_used, converged=bool(conv_a and conv_b))
        statements.append(stmt)
    return statements


# ---------------- 调用器（live / 回放） ----------------

class LiveCaller:
    """经计量 wrapper 调 LLM（逐次自增序号入 invoke_id，幂等去重）。"""

    def __init__(self):
        self.seq = 0

    def __call__(self, prompt):
        self.seq += 1
        return derive.call_wrapper(prompt, policy.FAMILY_ROUTES[policy.NOISE_FAMILY][0],
                                   policy.EXAMINE_ROLE, f"entropy-examine-{derive.RUN_NONCE}-{self.seq:03d}",
                                   base_url=policy.route_base_url(policy.NOISE_FAMILY))


class ReplayCaller:
    """按确定序消费回放目录 examine-NNN.json——同样经 wrapper（--replay-file
    路径，落计量账本），保持与 live 完全一致的 invoke 记录形态。"""

    def __init__(self, replay_dir):
        self.files = sorted(
            f for f in os.listdir(replay_dir) if f.startswith("examine-") and f.endswith(".json"))
        self.dir, self.seq = replay_dir, 0

    def __call__(self, prompt):
        self.seq += 1
        if self.seq > len(self.files):
            raise SystemExit(f"回放响应不足：需第 {self.seq} 份，目录仅 {len(self.files)} 份"
                             f"（{self.dir}）——fail-closed，不静默降级")
        return derive.call_wrapper(prompt, "replay-examine", policy.EXAMINE_ROLE,
                                   f"entropy-examine-replay-{derive.RUN_NONCE}-{self.seq:03d}",
                                   replay_file=os.path.join(self.dir, self.files[self.seq - 1]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="LM vs LM 交叉质询定位（轮次硬上限）")
    ap.add_argument("--clusters", required=True)
    ap.add_argument("--verdict", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--replay-dir")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    clusters = json.load(open(args.clusters, encoding="utf-8"))
    verdict = json.load(open(args.verdict, encoding="utf-8"))
    spec_text = open(args.spec, encoding="utf-8").read()
    clause_map = derive.spec_clause_map(spec_text)
    if not verdict["attributed"]:
        result = {"schema": "entropy-examination/v1", "max_rounds": policy.CROSS_EXAM_MAX_ROUNDS,
                  "rounds_used": 0, "statements": [],
                  "note": "未过底噪扣减门槛，不触发质询（AC-2/AC-3 门控）"}
    else:
        caller = ReplayCaller(args.replay_dir) if args.replay_dir else LiveCaller()
        rounds, statements = 0, []
        for clause in verdict["hotspots"]:
            if clause not in clause_map:
                raise SystemExit(f"热点条款 {clause} 不在 spec（坐标断裂，fail-closed）")
            ss = run_examination(clause, clause_map[clause],
                                 representatives(clusters, clause), caller)
            rounds = max(rounds, max(s["rounds"] for s in ss))
            statements.extend(ss)
        result = {"schema": "entropy-examination/v1",
                  "max_rounds": policy.CROSS_EXAM_MAX_ROUNDS,
                  "rounds_used": rounds, "statements": statements}
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"质询完成：轮次 {result['rounds_used']}/{result['max_rounds']}（硬上限）"
          f"陈述 {len(result['statements'])} 份 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
