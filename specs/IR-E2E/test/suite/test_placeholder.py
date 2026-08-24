"""故意极差 spec 的占位测试（W5-C2 E2E fixture）。

本测试文件仅用于满足 T5 suite-ready 谓词（suite/ 存在 + 非空 + ast.parse 可解析）；
其断言恒不验证任何真实 AC 语义 → 红队 judge-deep 档将判定「套件不充分」。
构造独立于判定脚本：纯占位断言，不引用任何实现。
"""


def test_placeholder_passes():
    """恒真占位 —— 套件不充分的典型症状：测试从不失败。"""
    assert True


def test_ac_traceability_absent():
    """AC 溯源缺失：本测试假装覆盖 AC-1，但无任何真实断言。"""
    ac_trace_present = False  # 故意：spec.md 中 AC 无 criteria 分解
    # 不 assert ac_trace_present —— 红队套件充分性审计将捕获此「测试名不副实」
    assert True
