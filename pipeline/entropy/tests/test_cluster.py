#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cluster.py 自测（W4-C1 AC-1 机制面）：语义聚簇=同义不同措辞并簇、异读法
裂簇；传递闭包；结构等价；熵；引擎可插拔（deberta-mnli 未部署 fail-closed）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cluster  # noqa: E402
import policy   # noqa: E402

# 同义不同措辞（语序/功能词/同义替换，术语保持——真实派生形态）
PARA_A = "每个租户有独立配额，配额按分钟窗口到期重置"
PARA_B = "租户各自维护独立配额，按分钟窗口周期重置配额"
PARA_C = "为租户维护独立的分钟窗口配额，窗口到期时重置"
# 不同读法（同一 ambiguous 条款的两种实现）
READ_REJECT = "超出配额的请求直接拒绝，返回 429 状态码，提示调用方稍后重试"
READ_QUEUE = "超额请求进入降级队列排队转发，非核心字段被裁剪，响应标记降级"


class TestHeuristicEntailment(unittest.TestCase):
    def test_paraphrase_clusters_together(self):
        """AC-1：同义不同措辞 → 互蕴含（不误报的机制基础）。"""
        self.assertTrue(cluster.heuristic_bidirectional(PARA_A, PARA_B))
        self.assertTrue(cluster.heuristic_bidirectional(PARA_A, PARA_C))

    def test_distinct_readings_split(self):
        """不同读法 → 不蕴含（分歧可测）。"""
        self.assertFalse(cluster.heuristic_bidirectional(READ_REJECT, READ_QUEUE))

    def test_structural_equivalence(self):
        """结构等价：JSON 同构即等价（字段次序无关）；异值不等价。"""
        self.assertTrue(cluster.heuristic_bidirectional(
            '{"a":1,"b":[1,2]}', '{"b":[1,2],"a":1}'))
        self.assertFalse(cluster.heuristic_bidirectional(
            '{"action":"reject immediately with http status 429"}',
            '{"action":"queue then degrade and trim non-core fields"}'))

    def test_transitive_closure(self):
        """传递闭包：A~B、B~C 而 A≁C → 仍归一簇（并查集语义）。"""
        texts = [PARA_A, PARA_B, PARA_C]
        entail = lambda x, y: (x, y) in {(PARA_A, PARA_B), (PARA_B, PARA_A),
                                         (PARA_B, PARA_C), (PARA_C, PARA_B)}
        self.assertEqual(cluster.cluster_texts(texts, entail), [[0, 1, 2]])

    def test_cluster_texts_on_mixed(self):
        groups = cluster.cluster_texts(
            [PARA_A, READ_REJECT, PARA_B, READ_QUEUE], cluster.heuristic_bidirectional)
        self.assertEqual(groups, [[0, 2], [1], [3]])


class TestEntropy(unittest.TestCase):
    def test_entropy_uniform_split(self):
        self.assertAlmostEqual(cluster.semantic_entropy([2, 2]), 0.6931471805599453)

    def test_entropy_degenerate(self):
        self.assertEqual(cluster.semantic_entropy([5]), 0.0)
        self.assertEqual(cluster.semantic_entropy([]), 0.0)


class TestBuildClustersNoise(unittest.TestCase):
    def _deriv(self, readings_per_lane):
        lanes = []
        for i, readings in enumerate(readings_per_lane, 1):
            lanes.append({"lane": i, "family": f"fam{i}", "model": "m",
                          "context": "cold", "family_marker": True,
                          "readings": [{"clause": c, "text": t} for c, t in readings]})
        return {"schema": "entropy-derivations/v1", "mode": "replay",
                "spec_sha256": "sha256:" + "0" * 64, "k": len(lanes), "lanes": lanes}

    def test_per_clause_and_noise_B(self):
        """AC-2 机制：底噪自簇数 B = 各条款自簇数的最大值；跨族簇独立记录。"""
        reject, queue = READ_REJECT, READ_QUEUE
        d = self._deriv([
            [("C1", PARA_A), ("C2", reject)],
            [("C1", PARA_B), ("C2", reject)],
            [("C1", PARA_C), ("C2", queue)],
        ])
        n = {"family": "glm", "samples": [
            {"sample": 1, "family": "glm", "readings": [{"clause": "C1", "text": PARA_A},
                                                        {"clause": "C2", "text": reject}]},
            {"sample": 2, "family": "glm", "readings": [{"clause": "C1", "text": PARA_A},
                                                        {"clause": "C2", "text": queue}]},
        ]}
        c = cluster.build_clusters(d, n, "heuristic")
        self.assertEqual(c["per_clause"]["C1"]["clusters"], 1)   # 同义改写并簇
        self.assertEqual(c["per_clause"]["C2"]["clusters"], 2)   # 异读法裂簇
        self.assertEqual(c["noise"]["B"], 2)                     # C2 自分歧 2 簇 → B
        self.assertEqual(c["noise"]["self_clusters_per_clause"]["C1"], 1)

    def test_determinism(self):
        """判定链确定性：同输入两次构建输出全等（无时间戳/随机源）。"""
        d = self._deriv([[("C1", PARA_A)], [("C1", PARA_B)]])
        a = cluster.build_clusters(d, None, "heuristic")
        b = cluster.build_clusters(d, None, "heuristic")
        self.assertEqual(a, b)


class TestEnginePluggable(unittest.TestCase):
    def test_heuristic_default(self):
        self.assertIs(cluster.get_engine("heuristic"), cluster.heuristic_bidirectional)

    def test_unknown_engine_rejected(self):
        with self.assertRaises(SystemExit):
            cluster.get_engine("gpt-as-judge")

    def test_deberta_mnli_fail_closed_without_service(self):
        """deberta-mnli：接口在，未部署 fail-closed（不静默回落 heuristic）。"""
        import nli_deberta  # 惰性路径的手工触发面
        os.environ.pop("ENTAILMENT_NLI_URL", None)
        with self.assertRaises(nli_deberta.NliUnavailable):
            nli_deberta.bidirectional(PARA_A, PARA_B)
        eng = cluster.get_engine("deberta-mnli")
        self.assertIs(eng, nli_deberta.bidirectional)


if __name__ == "__main__":
    unittest.main()
