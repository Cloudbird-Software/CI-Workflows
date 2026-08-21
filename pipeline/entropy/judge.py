#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""judge.py —— 簇数/熵阈值判定 + 底噪扣减（W4-C1 .github#220，ADR-0066 决策 3/4）。

零 LLM/零网络确定性脚本（AC-4）：输入 cluster.py 的 clusters.json，输出 verdict。
判定规则唯一且可静态审计：**跨族簇数 C − 底噪自簇数 B >= NOISE_MARGIN 才归因
spec 歧义**。铁律（ADR-0066）：分歧绝不退化为投票——本文件无任何多数决逻辑，
簇内样本数只进熵与报告，不进判定；测试 test_static_zero_llm.py 含"反投票"
断言（簇成员数改变而簇数不变 → 判定不变）。

用法: python3 judge.py --clusters c.json --out v.json
"""
import argparse
import json
import sys

import policy


def build_verdict(clusters):
    """纯函数：clusters.json → verdict dict。同输入同输出（确定性）。"""
    b = clusters["noise"]["B"] if clusters.get("noise") else 0
    per_clause = {}
    attributed = []
    for clause, v in sorted(clusters["per_clause"].items()):
        excess = v["clusters"] - b
        is_hot = excess >= policy.NOISE_MARGIN
        per_clause[clause] = {
            "cross_family_clusters": v["clusters"],
            "noise_b": b,
            "excess": excess,
            "attributed": is_hot,
            "entropy_nats": v["entropy_nats"],
        }
        if is_hot:
            attributed.append((excess, v["entropy_nats"], clause))
    # 热点排序：净分歧簇数降序，次键熵，末键条款 ID（全确定性，无平局歧义）
    attributed.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return {
        "schema": "entropy-verdict/v1",
        "rule": f"cross_family_clusters - B >= {policy.NOISE_MARGIN}（AC-2）",
        "noise_b": b,
        "noise_margin": policy.NOISE_MARGIN,
        "global_lane_clusters": clusters["global_lane_clusters"],
        "attributed": bool(attributed),
        "hotspots": [c for _, _, c in attributed],
        "per_clause": per_clause,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="簇数/底噪扣减判定（零 LLM）")
    ap.add_argument("--clusters", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    clusters = json.load(open(args.clusters, encoding="utf-8"))
    verdict = build_verdict(clusters)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    state = "归因 spec 歧义" if verdict["attributed"] else "不归因（未过底噪扣减门槛）"
    print(f"判定：{state}；B={verdict['noise_b']} "
          f"hotspots={verdict['hotspots'] or '无'}（规则 {verdict['rule']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
