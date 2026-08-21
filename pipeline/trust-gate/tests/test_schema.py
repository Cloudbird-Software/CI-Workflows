#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""predicates.yaml / unlock-state.yaml / bundle 严格校验自测（W5-C2，ADR-0071 决策 1）。

fail-closed 加载语义：策略表任何漂移（未知键/缺硬编码排除项/域集合不一致/
阈值类型不符）→ TrustGateError（infra），绝不静默降级为默认值。
零网络零 LLM。
"""
import copy
import os
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trust_gate  # noqa: E402

from _helpers import PREDICATES, UNLOCK_STATE, bundle  # noqa: E402


def dump_yaml(obj) -> str:
    """落盘临时 YAML 供 load_predicates 走完整加载路径（测试自清理）。"""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(obj, f, allow_unicode=True, sort_keys=False)
    return path


class TestPredicatesSchema(unittest.TestCase):
    def setUp(self):
        self.doc = trust_gate.load_predicates(PREDICATES)

    def _assert_bad(self, mutate):
        bad = copy.deepcopy(self.doc)
        mutate(bad)
        path = dump_yaml(bad)
        try:
            with self.assertRaises(trust_gate.TrustGateError):
                trust_gate.load_predicates(path)
        finally:
            os.unlink(path)

    def test_valid_default_file_loads(self):
        """真源 predicates.yaml 可加载且三域齐全（宪法 §5 前三解锁域固定）。"""
        self.assertEqual(self.doc["schema"], "trust-gate-predicates/v1")
        self.assertEqual(set(self.doc["domains"]),
                         {"docs-only", "test-only", "failing-repro-fix"})
        self.assertGreaterEqual(self.doc["defaults"]["min_consecutive_agreement"], 50)
        self.assertGreaterEqual(self.doc["defaults"]["min_trap_ratio"], 0.10)
        self.assertAlmostEqual(self.doc["defaults"]["post_unlock_sample_rate"], 0.05)

    def test_hardcoded_exclusion_immutable(self):
        """宪法 §5 明确排除四域硬编码在表——删任何一项=策略表非法（测试钉死）。"""
        self.assertEqual(
            trust_gate.HARDCODED_EXCLUDED,
            {"new-feature", "dependency-upgrade", "public-api-schema", "ci-workflows"})
        self._assert_bad(lambda b: b["excluded_domains"].pop())

    def test_domain_in_both_tables_rejected(self):
        """同一域同时出现在可解锁表与排除表=语义矛盾 → 拒绝。"""
        self._assert_bad(lambda b: b["excluded_domains"].append("docs-only"))

    def test_unknown_top_key_rejected(self):
        """spec v3 幽灵键必须被拒（讽刺性负控制：risk-score 不得借尸还魂）。"""
        self._assert_bad(lambda b: b.update(risk_score_ceiling=40))

    def test_bad_threshold_type_rejected(self):
        self._assert_bad(lambda b: b["defaults"].update(min_consecutive_agreement="fifty"))

    def test_empty_requires_rejected(self):
        self._assert_bad(lambda b: b["domains"]["docs-only"].update(requires=[]))


class TestUnlockStateSchema(unittest.TestCase):
    def setUp(self):
        self.p = trust_gate.load_predicates(PREDICATES)
        self.state = trust_gate.load_unlock_state(UNLOCK_STATE, self.p)

    def test_initial_state_all_locked(self):
        """初始（shadow 期起点）：三域全部 locked。"""
        self.assertEqual(self.state,
                         {"docs-only": "locked", "test-only": "locked",
                          "failing-repro-fix": "locked"})

    def _assert_bad_state(self, text):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        try:
            with self.assertRaises(trust_gate.TrustGateError):
                trust_gate.load_unlock_state(path, self.p)
        finally:
            os.unlink(path)

    def test_domain_set_drift_rejected(self):
        """unlock-state 域集合与 predicates 不一致（漂移）→ 拒绝。"""
        self._assert_bad_state(
            "schema: trust-gate-domains/v1\nupdated: \"2026-08-22\"\ndomains:\n"
            "  docs-only:\n    status: locked\n  new-domain:\n    status: locked\n")

    def test_bad_status_rejected(self):
        self._assert_bad_state(
            "schema: trust-gate-domains/v1\nupdated: \"2026-08-22\"\n"
            "domains:\n  docs-only:\n    status: maybe\n")


class TestBundleValidation(unittest.TestCase):
    def test_valid_bundle_passes(self):
        trust_gate.validate_bundle(bundle())

    def test_missing_required_key_rejected(self):
        b = bundle()
        del b["breaker_tripped"]
        with self.assertRaises(trust_gate.TrustGateError):
            trust_gate.validate_bundle(b)

    def test_non_bool_evidence_rejected(self):
        """证据值必须是布尔——risk-score 的教训：不许「半信半疑」的数值证据。"""
        b = bundle()
        b["evidence"]["diff.ast-equivalence.proven"] = 0.8  # 分数形态=拒绝
        with self.assertRaises(trust_gate.TrustGateError):
            trust_gate.validate_bundle(b)

    def test_non_int_pr_rejected(self):
        b = bundle(pr="42")
        with self.assertRaises(trust_gate.TrustGateError):
            trust_gate.validate_bundle(b)


if __name__ == "__main__":
    unittest.main()
