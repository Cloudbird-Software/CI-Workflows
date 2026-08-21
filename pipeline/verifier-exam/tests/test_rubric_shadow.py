# -*- coding: utf-8 -*-
"""rubric_shadow 自测试（AC-4；ADR-0072 决策 5；零真实 LLM——回放打分）。

覆盖：shadow 出分仅记录不阻断（含全零极端分仍 exit 0）；标注负债显式申报
（数据不足维度不打分、记 annotation_debt）；rubric 契约完整性（五维×五档锚定）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rubric_shadow as R  # noqa: E402
import examutil as U  # noqa: E402

RUBRIC = U.HERE / "rubrics" / "ai-readability-v1.yaml"


def synth_repo(tmp: Path) -> Path:
    root = tmp / "repo"
    root.mkdir()
    (root / "README.md").write_text(
        "# synth\n入口见 pipeline/demo/run.py 与 scripts/nope.sh\n", encoding="utf-8")
    (root / "pipeline").mkdir()
    (root / "pipeline" / "demo").mkdir()
    (root / "pipeline" / "demo" / "run.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pipeline" / "demo" / "helper_mod.py").write_text("y = 2\n", encoding="utf-8")
    return root


def run_shadow(tmp, fixture: dict, repo=None):
    fx = U.write_json(tmp / "scores.json", fixture)
    argv = ["--repo-root", str(repo or synth_repo(tmp)), "--rubric", str(RUBRIC),
            "--scores-fixture", str(fx), "--out", str(tmp / "rec.jsonl"), "--run-id", "t"]
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = R.main(argv)
    return rc, json.loads((tmp / "rec.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])


class TestRubricShadow(unittest.TestCase):
    def test_rubric_contract_five_dims_five_anchors(self):
        rubric = yaml.safe_load(RUBRIC.read_text(encoding="utf-8"))
        self.assertEqual([d["id"] for d in rubric["dimensions"]],
                         list(R.DIMENSIONS))
        for d in rubric["dimensions"]:
            self.assertEqual(len(d["anchors"]), 5, f"{d['id']} 锚档非五档")
            self.assertEqual(sorted(d["anchors"]), ["0.0", "0.25", "0.5", "0.75", "1.0"])
        self.assertIn("annotation_debt_policy", rubric)

    def test_scores_recorded_not_blocking_with_debt(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rc, rec = run_shadow(tmp, {"fixture_id": "t1", "locatability": 0.5,
                                       "entry_clarity": 0.75, "module_depth": 0.5,
                                       "naming_vocabulary": 0.75})   # 缺 example_freshness
            self.assertEqual(rc, 0)   # shadow：记录路径恒 0
            self.assertFalse(rec["blocking"])   # AC-4：仅记录不阻断
            self.assertEqual(rec["scored_dimensions"], 4)
            self.assertIsNone(rec["dimensions"]["example_freshness"]["score"])
            debts = {d["dimension"] for d in rec["annotation_debt"]}
            self.assertIn("example_freshness", debts)   # 数据不足=显式负债申报
            self.assertIn("locatability", debts)        # 合成仓无 AGENTS.md
            self.assertEqual(rec["annotation_debt_count"], len(rec["annotation_debt"]))
            for d in rec["annotation_debt"]:
                self.assertEqual(d["status"], "insufficient-data")
                self.assertTrue(d["reason"])

    def test_all_zero_scores_still_exit_zero(self):
        # 不阻断断言的极端侧：全零分（最差仓库画像）依然只记录——shadow 无 veto 面
        with tempfile.TemporaryDirectory() as td:
            rc, rec = run_shadow(Path(td), {"fixture_id": "worst", **{d: 0.0 for d in R.DIMENSIONS}})
            self.assertEqual(rc, 0)
            self.assertFalse(rec["blocking"])
            self.assertEqual(rec["dimensional_mean"], 0.0)

    def test_context_entry_clarity_probe(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ctx = R.build_context(synth_repo(tmp))
            # README 宣称 pipeline/demo/run.py（存在）与 scripts/nope.sh（不存在）
            self.assertEqual(ctx["entry_clarity"]["resolved_paths"], 1)
            self.assertEqual(ctx["entry_clarity"]["claimed_paths"], 2)
            self.assertIn("scripts/nope.sh", ctx["entry_clarity"]["sample_unresolved"])
            self.assertEqual(ctx["naming_vocabulary"]["python_files"], 2)
            self.assertEqual(ctx["naming_vocabulary"]["snake_case"], 2)

    def test_bad_scores_out_of_range_treated_as_no_data(self):
        with tempfile.TemporaryDirectory() as td:
            rc, rec = run_shadow(Path(td), {"fixture_id": "bad", "locatability": 7.5,
                                            "module_depth": "high"})
            self.assertEqual(rc, 0)
            self.assertIsNone(rec["dimensions"]["locatability"]["score"])   # 非法值=无数据（负债）

    def test_rubric_dimension_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bad = yaml.safe_load(RUBRIC.read_text(encoding="utf-8"))
            bad["dimensions"] = bad["dimensions"][:3]   # 契约破坏注入
            bp = tmp / "bad-rubric.yaml"
            bp.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
            argv = ["--repo-root", str(synth_repo(tmp)), "--rubric", str(bp),
                    "--scores-fixture", str(U.write_json(tmp / "s.json", {})),
                    "--out", str(tmp / "r.jsonl")]
            self.assertEqual(R.main(argv), 2)   # 配置失败=显式红（fail-visible）


if __name__ == "__main__":
    unittest.main(verbosity=2)
