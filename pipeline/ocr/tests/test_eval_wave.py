#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_eval_wave.py —— W5-E2 optimization 波次自测（.github#422 / AC-10c）

基线=原规则（去掉 W5-E2 新增措辞形态），候选=rules.yaml 现状；同 harness
同语料（eval/corpus）——指标差异只归因于规则优化本体。
零网络零真实推理。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent
PY = sys.executable


def run_harness(rules: str, out: str):
    return subprocess.run(
        [PY, str(HERE / "eval" / "eval_wave.py"),
         "--corpus", str(HERE / "eval" / "corpus.jsonl"),
         "--diff", str(HERE / "eval" / "corpus.diff"),
         "--rules", rules, "--out", out],
        capture_output=True, text=True, encoding="utf-8")


class TestEvalWave(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="eval-wave-test-")
        # 基线规则=候选规则去掉 W5-E2 新增的措辞形态 regex（机械构造，防漂移）
        doc = yaml.safe_load((HERE / "rules.yaml").read_text(encoding="utf-8"))
        for r in doc["rules"]:
            if r["id"] == "hardcoded-secret":
                r["content_regex"] = [p for p in r["content_regex"]
                                      if "hardcoded" not in p]
        cls.base_rules = f"{cls.tmp}/baseline-rules.yaml"
        Path(cls.base_rules).write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")

    def report(self, rules, name):
        out = f"{self.tmp}/{name}.json"
        r = run_harness(rules, out)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(Path(out).read_text(encoding="utf-8"))

    def test_optimization_improves_all_declared_metrics(self):
        """W5-E2 优化本体：措辞形态规则让 hardcoded-secret 类不再全量落
        no-rule-hit——evaluated↑、drop_rate↓、precision↑（三项同向改善，
        非劣性家族自然通过）。"""
        base = self.report(self.base_rules, "base")
        cand = self.report(HERE / "rules.yaml", "cand")
        self.assertEqual(base["metrics"]["evaluated"], 3)
        self.assertEqual(cand["metrics"]["evaluated"], 4)
        self.assertEqual(base["metrics"]["drop_rate"], 0.625)
        self.assertEqual(cand["metrics"]["drop_rate"], 0.5)
        self.assertGreaterEqual(cand["metrics"]["precision"], base["metrics"]["precision"])
        # 负对照：措辞未入类的 "Token literal assigned…" 仍 no-rule-hit
        #（不因新规则放宽而误纳）——precision 不虚高
        self.assertEqual(cand["metrics"]["evaluated"] - base["metrics"]["evaluated"], 1)

    def test_report_schema_contract(self):
        """报告 schema 契约（eval-gate.py 消费面）：metrics 三指标+cost+latency+provenance。"""
        rep = self.report(HERE / "rules.yaml", "schema")
        for k in ("precision", "evaluated", "drop_rate"):
            self.assertIn(k, rep["metrics"])
        self.assertEqual(rep["cost_usd"], 0.0)
        self.assertIsInstance(rep["latency_ms"], float)
        self.assertIn("corpus@", rep["provenance"])
        self.assertIn("rules@", rep["provenance"])

    def test_corpus_without_ground_truth_fails_closed(self):
        """语料缺 fixed_later 标注 → exit 2（fail-closed：ground truth 不可缺）。"""
        bad = f"{self.tmp}/bad-corpus.jsonl"
        Path(bad).write_text(
            '{"schema":"ocr-eval-corpus/v1","record":"comment","idx":1,'
            '"path":"src/handler.py","start_line":11,"content":"SQL injection: x"}\n',
            encoding="utf-8")
        r = subprocess.run(
            [PY, str(HERE / "eval" / "eval_wave.py"),
             "--corpus", bad, "--diff", str(HERE / "eval" / "corpus.diff"),
             "--rules", str(self.base_rules), "--out", f"{self.tmp}/bad.json"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 2)
        self.assertIn("fixed_later", r.stderr)


if __name__ == "__main__":
    unittest.main()
