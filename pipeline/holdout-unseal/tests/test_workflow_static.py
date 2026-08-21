#!/usr/bin/env python3
# test_workflow_static.py —— 自指审计（W4-C3 AC-2）：holdout-unseal.yml 的输出面
# 必须全部过白名单注记——workflow 变更引入未注记的 echo/cat/…时本测试红。
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WF = HERE.parents[2] / ".github" / "workflows" / "holdout-unseal.yml"


class TestWorkflowStatic(unittest.TestCase):
    def test_self_workflow_output_surface_clean(self):
        self.assertTrue(WF.is_file(), f"{WF} 缺失——本测试随 workflow PR 引入")
        p = subprocess.run([sys.executable, str(HERE.parent / "audit_outputs.py"),
                            "static", "--workflow", str(WF)],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("静态审计干净", p.stdout)


if __name__ == "__main__":
    unittest.main()
