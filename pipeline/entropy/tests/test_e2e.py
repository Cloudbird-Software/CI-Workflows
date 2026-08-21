#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e 自测（W4-C1 AC-1/2/3）：fixture 回放全链路（零真实 LLM——回放文件经
计量 wrapper 落账，与 live 同构）。

AC-1: 含植入歧义的 spec → 运行 → 歧义定位到 INV-2；其余条款 5 路同义
      不同措辞 → 单簇不误报。
AC-2: 底噪扣减 e2e 变体（同族自分歧 B=2、跨族 3 簇 → 3−2=1 <2 不归因、
      质询不触发）；"跨族 4 簇→报"数值面见 test_judge。
AC-3: 质询输出结构化（spec 原文引用逐字命中 + 条款坐标）；轮次不超上限。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
REPLAY = os.path.join(FIX, "replay-ambiguous")
SPEC = os.path.join(FIX, "spec-ambiguous.md")


def run_entropy(spec, out_dir, replay_dir):
    """经 run.sh 全链路（bash 缺席环境跳过——CI ubuntu 必有）。"""
    bash = shutil.which("bash")
    if not bash:
        raise unittest.SkipTest("bash 不可用（判定链 CLI 形态需 bash，核心逻辑见其余用例）")
    env = dict(os.environ)
    env["GATE_METERING_DIR"] = os.path.join(out_dir, "metering")
    r = subprocess.run(
        [bash, os.path.join(REPO, "pipeline", "entropy", "run.sh"),
         "--spec", spec, "--out-dir", out_dir, "--replay-dir", replay_dir],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=REPO, env=env)
    if r.returncode != 0:
        raise AssertionError(f"run.sh rc={r.returncode}\nstdout:{r.stdout[-800:]}\nstderr:{r.stderr[-800:]}")
    return json.load(open(os.path.join(out_dir, "report.json"), encoding="utf-8"))


def provider_resp(content):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}


def lane_content(path):
    """读回放文件里的派生 JSON 内容（readings 列表）。"""
    return json.loads(json.load(open(path, encoding="utf-8"))
                      ["choices"][0]["message"]["content"])["readings"]


class TestE2EAmbiguityLocated(unittest.TestCase):
    """AC-1 e2e。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="entropy-e2e-")
        cls.report = run_entropy(SPEC, os.path.join(cls.tmp, "out"), REPLAY)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_ambiguity_located_to_clause(self):
        self.assertTrue(self.report["verdict"]["attributed"])
        self.assertEqual(self.report["verdict"]["hotspots"], ["INV-2"])
        self.assertEqual(self.report["verdict"]["per_clause"]["INV-2"]["excess"], 2)

    def test_wording_variance_not_flagged(self):
        """其余条款 5 路同义不同措辞 → 单簇（语义聚簇不误报）。"""
        for clause, v in self.report["clusters"]["per_clause"].items():
            if clause != "INV-2":
                self.assertEqual(v["clusters"], 1, f"{clause} 措辞差异被误报")

    def test_k5_family_markers_in_report(self):
        """AC-4 e2e 旁证：报告 k=5、5 族各 1 路、族标记为真。"""
        self.assertEqual(self.report["k"], 5)
        fams = [l["family"] for l in self.report["lanes"]]
        self.assertEqual(sorted(fams), sorted(set(fams)))
        self.assertTrue(all(l["family_marker"] and l["context"] == "cold"
                            for l in self.report["lanes"]))

    def test_noise_recorded(self):
        """AC-2 e2e：B 记录在报告并参与判定。"""
        self.assertEqual(self.report["noise"]["B"], 1)
        self.assertEqual(self.report["verdict"]["noise_b"], 1)

    def test_examination_structured_and_capped(self):
        """AC-3 e2e：spec 原文逐字引用 + 条款坐标；轮次 <= 上限。"""
        spec_text = open(SPEC, encoding="utf-8").read()
        ce = self.report["cross_examination"]
        self.assertGreater(len(ce["statements"]), 0)
        self.assertLessEqual(ce["rounds_used"], ce["max_rounds"])
        for s in ce["statements"]:
            self.assertEqual(s["clause"], "INV-2")
            self.assertIn(s["spec_quote"], spec_text)          # 原文引用为真
            self.assertEqual(s["clause_coordinate"]["id"], "INV-2")
            self.assertTrue(s["statement_a"] and s["statement_b"])

    def test_metering_ledger_written(self):
        """LLM 环节全程走计量 wrapper：账本有本运行 invoke 记录（INV-06）。"""
        ledger = os.path.join(self.tmp, "out", "metering")
        records = [f for f in os.listdir(ledger) if f.endswith(".jsonl")]
        self.assertTrue(records, "回放模式也须落计量账本")
        lines = sum(len(open(os.path.join(ledger, f), encoding="utf-8").readlines())
                    for f in records)
        self.assertGreaterEqual(lines, 14)   # 5 派生 + 3 底噪 + 6 质询


class TestE2ENoiseFloorSuppresses(unittest.TestCase):
    """AC-2 e2e：同族自分歧 B=2 → 跨族 3 簇被扣减至 1 → 不归因、不质询。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="entropy-noise-")
        replay = os.path.join(cls.tmp, "replay")
        os.makedirs(replay)
        for i in range(1, 6):   # 跨族派生复用主 fixture（INV-2 = 3 簇）
            shutil.copy(os.path.join(REPLAY, f"derive-lane{i}.json"), replay)
        # 底噪重采样构造为同族自分歧：s1=读法A、s2=读法B、s3=读法A'（改写）
        # → INV-2 自簇 2（B=2），其余条款单簇
        for s, lane in ((1, 1), (2, 3), (3, 2)):
            content = json.dumps({"readings": lane_content(
                os.path.join(REPLAY, f"derive-lane{lane}.json"))}, ensure_ascii=False)
            with open(os.path.join(replay, f"noise-sample{s}.json"), "w",
                      encoding="utf-8", newline="\n") as f:
                json.dump(provider_resp(content), f, ensure_ascii=False,
                          separators=(",", ":"))
                f.write("\n")
        cls.report = run_entropy(SPEC, os.path.join(cls.tmp, "out"), replay)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_not_attributed_after_subtraction(self):
        self.assertEqual(self.report["noise"]["B"], 2)
        self.assertEqual(self.report["clusters"]["per_clause"]["INV-2"]["clusters"], 3)
        self.assertFalse(self.report["verdict"]["attributed"])
        self.assertEqual(self.report["verdict"]["per_clause"]["INV-2"]["excess"], 1)

    def test_examination_not_triggered(self):
        self.assertEqual(self.report["cross_examination"]["rounds_used"], 0)
        self.assertEqual(self.report["cross_examination"]["statements"], [])


class TestReportSchema(unittest.TestCase):
    def test_schema_is_valid_json_with_required_face(self):
        schema = json.load(open(os.path.join(REPO, "pipeline", "entropy",
                                             "report.schema.json"), encoding="utf-8"))
        self.assertEqual(schema["title"], "entropy-report/v1")
        for key in ("mode", "lanes", "noise", "clusters", "verdict", "cross_examination"):
            self.assertIn(key, schema["properties"])

    def test_report_validates_against_schema_if_lib_present(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("jsonschema 未安装（CI 零依赖形态；必填面由 run.py 内置断言）")
        import jsonschema
        out = os.path.join(tempfile.mkdtemp(prefix="entropy-schema-"), "out")
        report = run_entropy(SPEC, out, REPLAY)
        schema = json.load(open(os.path.join(REPO, "pipeline", "entropy",
                                             "report.schema.json"), encoding="utf-8"))
        jsonschema.validate(report, schema)


if __name__ == "__main__":
    unittest.main()
