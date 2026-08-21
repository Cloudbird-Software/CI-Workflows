# -*- coding: utf-8 -*-
"""run_exam 自测试（W5-C3 .github#226 / ADR-0072；零真实 LLM——全部回放）。

覆盖卡面自测清单：金丝雀 100% 判负（全条断言）；双序一致率边界（0.89 拒/0.90 过）；
冻结校验 fail-closed；成绩存档键 judge_id@版本@prompt_hash 完整性；prompt 改动即重考。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_exam  # noqa: E402
import examutil as U  # noqa: E402

REAL_EXAM = U.HERE / "examset" / "v1"
REAL_ITEMS = run_exam.load_items(REAL_EXAM)


class TestFreeze(unittest.TestCase):
    def test_vendored_freeze_verifies(self):
        m = run_exam.load_and_verify(REAL_EXAM)   # 钉版副本自洽（不抛即过）
        self.assertEqual(m["version"], "1.0.0")

    def test_tamper_rejected_exit_2(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            exam = U.synth_exam(tmp)
            cfg = U.judge_config(tmp)
            fx = U.write_json(tmp / "fx.json", U.gold_fixture(run_exam.load_items(exam)))
            rc, _ = U.run_main(exam, cfg, fx, tmp / "out")
            self.assertEqual(rc, 0)
            # 篡改：向一个 jsonl 追加一字节噪声 → 复算哈希破裂 → exit 2（不许开考）
            p = exam / "null-canaries.jsonl"
            p.write_bytes(p.read_bytes() + b" \n")
            rc, _ = U.run_main(exam, cfg, fx, tmp / "out")
            self.assertEqual(rc, 2)

    def test_manifest_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            exam = U.synth_exam(Path(td))
            mp = exam / "manifest.json"
            m = json.loads(mp.read_text(encoding="utf-8"))
            m["freeze_hash"] = "0" * 64   # 声明与实际不符（漂移注入）
            mp.write_text(json.dumps(m), encoding="utf-8")
            with self.assertRaises(run_exam.ExamError):
                run_exam.load_and_verify(exam)


class TestCanaries100Negative(unittest.TestCase):
    """AC-2：金丝雀 100% 判负——真实冻结集 24 条全断言 + 单点翻转即拒。"""

    def test_every_real_canary_judged_negative(self):
        judge = run_exam.ReplayJudge(json.loads(
            (U.HERE / "fixtures" / "judge-gold.json").read_text(encoding="utf-8")))
        canaries = REAL_ITEMS["null-canaries"]
        self.assertGreaterEqual(len(canaries), 20)   # 卡面：≥20 null-model 金丝雀
        for it in canaries:   # 逐条断言（24/24）
            self.assertEqual(judge.judge_canary(it), "negative", f"{it['id']} 未判负")

    def test_real_set_gold_fixture_full_pass(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rc, _ = U.run_main(REAL_EXAM, U.judge_config(tmp, "gold-ci"),
                               U.HERE / "fixtures" / "judge-gold.json", tmp / "out", run_id="ci")
            self.assertEqual(rc, 0)
            rec = U.load_record(tmp / "out")
            self.assertTrue(rec["overall_pass"])
            self.assertEqual(rec["sections"]["null_canary"]["negative_rate"], 1.0)
            self.assertEqual(rec["sections"]["null_canary"]["n"], 24)

    def test_one_positive_canary_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rc, _ = U.run_main(REAL_EXAM, U.judge_config(tmp, "canary-miss"),
                               U.HERE / "fixtures" / "judge-canary-miss.json", tmp / "out")
            self.assertEqual(rc, 1)   # 23/24=0.958<1.00 → 拒上岗
            rec = U.load_record(tmp / "out")
            self.assertFalse(rec["overall_pass"])
            self.assertFalse(rec["sections"]["null_canary"]["pass"])
            self.assertTrue(rec["sections"]["dual_order"]["pass"])   # 其余分项不受累及


class TestDualOrderBoundary(unittest.TestCase):
    """双序一致率边界：0.89 拒 / 0.90 过（阈值真源=exam-policy.yaml，不硬编码）。"""

    def _gate(self, agreement):
        sections = {"rewardbench2_generative": {"accuracy": 1.0},
                    "llmbar_adversarial": {"accuracy": 1.0},
                    "null_canary": {"negative_rate": 1.0},
                    "dual_order": {"agreement": agreement}}
        import yaml
        policy = yaml.safe_load((U.HERE / "exam-policy.yaml").read_text(encoding="utf-8"))
        return run_exam.gate(sections, policy), sections

    def test_boundary_089_rejected_090_passed(self):
        self.assertFalse(self._gate(0.89)[0])
        self.assertTrue(self._gate(0.90)[0])

    def test_integration_100_items_090_pass(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            exam = U.synth_exam(tmp, n_rb2=50, n_llm=50)   # 100 成对条目
            items = run_exam.load_items(exam)
            fx = U.write_json(tmp / "fx90.json", U.gold_fixture(items, swap_flips=10))
            rc, _ = U.run_main(exam, U.judge_config(tmp), fx, tmp / "o90")
            rec = U.load_record(tmp / "o90")
            self.assertEqual(rec["sections"]["dual_order"]["agreement"], 0.90)
            self.assertTrue(rec["overall_pass"])
            self.assertEqual(rc, 0)

    def test_integration_100_items_089_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            exam = U.synth_exam(tmp, n_rb2=50, n_llm=50)
            items = run_exam.load_items(exam)
            fx = U.write_json(tmp / "fx89.json", U.gold_fixture(items, swap_flips=11))
            rc, _ = U.run_main(exam, U.judge_config(tmp), fx, tmp / "o89")
            rec = U.load_record(tmp / "o89")
            self.assertEqual(rec["sections"]["dual_order"]["agreement"], 0.89)
            self.assertFalse(rec["overall_pass"])
            self.assertEqual(rc, 1)

    def test_positional_bias_zero_agreement(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rc, _ = U.run_main(REAL_EXAM, U.judge_config(tmp, "positional"),
                               U.HERE / "fixtures" / "judge-positional.json", tmp / "out")
            self.assertEqual(rc, 1)
            rec = U.load_record(tmp / "out")
            self.assertEqual(rec["sections"]["dual_order"]["agreement"], 0.0)


class TestArchiveKeyIntegrity(unittest.TestCase):
    """成绩存档键 judge_id@exam_version@prompt_hash 完整性（AC-1）。"""

    def test_key_format_and_locked_fields(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rc, _ = U.run_main(REAL_EXAM, U.judge_config(tmp, "key-check"),
                               U.HERE / "fixtures" / "judge-gold.json", tmp / "out")
            rec = U.load_record(tmp / "out")
            ph = run_exam.compute_prompt_hash(U.HERE / "prompts", "v1")
            self.assertEqual(rec["prompt_hash"], ph)
            self.assertEqual(rec["archive_key"], f"key-check@1.0.0@{ph[:12]}")
            self.assertEqual(rec["frozen_exam_sha256"],
                             json.loads((REAL_EXAM / "manifest.json").read_text(encoding="utf-8"))["freeze_hash"])
            self.assertEqual(rec["judge_mode"], "replay")
            self.assertEqual(rec["sampling"]["seed"], 7)   # 采样参数全锁定记录
            self.assertEqual(rc, 0)

    def test_sampling_change_same_key_prompt_change_new_key(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            out1, out2, out3 = tmp / "a", tmp / "b", tmp / "c"
            U.run_main(REAL_EXAM, U.judge_config(tmp, "stable"), U.HERE / "fixtures" / "judge-gold.json", out1)
            # 采样参数变化：键不变（键=模型/prompt 语义），但锁定记录反映新参数
            U.run_main(REAL_EXAM, U.judge_config(tmp, "stable", temperature=0.3, seed=99),
                       U.HERE / "fixtures" / "judge-gold.json", out2)
            r1, r2 = U.load_record(out1), U.load_record(out2)
            self.assertEqual(r1["archive_key"], r2["archive_key"])
            self.assertNotEqual(r1["sampling"], r2["sampling"])
            # prompt 改动：prompt_hash 变 → 键变（prompt 改动即重考，ADR-0072 决策 2）
            rc, _ = U.run_main(REAL_EXAM, U.judge_config(tmp, "stable"),
                               U.HERE / "fixtures" / "judge-gold.json", out3,
                               prompts=U.prompts_dir(tmp, mutate=True))
            r3 = U.load_record(out3)
            self.assertEqual(rc, 0)
            self.assertNotEqual(r3["archive_key"], r1["archive_key"])
            self.assertNotEqual(r3["prompt_hash"], r1["prompt_hash"])

    def test_same_key_rerun_appends_history(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for rid in ("run-1", "run-2"):
                U.run_main(REAL_EXAM, U.judge_config(tmp, "hist"),
                           U.HERE / "fixtures" / "judge-gold.json", tmp / "out", run_id=rid)
            path = list((tmp / "out").glob("*.jsonl"))[0]
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)   # 同键多次考试=追加（历史可追溯）
            self.assertEqual({json.loads(l)["run_id"] for l in lines}, {"run-1", "run-2"})


class TestVerdictParsing(unittest.TestCase):
    def test_pairwise_and_canary_protocol(self):
        self.assertEqual(run_exam.parse_verdict("前言……\nVERDICT: response0", "pairwise"), "response0")
        self.assertEqual(run_exam.parse_verdict("VERDICT: response1\n", "pairwise"), "response1")
        self.assertEqual(run_exam.parse_verdict("答非所问", "pairwise"), "unparseable")
        self.assertEqual(run_exam.parse_verdict("VERDICT: negative", "canary"), "negative")
        self.assertEqual(run_exam.parse_verdict("VERDICT: POSITIVE", "canary"), "positive")
        self.assertEqual(run_exam.parse_verdict("", "canary"), "unparseable")

    def test_gold_fixture_never_sees_labels(self):
        # 输入隔离佐证：判官接口只接受 prompt/responses——fixture 键=id，值不含 label
        fx = json.loads((U.HERE / "fixtures" / "judge-gold.json").read_text(encoding="utf-8"))
        for table in (fx["pairwise"], fx["canary"]):
            for v in table.values():
                self.assertIn(v, ("resp0", "resp1", "negative"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
