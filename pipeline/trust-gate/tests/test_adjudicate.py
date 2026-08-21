#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""adjudicate 判定自测（W5-C2 AC-1/AC-4，ADR-0071 决策 1/6/7）。

核心断言：缺证据=拒绝（不是中性、不是降级）且逐项列出缺失谓词键——
每个域的每个证据谓词键逐个缺失断言；熔断前置 fail-closed（ADR-0040 联动）；
排除域永远人签（证据全绿也不放行）；锁定域只出 would-* 形态（executed=false）。
零网络零 LLM。
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trust_gate  # noqa: E402

from _helpers import PREDICATES, UNLOCK_STATE, all_ev, bundle  # noqa: E402


class GateCase(unittest.TestCase):
    def setUp(self):
        self.p = trust_gate.load_predicates(PREDICATES)
        self.locked = trust_gate.load_unlock_state(UNLOCK_STATE, self.p)
        self.unlocked = {d: "unlocked" for d in self.p["domains"]}

    def judge(self, b, unlock=None, sample_prs=None):
        return trust_gate.adjudicate(b, self.p, unlock if unlock is not None else self.locked,
                                     sample_prs)


class TestMissingEvidenceRejects(GateCase):
    """AC-1：缺证据=拒绝——每域每谓词键逐个缺失断言（含关卡键）。"""

    def test_every_domain_every_predicate_key(self):
        for domain, spec in self.p["domains"].items():
            for key in spec["requires"]:
                ev = all_ev(domain, self.p)
                del ev[key]  # 逐个缺失
                out = self.judge(bundle(domain=domain, evidence=ev))
                self.assertEqual(out["decision"], "would-reject",
                                 f"{domain} 缺 {key} 必须拒绝（影子形态）")
                self.assertEqual(out["missing_predicates"], [key])
                self.assertEqual(out["reason"], "missing-predicates")
                self.assertFalse(out["executed"])

    def test_false_evidence_is_missing_too(self):
        """证据键存在但为 false = 与缺位同罪（不是中性）。"""
        ev = all_ev("docs-only", self.p)
        ev["evidence.negative-control"] = False
        out = self.judge(bundle(evidence=ev))
        self.assertEqual(out["decision"], "would-reject")
        self.assertIn("evidence.negative-control", out["missing_predicates"])

    def test_gate_not_green_rejects(self):
        """全关卡绿谓词：gate 缺席/失败/取消都是拒绝，仅 success 放行。"""
        for conclusion in (None, "failure", "timed_out", "cancelled", "skipped"):
            checks = {} if conclusion is None else {"gate": conclusion}
            out = self.judge(bundle(checks=checks))
            self.assertEqual(out["decision"], "would-reject", f"gate={conclusion} 必须拒绝")
            self.assertIn("gates.gate", out["missing_predicates"])

    def test_multiple_missing_all_listed(self):
        """缺失多项时报告逐项列全（报告完整性=可解释可回放）。"""
        out = self.judge(bundle(evidence={}))  # 关卡绿、证据全缺
        self.assertEqual(out["missing_predicates"],
                         self.p["domains"]["docs-only"]["requires"])
        out2 = self.judge(bundle(evidence={}, checks={}))  # 关卡+证据全缺
        self.assertEqual(len(out2["missing_predicates"]),
                         1 + len(self.p["domains"]["docs-only"]["requires"]))
        self.assertIn("gates.gate", out2["missing_predicates"])


class TestBreakerPrecedence(GateCase):
    """ADR-0040 联动：AUTO_MERGE_DISABLED 置位 → 前置直接拒绝（先于一切域判定）。"""

    def test_breaker_trumped_everything(self):
        b = bundle(breaker=True)  # 证据全绿 + 域已解锁形态下仍拒绝
        out = self.judge(b, unlock=self.unlocked)
        self.assertEqual(out["decision"], "reject")
        self.assertEqual(out["reason"], "circuit-breaker-tripped")
        self.assertFalse(out["executed"])

    def test_breaker_beats_missing_evidence_report(self):
        """熔断先行：此时缺失清单为空（拒绝理由唯一且明确=熔断，不混报告）。"""
        b = bundle(breaker=True, evidence={})
        out = self.judge(b)
        self.assertEqual(out["reason"], "circuit-breaker-tripped")
        self.assertEqual(out["missing_predicates"], [])


class TestExcludedDomains(GateCase):
    """AC-4：前三域外（新功能/依赖升级/API 变更/CI-Workflows）永远人签。"""

    def test_excluded_domains_never_auto(self):
        for domain in self.p["excluded_domains"]:
            out = self.judge(bundle(domain=domain), unlock=self.unlocked)
            self.assertEqual(out["decision"], "human-sign", f"{domain} 必须人签")
            self.assertEqual(out["reason"], "excluded-domain")
            self.assertEqual(out["mode"], "excluded")

    def test_unknown_domain_rejected_fail_closed(self):
        """表外域声明=拒绝（fail-closed），不是放行也不是默认人签。"""
        out = self.judge(bundle(domain="totally-new-thing"), unlock=self.unlocked)
        self.assertEqual(out["decision"], "would-reject")
        self.assertEqual(out["reason"], "unknown-domain")


class TestUnlockForms(GateCase):
    """AC-4：解锁形态 auto-merge；锁定形态 would-*（纯记录不执行）。"""

    def test_locked_domain_yields_would_merge(self):
        out = self.judge(bundle())
        self.assertEqual(out["decision"], "would-merge")
        self.assertEqual(out["mode"], "shadow")
        self.assertFalse(out["executed"])  # AC-2：不执行

    def test_unlocked_domain_yields_auto_merge(self):
        out = self.judge(bundle(), unlock=self.unlocked)
        self.assertEqual(out["decision"], "auto-merge")
        self.assertEqual(out["mode"], "enforced")
        self.assertEqual(out["missing_predicates"], [])

    def test_sample_review_beats_auto_merge(self):
        """解锁域 5% 抽审命中 → 转人审（ADR-0071 决策 5，fail-closed 方向）。"""
        out = self.judge(bundle(pr=7), unlock=self.unlocked, sample_prs={7})
        self.assertEqual(out["decision"], "sample-review")
        self.assertTrue(out["sample_review"])
        # 未命中样本照常 auto-merge
        out2 = self.judge(bundle(pr=8), unlock=self.unlocked, sample_prs={7})
        self.assertEqual(out2["decision"], "auto-merge")

    def test_required_predicates_reported_in_order(self):
        """required_predicates = 关卡键 + 域证据键（审计者可按清单复算）。"""
        out = self.judge(bundle())
        self.assertEqual(out["required_predicates"],
                         ["gates.gate"] + self.p["domains"]["docs-only"]["requires"])

    def test_engine_never_executes(self):
        """引擎在任何形态下都不执行合并（执行权在合并机器人，引擎只出判定）。"""
        for unlock in (self.locked, self.unlocked):
            self.assertFalse(self.judge(bundle(), unlock=unlock)["executed"])


if __name__ == "__main__":
    unittest.main()
