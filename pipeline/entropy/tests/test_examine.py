#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""examine.py 自测（W4-C1 AC-3）：结构化不一致陈述（spec 原文引用+条款 ID 坐标
由确定性侧构造保证）；质询轮次硬上限；call_llm 可注入（测试桩零 LLM）。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import examine  # noqa: E402
import policy  # noqa: E402

CLAUSE_INFO = {"id": "INV-2", "section": "INV 不变量", "line": 21,
               "text": "高峰期网关对超出配额的请求进行限制，保障核心服务可用"}

REPS = [
    {"cluster": 0, "lane": "lane1", "family": "glm", "reading": "直接拒绝返回 429", "members": ["lane1", "lane2"]},
    {"cluster": 1, "lane": "lane3", "family": "qwen", "reading": "排队降级转发", "members": ["lane3", "lane4"]},
    {"cluster": 2, "lane": "lane5", "family": "llama", "reading": "只拒绝新到请求", "members": ["lane5"]},
]


def resp(converged):
    return json.dumps({"statement": "分歧陈述：原文'限制'未定义处置动作",
                       "ambiguity_type": "处置动作未定义",
                       "stance_converged": converged}, ensure_ascii=False)


class CountingCaller:
    def __init__(self, converged):
        self.converged, self.calls = converged, 0

    def __call__(self, prompt):
        self.calls += 1
        return resp(self.converged)


class TestRoundCap(unittest.TestCase):
    def test_hard_cap_when_never_converged(self):
        """AC-3：永不收敛 → 轮次被硬上限封顶（policy 常量），不再多打一轮。"""
        caller = CountingCaller(False)
        stmts = examine.run_examination("INV-2", CLAUSE_INFO, REPS, caller)
        n_pairs = 3   # C(3,2)=3 对
        self.assertEqual(caller.calls, n_pairs * 2 * policy.CROSS_EXAM_MAX_ROUNDS)
        for s in stmts:
            self.assertEqual(s["rounds"], policy.CROSS_EXAM_MAX_ROUNDS)
            self.assertFalse(s["converged"])

    def test_early_stop_when_converged(self):
        """双方承认两读法均成立 → 提前停轮（1 轮即止）。"""
        caller = CountingCaller(True)
        stmts = examine.run_examination("INV-2", CLAUSE_INFO, REPS, caller)
        self.assertEqual(caller.calls, 3 * 2 * 1)
        for s in stmts:
            self.assertEqual(s["rounds"], 1)
            self.assertTrue(s["converged"])

    def test_cap_is_policy_constant(self):
        self.assertEqual(policy.CROSS_EXAM_MAX_ROUNDS, 2)


class TestStatementStructure(unittest.TestCase):
    def test_statement_has_quote_and_coordinate(self):
        """AC-3：陈述含 spec 原文引用（逐字、构造上保证）+ 条款 ID 坐标。"""
        stmts = examine.run_examination("INV-2", CLAUSE_INFO, REPS, CountingCaller(True))
        self.assertEqual(len(stmts), 3)
        for s in stmts:
            self.assertEqual(s["spec_quote"], CLAUSE_INFO["text"])       # 原文逐字
            self.assertEqual(s["clause_coordinate"]["id"], "INV-2")
            self.assertEqual(s["clause_coordinate"]["line"], 21)
            self.assertIn("statement_a", s) and self.assertIn("statement_b", s)
            self.assertEqual(len(s["pair_clusters"]), 2)
            self.assertIn(s["ambiguity_type"], ("处置动作未定义", None))
        # 两两配对全覆盖（C(3,2)）
        pairs = {tuple(s["pair_clusters"]) for s in stmts}
        self.assertEqual(pairs, {(0, 1), (0, 2), (1, 2)})

    def test_parse_exam_tolerates_non_json(self):
        out = examine.parse_exam("不是 JSON 的陈述文本")
        self.assertTrue(out["raw"])
        self.assertEqual(out["stance_converged"], False)
        out2 = examine.parse_exam(resp(True))
        self.assertFalse(out2["raw"]) and self.assertTrue(out2["stance_converged"])


if __name__ == "__main__":
    unittest.main()
