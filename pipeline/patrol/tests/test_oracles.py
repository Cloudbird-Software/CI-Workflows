#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""oracle 五类判定自测（W3-C2 AC-1；ADR-0065 风险缓解条款：每个 oracle 带
负控制——已知违约样本必须被抓）。全部打 demo 靶场（pipeline/patrol/demo-target），
零网络零真实 LLM。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import patrol  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(HERE, "..", "demo-target", "service.py")


def probe(payload):
    return patrol.run_probes(SERVICE, [payload])[0]


def grade(payload, oracle):
    return patrol.grade_payload(payload, probe(payload), oracle)


class TestFiveOracleClasses(unittest.TestCase):
    # 五类 oracle 各一组（违约必须抓 / 干净必须放）——机器可判定性的回归钉

    def test_crash_violation(self):
        v = grade({"op": "div", "a": 1.0, "b": 0.0}, {"class": "crash"})
        self.assertIsNotNone(v)
        self.assertEqual(v["class"], "crash")
        self.assertIn("exit=", v["symptom"])

    def test_crash_clean(self):
        self.assertIsNone(grade({"op": "div", "a": 6.0, "b": 3.0}, {"class": "crash"}))

    def test_http5xx_violation(self):
        v = grade({"op": "fetch", "id": "boom"}, {"class": "http-5xx"})
        self.assertIsNotNone(v)
        self.assertEqual(v["class"], "http-5xx")
        self.assertIn("500", v["symptom"])

    def test_http5xx_clean(self):
        self.assertIsNone(grade({"op": "fetch", "id": "x-1"}, {"class": "http-5xx"}))

    def test_schema_violation(self):
        v = grade({"op": "report", "include_debug": True},
                  {"class": "schema", "required": ["summary", "rows"]})
        self.assertIsNotNone(v)
        self.assertEqual(v["class"], "schema")
        self.assertIn("summary", v["symptom"])

    def test_schema_clean(self):
        self.assertIsNone(grade({"op": "report"},
                                {"class": "schema", "required": ["summary", "rows"]}))

    def test_invariant_violation(self):
        v = grade({"op": "transfer", "from_before": 2000, "to_before": 100, "amount": 1000.01},
                  {"class": "invariant",
                   "expr": "from_after + to_after == from_before + to_before"})
        self.assertIsNotNone(v)
        self.assertEqual(v["class"], "invariant")

    def test_invariant_clean_boundary(self):
        # 边界负控制：amount=1000 恰好守恒（>1000 才触发隐性费用）——防 oracle 误报
        self.assertIsNone(grade({"op": "transfer", "from_before": 2000, "to_before": 100,
                                 "amount": 1000},
                                {"class": "invariant",
                                 "expr": "from_after + to_after == from_before + to_before"}))

    def test_perf_violation(self):
        v = grade({"op": "search", "q": "zzz-full-scan"},
                  {"class": "perf-budget", "budget_ms": 200})
        self.assertIsNotNone(v)
        self.assertEqual(v["class"], "perf-budget")
        self.assertEqual(v["symptom"], "over-budget(>200ms)",
                         "症状只含稳定预算值——实测耗时波动值在 detail（指纹稳定）")
        self.assertIn("measured_ms=", v["detail"])

    def test_perf_clean(self):
        # service_ms 契约：干净路径的服务内耗时应远低于预算（墙钟含解释器启动不算账）
        pr = probe({"op": "search", "q": "hello"})
        env = json.loads(pr["stdout"])
        self.assertLess(env.get("service_ms", 10**9), 50)
        self.assertIsNone(grade({"op": "search", "q": "hello"},
                                {"class": "perf-budget", "budget_ms": 200}))

    def test_negative_control_matrix(self):
        """ADR-0065 风险缓解：已知违约样本逐类必抓（oracle 自身失效会被此矩阵钉死）。"""
        matrix = [
            ({"op": "div", "a": 1.0, "b": 0.0}, {"class": "crash"}),
            ({"op": "fetch", "id": "boom"}, {"class": "http-5xx"}),
            ({"op": "report", "include_debug": True},
             {"class": "schema", "required": ["summary", "rows"]}),
            ({"op": "transfer", "from_before": 2000, "to_before": 100, "amount": 1000000},
             {"class": "invariant",
              "expr": "from_after + to_after == from_before + to_before"}),
            ({"op": "search", "q": "zzz-x"}, {"class": "perf-budget", "budget_ms": 200}),
        ]
        for payload, oracle in matrix:
            with self.subTest(oracle=oracle["class"]):
                self.assertIsNotNone(grade(payload, oracle),
                                     f"负控制失守：{oracle['class']} 未抓到已知违约")

    def test_oracle_class_closed_set(self):
        with self.assertRaises(SystemExit) as cm:
            patrol._valid_oracle({"class": "looks-wrong"}, "T")
        self.assertEqual(cm.exception.code, 2)  # LLM 主观类进不了 oracle（走 observation 桶）


class TestMetamorphicOracle(unittest.TestCase):
    """metamorphic 等价 oracle：交换律破坏必须抓；数值形态等价必须放（防误报）。"""

    def _scenarios(self, seed):
        return {s["id"]: s for s in patrol.scenarios_metamorphic(seed, 4, set())}

    def test_commute_catches_seeded_equivalence_break(self):
        sc = self._scenarios(42)["mt-commute-add"]
        prs = patrol.run_probes(SERVICE, sc["payloads"])
        f = patrol.grade_metamorphic(sc, prs, SERVICE)
        self.assertIsNotNone(f)
        self.assertEqual(f["violation"]["class"], "invariant")
        self.assertIn("metamorphic:commute-add", f["violation"]["symptom"])

    def test_numform_is_clean_relation(self):
        # 7 与 7.0 数值等价（Python 7.0==7 为 True，服务对两种形态同判）——
        # 干净 metamorphic 关系必须零误报（oracle 负控制的另一面）
        sc = self._scenarios(42)["mt-numform"]
        prs = patrol.run_probes(SERVICE, sc["payloads"])
        self.assertIsNone(patrol.grade_metamorphic(sc, prs, SERVICE))

    def test_pairs_seed_determinism(self):
        a = [s["payloads"] for s in patrol.scenarios_metamorphic(7, 4, set())]
        b = [s["payloads"] for s in patrol.scenarios_metamorphic(7, 4, set())]
        self.assertEqual(a, b)  # 同 seed 同变体（可复现可审计）


if __name__ == "__main__":
    unittest.main()
