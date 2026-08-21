#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bugflow fixture 自测（W3-C1 .github#218，ADR-0064）——零真实 LLM、零网络。

用例 ↔ AC 映射：
  T1  AC-1  base fail + fix pass → reproduced（标签 reproduced + state:reproduced）
  T2  AC-2  base pass → cannot-reproduce（不关单、不转移状态——保留人裁）
  T3  AC-3  超时 → inconclusive → 换新环境重试一次 → 仍不可判定 → needs-human
  T4  AC-1  自证日志缺失/基线不绿 → fail-closed（exit 2，无判定、无判定标签）
  T5  AC-2  同指纹二次上报 → 绕过重复 reproduce（幂等：再跑同结果）
  T6  决策2 环境指纹不匹配 → inconclusive（绝不 cannot-reproduce/reproduced）
  T7  决策4 翻转异常（两采样不一致）→ inconclusive → 重试 → needs-human
  T8  AC-4  周抽样：每周 3 单、确定性（同周两次抽样输出一致）
  T9  AC-1  哨兵绿了/无断言标记 → fail-closed（红必须被看见且是断言红）
  T10 单元  tri_verdict 纯函数补边（fix-unresolved / 无 fix 候选形态）
全部经 CLI 子进程断言（测的就是执法路径本身，不是旁路库调用）。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bugflow  # noqa: E402 （单测直用纯函数/指纹；CLI 断言走子进程）

PY = sys.executable.replace(os.sep, "/")  # 反斜杠路径会被 shlex 吃掉——统一正斜杠
BUGFLOW = os.path.join(os.path.dirname(HERE), "bugflow.py")
FIXTURES = os.path.join(HERE, "fixtures")
BODY = os.path.join(FIXTURES, "issue-body-demo.md")
# 自证覆写（本测必须快且不依赖默认全套件）：基线绿=python 直跑；哨兵=真实
# sentinel_red.py（顺带覆盖哨兵资产本体）；坏基线/坏哨兵用内联码制造。
BASE_OK = f'"{PY}" -c pass'
SENT_RED = f'"{PY}" ' + os.path.join(os.path.dirname(HERE), "sentinel", "sentinel_red.py").replace(os.sep, "/")
BASE_BAD = f'"{PY}" -c "import sys; sys.exit(1)"'
SENT_GREEN = BASE_OK  # 哨兵绿了=环境看不见红→fail-closed（T9）
REPO = "Cloudbird-Software/CI-Workflows"
SYMPTOM = "fixture 演示：case_bug 断言失败（demo 场景）"
STACK = "AssertionError: demo fixture red"
FLAP_STATE = os.path.join(FIXTURES, "scenario-flapping", ".flap-state")
CLI_ENV = {**os.environ, "PYTHONUTF8": "1"}  # 子进程中文输出稳定 utf-8（Windows 本地默认 cp936）


def run_cli(args):
    return subprocess.run([PY, BUGFLOW] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=CLI_ENV)


def repro_cmd(scenario, tmp, extra=()):
    d = os.path.join(tmp, "log")
    return run_cli(["repro", "--repo", REPO, "--issue", "9001",
                    "--body-file", BODY, "--scenario", os.path.join(FIXTURES, scenario),
                    "--label-mode", "dry-run", "--log-dir", d,
                    "--ledger", os.path.join(tmp, "verdicts.jsonl"),
                    "--baseline-cmd", BASE_OK, "--sentinel-cmd", SENT_RED] + list(extra))


class Reproduced(unittest.TestCase):
    def test_t1_base_fail_fix_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = repro_cmd("scenario-reproduced", tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("verdict=reproduced", r.stdout)
            self.assertIn("F→P", r.stdout)
            self.assertIn("add=['reproduced', 'state:reproduced']", r.stdout)   # AC-1 标签
            self.assertIn("remove=['state:bug']", r.stdout)
            v = json.load(open(os.path.join(tmp, "log", "verdict.json"), encoding="utf-8"))
            self.assertEqual(v["verdict"], "reproduced")
            self.assertEqual(v["attempts"][0]["base"], ["fail", "fail"])
            self.assertEqual(v["attempts"][0]["fix"], "pass")
            self.assertTrue(v["attempts"][0]["env_gate_ok"])
            self.assertTrue(v["attempts"][0]["env"]["image"].startswith("sha256:"))  # 双锁证据入日志
            self.assertTrue(v["attempts"][0]["env"]["lockfiles"])
            self.assertEqual(len(open(os.path.join(tmp, "verdicts.jsonl"), encoding="utf-8").readlines()), 1)


class CannotReproduce(unittest.TestCase):
    def test_t2_base_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = repro_cmd("scenario-cannot", tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("verdict=cannot-reproduce", r.stdout)
            self.assertIn("add=['cannot-reproduce']", r.stdout)
            self.assertNotIn("state:needs-human", r.stdout)   # 不转移状态
            self.assertNotIn("remove=", r.stdout.replace("remove=[]", ""))  # 不摘 state:bug——单保留


class InconclusiveRetry(unittest.TestCase):
    def test_t3_timeout_retry_needs_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = repro_cmd("scenario-inconclusive", tmp)  # manifest.timeout_s=2，用例睡 8s
            self.assertEqual(r.returncode, 0, r.stderr)
            # 首判 + 换新环境重试恰好各一次（评论里也有 verdict= 字样——只数 attempt 行）
            self.assertEqual(len([l for l in r.stdout.splitlines() if l.startswith("attempt-")]), 2)
            self.assertIn("verdict=inconclusive", r.stdout)
            self.assertIn("add=['inconclusive', 'state:needs-human']", r.stdout)  # AC-3 终点
            self.assertIn("remove=['state:bug']", r.stdout)
            v = json.load(open(os.path.join(tmp, "log", "verdict.json"), encoding="utf-8"))
            self.assertEqual(len(v["attempts"]), 2)  # 重试恰好一次
            self.assertTrue(all(a["base"] == ["timeout", "timeout"] for a in v["attempts"]))
            self.assertEqual(sorted(os.listdir(os.path.join(tmp, "log"))),
                             ["attestation-0.json", "attestation-1.json", "verdict.json"])  # 每次尝试都重新自证

    def test_t7_flapping(self):
        self.addCleanup(lambda: os.path.exists(FLAP_STATE) and os.remove(FLAP_STATE))
        if os.path.exists(FLAP_STATE):
            os.remove(FLAP_STATE)
        with tempfile.TemporaryDirectory() as tmp:
            r = repro_cmd("scenario-flapping", tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("翻转异常", r.stdout)
            self.assertIn("add=['inconclusive', 'state:needs-human']", r.stdout)
            v = json.load(open(os.path.join(tmp, "log", "verdict.json"), encoding="utf-8"))
            self.assertEqual(len(v["attempts"]), 2)  # 两轮采样都翻转 → 重试耗尽 → 人裁
            self.assertTrue(all(set(a["base"]) == {"pass", "fail"} for a in v["attempts"]))


class FailClosed(unittest.TestCase):
    def test_t4_baseline_not_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = repro_cmd("scenario-reproduced", tmp, ["--baseline-cmd", BASE_BAD])
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("FAIL-CLOSED", r.stderr)
            self.assertFalse(os.path.isfile(os.path.join(tmp, "log", "verdict.json")))  # 无判定
            self.assertNotIn("add=['reproduced'", r.stdout)  # 无判定标签

    def test_t9_sentinel_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = repro_cmd("scenario-reproduced", tmp, ["--sentinel-cmd", SENT_GREEN])
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("哨兵未红", r.stderr)

    def test_t4_attestation_log_missing(self):
        # AC-1 执法点直测：判定只认落盘自证日志——目录里没有日志必须拒绝判定
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(bugflow.AttestationError):
                bugflow.check_attestation_log(tmp, 0)
            # 伪造"基线绿"但不带哨兵节——日志不完整同样拒绝（哨兵节缺失≠无哨兵）
            fake = os.path.join(tmp, "attestation-0.json")
            open(fake, "w", encoding="utf-8").write(json.dumps({"baseline": {"rc": 0}}))
            with self.assertRaises(bugflow.AttestationError):
                bugflow.check_attestation_log(tmp, 0)


class Dedup(unittest.TestCase):
    def test_t5_same_fingerprint_bypass_idempotent(self):
        fp = bugflow.fingerprint(REPO, SYMPTOM, STACK)
        with tempfile.TemporaryDirectory() as tmp:
            corpus = os.path.join(tmp, "corpus.txt")
            open(corpus, "w", encoding="utf-8").write(
                f"早前判定评论：`bugfp:{fp} | verdict=cannot-reproduce | run=prior`\n")
            for _ in range(2):  # 幂等：二次/三次上报同指纹=同结果
                r = repro_cmd("scenario-reproduced", tmp, ["--corpus-file", corpus])
                self.assertEqual(r.returncode, 3, r.stdout)
                self.assertIn("BYPASS prior-verdict=cannot-reproduce", r.stdout)
                self.assertIn("duplicate-fingerprint", r.stdout)
            self.assertFalse(os.path.isfile(os.path.join(tmp, "log", "verdict.json")))  # 未跑重复 reproduce


class EnvGate(unittest.TestCase):
    def test_t6_env_mismatch_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = repro_cmd("scenario-envmismatch", tmp)  # base fail 但上报镜像指纹不符
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("verdict=inconclusive", r.stdout)
            self.assertNotIn("verdict=reproduced", r.stdout)          # 决策 2：不匹配≠复现
            self.assertNotIn("add=['cannot-reproduce'", r.stdout)    # 也不允许偷渡成证伪
            v = json.load(open(os.path.join(tmp, "log", "verdict.json"), encoding="utf-8"))
            self.assertFalse(v["attempts"][0]["env_gate_ok"])
            self.assertIn("image", v["attempts"][0]["detail"])


class TriVerdictUnit(unittest.TestCase):
    def test_t10_edges(self):
        f = {"status": "fail"}
        p = {"status": "pass"}
        self.assertEqual(bugflow.tri_verdict(f, f, None, True, "")[0], "reproduced")          # 无 fix 候选
        self.assertEqual(bugflow.tri_verdict(f, f, f, True, "")[0], "reproduced")             # fix fail：bug 真实
        self.assertIn("fix-unresolved", bugflow.tri_verdict(f, f, f, True, "")[1])
        self.assertEqual(bugflow.tri_verdict(p, p, p, True, "")[0], "cannot-reproduce")
        self.assertEqual(bugflow.tri_verdict(f, f, {"status": "timeout"}, True, "")[0], "inconclusive")
        self.assertEqual(bugflow.tri_verdict(f, p, None, True, "")[0], "inconclusive")        # 翻转
        self.assertEqual(bugflow.tri_verdict(f, f, None, False, "x")[0], "inconclusive")      # 环境不匹配

    def test_t5_fingerprint_stable_and_distinct(self):
        a = bugflow.fingerprint(REPO, SYMPTOM, STACK)
        self.assertEqual(a, bugflow.fingerprint(REPO, "  " + SYMPTOM + " \n", STACK))  # 空白归一
        self.assertNotEqual(a, bugflow.fingerprint("org/other", SYMPTOM, STACK))       # 仓不同→指纹不同
        self.assertNotEqual(a, bugflow.fingerprint(REPO, SYMPTOM + "!", STACK))        # 症状不同→指纹不同
        self.assertEqual(bugflow.find_prior_verdict(f"bugfp:{a} | verdict=reproduced", a), "reproduced")
        self.assertIsNone(bugflow.find_prior_verdict("bugfp:other | verdict=reproduced", a))


class SampleWeek(unittest.TestCase):
    def test_t8_three_per_week_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = os.path.join(tmp, "v.jsonl")
            with open(led, "w", encoding="utf-8") as f:
                for i in range(5):  # 2026-W34（8/17-8/23，周一始）内 5 条判定
                    f.write(json.dumps({"schema": "bugflow-verdict/v1", "ts": f"2026-08-{17 + i}T0{i}:00:00Z",
                                        "repo": REPO, "issue": str(100 + i), "fingerprint": f"bugfp:{i:064d}",
                                        "verdict": ["reproduced", "cannot-reproduce", "inconclusive"][i % 3]}) + "\n")
                f.write(json.dumps({"schema": "bugflow-verdict/v1", "ts": "2026-08-16T00:00:00Z",
                                    "repo": REPO, "issue": "99", "fingerprint": "bugfp:o",
                                    "verdict": "reproduced"}) + "\n")  # W33——不入本周总体
            out1, out2 = os.path.join(tmp, "s1.jsonl"), os.path.join(tmp, "s2.jsonl")
            for out in (out1, out2):
                r = run_cli(["sample-week", "--ledger", led, "--week", "2026-W34", "--out", out])
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("population=5", r.stdout)  # W33 那条被滤掉
                self.assertIn("sampled=3", r.stdout)     # 每周 3 单（AC-4）
            self.assertEqual(open(out1, encoding="utf-8").read(), open(out2, encoding="utf-8").read())  # 确定性
            d = json.loads(open(out1, encoding="utf-8").read())
            self.assertEqual(d["schema"], "bugflow-weekly-sample/v1")
            self.assertIsNone(d["misclosure_rate"])  # 人工复核回填位
            self.assertEqual({s["issue"] for s in d["sampled"]} <= {str(100 + i) for i in range(5)}, True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
