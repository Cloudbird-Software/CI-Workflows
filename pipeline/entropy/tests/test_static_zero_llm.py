#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态零 LLM 断言（W4-C1 AC-4）：判定链路（cluster.py/judge.py/policy.py）
import 面黑名单扫描——无 LLM SDK、无网络库、无子进程；k 路输入含族标记且
各族>=1 路。黑名单与 org scan-patterns.yaml 的 INV-06 直连模式同源，另加
subprocess/socket/urllib（判定链连网络底座都不许有）。"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import derive  # noqa: E402
import policy  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# AC-4 判定链路文件面（nli_deberta.py 是可选重引擎适配器，惰性加载、不在
# 默认 import 面——其 urllib 是 NLI 服务调用而非 LLM，scan-patterns 不命中）
VERDICT_CHAIN = ["cluster.py", "judge.py", "policy.py"]

BLACKLIST = re.compile(
    r"\b(?:openai|anthropic|google\.generativeai|requests|httpx|urllib|"
    r"aiohttp|socket|subprocess|curl|wget)\b")


def _src(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


class TestZeroLlmImportSurface(unittest.TestCase):
    def test_verdict_chain_no_llm_no_network(self):
        """判定脚本 import 面/文本面零 LLM、零网络、零子进程（静态可证）。"""
        hits = []
        for name in VERDICT_CHAIN:
            for lineno, line in enumerate(_src(name).splitlines(), 1):
                if BLACKLIST.search(line):
                    hits.append(f"{name}:{lineno}:{line.strip()}")
        self.assertEqual(hits, [], f"判定链路出现黑名单符号：{hits}")

    def test_verdict_chain_module_imports_whitelisted(self):
        """判定链模块级 import（AST 面非文本面）全部命中白名单：
        stdlib + policy。cluster.py 内 nli_deberta 为函数体惰性 import——
        不进模块 import 面（默认链路仍零网络；显式选 deberta-mnli 才加载）。"""
        import ast
        allow = {"argparse", "json", "math", "re", "sys", "unicodedata", "os", "policy"}
        for name in VERDICT_CHAIN:
            tree = ast.parse(_src(name))
            mods = set()
            # 仅模块顶层（tree.body 直属）——函数体内惰性 import 不进 import 面
            for node in tree.body:
                if isinstance(node, ast.Import):
                    mods.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    mods.add((node.module or "").split(".")[0])
            extra = mods - allow
            self.assertFalse(extra, f"{name} 模块级 import 越白名单：{extra}")

    def test_cluster_judge_not_importing_derive_or_examine(self):
        """判定链不得 import 编排/LLM 侧模块（derive/examine 含 wrapper 调用）。"""
        for name in ("cluster.py", "judge.py"):
            src = _src(name)
            self.assertNotIn("import derive", src)
            self.assertNotIn("import examine", src)


class TestLaneFamilyMarkers(unittest.TestCase):
    """AC-4 后半：k 路输入含族标记且各族>=1 路。"""

    def test_k5_families_each_at_least_one(self):
        fams = derive._families_for_k()
        self.assertEqual(len(fams), policy.K)
        self.assertEqual(len(set(fams)), len(policy.FAMILIES))   # 5 族各 1 路
        for f in policy.FAMILIES:
            self.assertGreaterEqual(fams.count(f), 1)

    def test_prompt_contains_family_marker(self):
        prompt = derive.build_prompt("spec 正文", ["INV-1"], "qwen", 3)
        self.assertIn("qwen", prompt)                       # 族标记入输入
        self.assertIn("冷上下文", prompt)                    # 冷上下文声明
        self.assertNotIn("lane 2/", prompt)                  # 互不可见（无他路残留）

    def test_validate_lanes_rejects_violations(self):
        def lanes(n, marker=True, families=None):
            families = families or [f"fam{i}" for i in range(1, n + 1)]
            return {"lanes": [{"lane": i + 1, "family": families[i],
                               "family_marker": marker} for i in range(n)]}

        with self.assertRaises(SystemExit):   # k != 5
            derive.validate_lanes(lanes(4))
        with self.assertRaises(SystemExit):   # 缺族（5 路但只覆盖 1 族）
            derive.validate_lanes(lanes(5, families=["glm"] * 5))
        with self.assertRaises(SystemExit):   # 输入缺族标记
            derive.validate_lanes(lanes(5, marker=False))
        ok = {"lanes": [{"lane": i + 1, "family": f, "family_marker": True}
                        for i, f in enumerate(policy.FAMILIES)]}
        self.assertIs(derive.validate_lanes(ok), True)      # 5 族各 1 路通过


if __name__ == "__main__":
    unittest.main()
