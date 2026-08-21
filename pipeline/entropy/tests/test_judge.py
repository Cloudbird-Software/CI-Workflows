#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""judge.py 自测（W4-C1 AC-2 判定面 + 反投票铁律）：
底噪扣减 B 记录在案并参与判定；仅跨族簇数−B>=2 才归因；
判定=簇数/熵，绝不退化为投票（簇成员数改变簇数不变 → 判定不变）。"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import judge   # noqa: E402
import policy  # noqa: E402


def mk_clusters(clause_clusters, b):
    """构造 clusters.json 形态：{条款: 跨族簇数} + 底噪 B。"""
    per_clause = {}
    for clause, n in clause_clusters.items():
        per_clause[clause] = {"clusters": n,
                              "members": [[f"lane{i}"] for i in range(n)],
                              "readings": [[f"r{i}"] for i in range(n)],
                              "entropy_nats": 0.5}
    return {"schema": "entropy-clusters/v1", "engine": "heuristic", "k": policy.K,
            "global_lane_clusters": 1, "global_members": [["lane1"]],
            "per_clause": per_clause,
            "noise": {"family": "glm", "resample_m": 3,
                      "self_clusters_per_clause": {"X": b}, "B": b}}


class TestNoiseSubtraction(unittest.TestCase):
    def test_ac2_case_3_minus_2_not_attributed(self):
        """卡面 AC-2 数值：B=2、跨族 3 簇 → 3−2=1 <2 不报。"""
        v = judge.build_verdict(mk_clusters({"C1": 3}, 2))
        self.assertEqual(v["noise_b"], 2)                     # B 被记录
        self.assertFalse(v["attributed"])
        self.assertEqual(v["per_clause"]["C1"]["excess"], 1)

    def test_ac2_case_4_minus_2_attributed(self):
        """卡面 AC-2 数值：B=2、跨族 4 簇 → 4−2=2 >=2 报。"""
        v = judge.build_verdict(mk_clusters({"C1": 4}, 2))
        self.assertTrue(v["attributed"])
        self.assertEqual(v["hotspots"], ["C1"])
        self.assertEqual(v["per_clause"]["C1"]["excess"], 2)

    def test_below_margin_without_noise(self):
        """无底噪数据（B=0）时 2 簇即报；1 簇不报。"""
        self.assertTrue(judge.build_verdict(mk_clusters({"C1": 2}, 0))["attributed"])
        self.assertFalse(judge.build_verdict(mk_clusters({"C1": 1}, 0))["attributed"])

    def test_hotspot_ranking_deterministic(self):
        v = judge.build_verdict(mk_clusters({"C2": 5, "C1": 4, "C3": 4}, 2))
        self.assertEqual(v["hotspots"], ["C2", "C1", "C3"])   # 净分歧降序、ID 决胜


class TestNoVoting(unittest.TestCase):
    """铁律（ADR-0066）：分歧绝不退化为投票。"""

    def test_cluster_sizes_do_not_change_verdict(self):
        """反投票：4+1 裂 vs 3+2 裂（多数派强弱互换，簇数同）→ 判定一致。"""
        base = mk_clusters({"C1": 2}, 0)
        skew = copy.deepcopy(base)
        # 4:1（某一读法占多数）
        base["per_clause"]["C1"]["members"] = [
            ["lane1", "lane2", "lane3", "lane4"], ["lane5"]]
        # 3:2（多数派变弱）
        skew["per_clause"]["C1"]["members"] = [
            ["lane1", "lane2", "lane3"], ["lane4", "lane5"]]
        v1, v2 = judge.build_verdict(base), judge.build_verdict(skew)
        self.assertEqual(v1["attributed"], v2["attributed"])
        self.assertEqual(v1["hotspots"], v2["hotspots"])
        self.assertEqual(v1["per_clause"]["C1"]["excess"],
                         v2["per_clause"]["C1"]["excess"])

    def test_rule_is_cluster_count_not_majority(self):
        """判定规则文案与常量钉死：跨族簇数−B>=NOISE_MARGIN，无任何计票语义。"""
        v = judge.build_verdict(mk_clusters({"C1": 3}, 1))
        self.assertIn(f">= {policy.NOISE_MARGIN}", v["rule"])
        self.assertEqual(v["noise_margin"], policy.NOISE_MARGIN)
        self.assertEqual(policy.NOISE_MARGIN, 2)


if __name__ == "__main__":
    unittest.main()
