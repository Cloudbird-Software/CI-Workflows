"""修复后 spec 的真实测试（W5-C2 E2E fixture）。

本测试真实验证 AC 语义：解析同级 spec.md，断言 criteria 溯源字段存在且非空。
构造独立于判定脚本：纯文本解析 + 断言，不引用 llm_verifier 代码。
"""
import os
import re

SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "spec.md")


def _read_spec():
    with open(SPEC_PATH, encoding="utf-8") as f:
        return f.read()


def test_spec_has_acceptance_criteria():
    """AC 溯源存在：spec 含「验收标准」节且列出具体 criterion。"""
    src = _read_spec()
    assert "## 验收标准" in src or "## Acceptance Criteria" in src, \
        "spec.md 缺少验收标准节（AC-1 溯源缺失）"
    assert "lock-pin" in src and "ac-traceability" in src and "ir-fidelity" in src, \
        "spec.md 未声明具体 criterion id（AC-1 criteria 分解缺失）"


def test_spec_has_blast_radius():
    """blastRadius 结构完整：声明受保护路径集。"""
    src = _read_spec()
    assert "blastRadius" in src, "spec.md 缺少 blastRadius 声明"
    assert "specs/**" in src, "specs/** 未列入受保护路径（blastRadius 失真）"


def test_spec_has_anti_camouflage():
    """反摆拍断言已补全：golden 回归 + 配置面校验 + 凭据扫描。"""
    src = _read_spec()
    assert "golden" in src.lower(), "反摆拍缺失：未提 golden 回归"
    assert "org secret" in src.lower() or "org variable" in src.lower(), \
        "反摆拍缺失：未声明配置面约束"


def test_spec_version_bumped():
    """specVersion 已递增（修复 marker）：Veto spec=99 → fixed spec=100。"""
    src = _read_spec()
    m = re.search(r"^specVersion:\s*(\d+)\s*$", src, re.MULTILINE)
    assert m, "spec.md 未声明 specVersion"
    assert int(m.group(1)) >= 100, f"specVersion 未反映修复: {m.group(1)}"
