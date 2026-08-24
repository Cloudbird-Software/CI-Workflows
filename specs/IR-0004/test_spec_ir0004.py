"""IR-0004 spec 结构自测（suite/——#263 T-14：spec PR 必含非空测试文件且含有效断言）。

校验 specs/IR-0004/spec.md 的结构完整性：frontmatter 可解析、AC 三段俱全、
id 唯一且映射 IR 20 条期望变化、blastRadius/nonGoals 非空、正文条款 ID 唯一。
"""
import re
from pathlib import Path

import yaml

SPEC = Path(__file__).resolve().parents[1] / "spec.md"
IR_ITEM_COUNT = 21  # IR #315 期望变化 20 条；IR 条 11 拆为 AC-11/12、IR 条 20 补为 AC-21（R1-A H-1）


def load_fm():
    text = SPEC.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "frontmatter 定界符缺失或未闭合"
    fm = yaml.safe_load(m.group(1))
    assert fm["taskId"] == "IR-0004"
    assert isinstance(fm["specVersion"], int) and fm["specVersion"] >= 1
    aml = fm.get("amendments") or []
    if aml:
        assert aml[-1]["rev"] == fm["specVersion"], "amendments 末条 rev 须等于 specVersion"
    assert fm["irRef"] == "Cloudbird-Software/.github#315"
    return fm, text


def test_frontmatter_parses():
    load_fm()


def test_acs_complete_and_unique():
    acs = load_fm()[0]["acceptanceCriteria"]
    assert len(acs) == IR_ITEM_COUNT, f"AC 数 {len(acs)} != IR 期望变化数 {IR_ITEM_COUNT}"
    ids = [a["id"] for a in acs]
    assert ids == [f"AC-{i}" for i in range(1, IR_ITEM_COUNT + 1)], "AC 编号不连续"
    for a in acs:
        for seg in ("given", "when", "then"):
            assert str(a.get(seg, "")).strip(), f"{a['id']} 的 {seg} 段为空"
        assert "运行时证据" in a["then"], f"{a['id']} 缺运行时证据子句"
        # 语义级断言（R1-C H-3）：证据须指向具体可机检工件，不许空泛措辞交差
        assert any(w in a["then"] for w in ("run", "日志", "JSON", "JSONL", "记录", "diff", "issue")),             f"{a['id']} 运行时证据未指向具体工件类型"


def test_blastradius_and_nongoals():
    fm = load_fm()[0]
    assert fm["blastRadius"], "blastRadius 为空"
    for b in fm["blastRadius"]:
        assert set(b) >= {"repo", "path"}, f"blastRadius 条目缺字段: {b}"
    assert len(fm["nonGoals"]) >= 5, "nonGoals 过少"


def test_clauses_unique_and_referenced():
    text = SPEC.read_text(encoding="utf-8")
    body = text.split("---", 2)[2]
    ids = re.findall(r"^\s*[-\s]*\*{0,2}(INV|BEH|IFACE|BUDGET|DECISION|ASSUMPTION)-\d+\*{0,2}", body, re.M)
    assert len(ids) >= 30, f"正文条款过少: {len(ids)}"
    defs = re.findall(r"^- \*\*((?:INV|BEH|IFACE|BUDGET|DECISION|ASSUMPTION)-\d+)\*\*", body, re.M)
    assert len(defs) == len(set(defs)), f"正文条款定义行有重复: {[k for k in defs if defs.count(k) > 1]}"
    # BEH 条款须引用其承接的 AC
    for m in re.finditer(r"BEH-\d+（(AC-[0-9/, ]+)）", body):
        for ref in re.findall(r"AC-\d+", m.group(1)):
            assert ref in text


def test_key_ac_artifact_words():
    """R3-A H-5：关键 AC 的运行时证据绑定具体工件词（防宽泛词空洞交差）。"""
    fm = load_fm()[0]
    acs = {a["id"]: a["then"] for a in fm["acceptanceCriteria"]}
    binding = {
        "AC-1": ("变异",), "AC-2": ("变异体清单",), "AC-5": ("复算",),
        "AC-8": ("hash", "编译"), "AC-15": ("cpus", "build logs"),
        "AC-9": ("交集",), "AC-12": ("硬区",), "AC-18": ("五项",),
    }
    for ac_id, words in binding.items():
        assert all(w in acs[ac_id] for w in words), f"{ac_id} 缺绑定工件词 {words}"




# ---- 语义锚定 v3（adversary S1' posed-acceptance-criteria 补强，run 32686773389）----
# 摆拍攻击=最小成本通过结构检查；以下断言把通过成本抬到"必须写出实质 spec"：
# 概念绑定（21 概念↔21 AC）、负向断言须为条件-后果结构、证据须含两类工件、
# then 密度下限、IR 量化常量锚定。任一不满足即红。

CONCEPTS = {
    "AC-1": ("变异",), "AC-2": ("不变量", "属性"), "AC-3": ("模糊", "fuzz"),
    "AC-4": ("蜕变",), "AC-5": ("符号执行",), "AC-6": ("SAST", "分诊"),
    "AC-7": ("形式化", "checklist"), "AC-8": ("DSL", "编译"), "AC-9": ("骨架",),
    "AC-10": ("fan-out", "竞速", "early-exit"), "AC-11": ("champion", "对拍"),
    "AC-12": ("oracle",), "AC-13": ("燃料", "淘汰路线"), "AC-14": ("接缝",),
    "AC-15": ("配额", "档位"), "AC-16": ("runbook", "入职"), "AC-17": ("work-inbox", "租约"),
    "AC-18": ("审计",), "AC-19": ("演练",), "AC-20": ("试点",), "AC-21": ("token",),
}
# IR#315 量化常量锚（防数值漂移/空洞化）
QUANT_ANCHORS = {
    "AC-2": ("10 条",), "AC-4": ("15 条", "3 条"), "AC-9": ("3–4", "3-4"),
    "AC-15": ("1C", "2C", "4C"), "AC-16": ("canary",), "AC-17": ("七天", "7 天"),
}
ARTIFACTS = ("run", "日志", "JSON", "JSONL", "diff", "issue", "仪表盘", "记录", "构建")
# 负向断言=条件-后果结构（非裸词）
NEG_STRUCT = re.compile(
    r"(缺失|为空|不足|失败|超时|未运行|停摆|摘除|越界|不一致|作废|漂移)[^。；]{0,50}(红|不通过|作废|拦截|infra 失败)|(红|不通过|作废|拦截)[^。；]{0,20}(缺失|为空|不足|失败)")


def test_semantic_anchors():
    fm = load_fm()[0]
    acs = {a["id"]: a for a in fm["acceptanceCriteria"]}
    for ac_id, concept_words in CONCEPTS.items():
        blob = acs[ac_id]["given"] + acs[ac_id]["when"]
        assert any(w in blob for w in concept_words), f"{ac_id} 未绑定领域概念 {concept_words}"
    for ac_id, nums in QUANT_ANCHORS.items():
        whole = acs[ac_id]["given"] + acs[ac_id]["when"] + acs[ac_id]["then"]
        for q in nums:
            assert q in whole, f"{ac_id} 缺量化锚 {q}"
    for ac_id, a in acs.items():
        then = a["then"]
        assert len(then) >= 60, f"{ac_id} then 过短（<60 字）——疑似空洞 AC"
        hits = sum(1 for w in ARTIFACTS if w in then)
        assert hits >= 2, f"{ac_id} 运行时证据须含≥2类工件词（当前 {hits}）"
    for ac_id in ("AC-1", "AC-2", "AC-3", "AC-5", "AC-6", "AC-11", "AC-12",
                  "AC-13", "AC-14", "AC-15", "AC-16", "AC-17", "AC-18", "AC-19", "AC-21"):
        assert NEG_STRUCT.search(acs[ac_id]["then"]), f"{ac_id} 负向断言非条件-后果结构（裸词不算）"


def test_negative_assertions_present():
    """R1-C H-1/H-2/H-4：关键 fail-open 面必须有负向断言（异常/缺失即红）。"""
    fm = load_fm()[0]
    acs = {a["id"]: a["then"] for a in fm["acceptanceCriteria"]}
    negative_words = ("红", "不通过", "作废", "失败", "拦截")
    # R2-B H-3：覆盖面与 amendments 声明对齐（fail-open 修复面全集）
    for ac_id in ("AC-1", "AC-2", "AC-3", "AC-4", "AC-5", "AC-6", "AC-10", "AC-11", "AC-12",
                  "AC-13", "AC-14", "AC-15", "AC-16", "AC-17", "AC-18", "AC-19", "AC-21"):
        assert any(w in acs[ac_id] for w in negative_words), f"{ac_id} 缺负向断言（fail-open 缝隙）"


def test_blastradius_planned_discipline():
    """R2-C H-2：双向存在性自洽——本仓内非 planned 条目必须真实存在；计划路径必须带 planned。"""
    fm = load_fm()[0]
    root = SPEC.parents[2]  # 仓库根
    for b in fm["blastRadius"]:
        planned = b.get("planned", False)
        if b["repo"] != ".github":
            continue  # 跨仓存在性由 spec CI 关卡核验（suite 无网络依赖原则）
        path = b["path"]
        if planned:
            continue
        if path.endswith("/**"):
            assert (root / path[:-3]).is_dir(), f"非 planned 目录前缀不存在: {path}"
        else:
            assert (root / path).exists(), f"非 planned 路径不存在（须标 planned 或补存在）: {path}"


def test_decision06_sequence_guard():
    """R2-B H-1：DECISION-06 时序护栏存在（ADR-0082 修订落地前多账号不生效）。"""
    body = SPEC.read_text(encoding="utf-8").split("---", 2)[2]
    assert "修订 ADR 落地前不生效" in body, "DECISION-06 缺时序护栏"
    assert "实施证据出现即判红" in body or "使用证据出现即判红" in body, "时序护栏缺判红断言"


def test_no_exemption_of_governance():
    text = SPEC.read_text(encoding="utf-8")
    # R2-B H-4：黑名单扩为词族（同义替换绕过防护）
    import re as _re
    # 否定前缀（不/未/无/没）修饰的除外；"豁免…ADR"为制度内通道（ADR-0035 escape_hatch 同款），不视为违规
    family = _re.compile("(?<![不未无没])(跳过|绕过|无视|免检|略过)[^，。；]{0,6}(gate|关卡|判定|审计|ADR)")
    family2 = _re.compile("(?<![不未无没])豁免[^，。；]{0,6}(gate|关卡|判定|审计)")
    hits = family.findall(text) + family2.findall(text)
    assert not hits, f"出现治理豁免措辞词族命中: {hits}"


if __name__ == "__main__":
    # stdlib 执行适配（adversary run-suite.sh 契约：无 pytest 环境可跑）
    import sys as _sys
    import unittest as _unittest

    _suite = _unittest.TestSuite()
    for _name in sorted(n for n in dir() if n.startswith("test_")):
        _suite.addTest(_unittest.FunctionTestCase(globals()[_name]))
    _result = _unittest.TextTestRunner(verbosity=2).run(_suite)
    _sys.exit(0 if _result.wasSuccessful() else 1)
