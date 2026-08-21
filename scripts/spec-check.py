#!/usr/bin/env python3
"""spec-check.py —— g010 过渡版（IR-0001 W0-C4 / ADR-0050）

spec.md 的结构 + 注入双扫（W1-C1 交付正规 spec.schema.json 与 g010 后由其接管）：
  1. frontmatter 可解析且必备键齐全（taskId/specVersion/title/irRef/
     acceptanceCriteria/blastRadius/nonGoals）
  2. 每条 AC：id 唯一、given/when/then 非空
  3. blastRadius 非空
  4. 无实现细节关键词（代码围栏/函数定义/依赖安装命令）
  5. 注入扫描（INV-10 / AC-12）：豁免/放宽/跳过关卡类条款出现即 fail

用法: python3 spec-check.py <spec.md>
退出码: 0 通过 / 1 拒绝（stderr 逐条列原因）
"""
import re
import sys

import yaml  # runner 预装 PyYAML；CI 的 yaml 全量解析同依赖

IMPL_PATTERNS = [
    (r"```", "代码围栏（spec 不含实现代码）"),
    (r"\bdef\s+\w+\(", "函数定义"),
    (r"\bfunction\s+\w+\(", "函数定义"),
    (r"\bclass\s+\w+[:(]", "类定义"),
    (r"\bnpm\s+(install|add|i)\b", "依赖安装命令"),
    (r"\bpip\s+install\b", "依赖安装命令"),
    (r"\bimport\s+[\w.]+\s+from\b", "import 语句"),
]
INJ_PATTERNS = [
    # 豁免动词 × 关卡名词的邻近组合（方向：为本次工作豁免关卡）
    (r"(豁免|免除|跳过|忽略|放宽|绕过|不加|移除)[^。\n]{0,24}(关卡|门禁|gate|检查|校验|测试|g\d{3}|zizmor|lint|review)", None),
    (r"(关卡|门禁|gate|检查|校验|测试)[^。\n]{0,16}(可以|允许|不用|不必|免)(跳过|豁免|通过|执行)?", None),
    (r"(?i)disable[d]?\s*g\d+|bypass\s+(the\s+)?gate|skip\s+(the\s+)?gate", None),
    (r"ceiling[^。\n]{0,20}(9999|999\b|无限|infinity)", None),
]
REQUIRED_KEYS = ["taskId", "specVersion", "title", "irRef",
                 "acceptanceCriteria", "blastRadius", "nonGoals"]


def fail(msgs):
    for m in msgs:
        print(f"REJECT: {m}", file=sys.stderr)
    sys.exit(1)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    if not path:
        print("用法: spec-check.py <spec.md>", file=sys.stderr)
        sys.exit(2)
    text = open(path, encoding="utf-8").read()

    if not text.startswith("---\n"):
        fail(["缺 YAML frontmatter（必须以 --- 开头）"])
    parts = text[4:].split("\n---", 1)
    if len(parts) < 2:
        fail(["frontmatter 未闭合（缺结束 ---）"])
    try:
        fm = yaml.safe_load(parts[0])
    except yaml.YAMLError as e:
        fail([f"frontmatter YAML 解析失败: {e}"])
    errs = []

    # 1. 必备键（nonGoals 允许空列表——"无非目标"是合法态；其余为空即拒绝）
    for k in REQUIRED_KEYS:
        if k not in fm:
            errs.append(f"frontmatter 缺必备键: {k}")
        elif k == "nonGoals":
            continue
        elif fm[k] in (None, "", []):
            errs.append(f"frontmatter 键为空: {k}")
    if errs:
        fail(errs)

    # 2. AC 结构
    acs = fm["acceptanceCriteria"]
    if not isinstance(acs, list) or not acs:
        fail(["acceptanceCriteria 必须是非空列表"])
    ids = set()
    for ac in acs:
        if not isinstance(ac, dict):
            errs.append(f"AC 条目必须是对象: {ac!r}")
            continue
        for f in ("id", "given", "when", "then"):
            v = ac.get(f)
            if not isinstance(v, str) or not v.strip():
                errs.append(f"AC {ac.get('id', '?')} 字段 {f} 缺失或为空")
        if ac.get("id") in ids:
            errs.append(f"AC id 重复: {ac.get('id')}")
        ids.add(ac.get("id"))

    # 3. blastRadius 元素非空
    if not all(isinstance(x, str) and x.strip() for x in fm["blastRadius"]):
        errs.append("blastRadius 含空元素")

    # 4. 实现细节关键词
    for pat, why in IMPL_PATTERNS:
        if re.search(pat, text):
            errs.append(f"含实现细节（{why}）——spec 只描述是什么/验收什么")

    # 5. 注入扫描（只扫 spec 正文语义——模式命中即报，宁枉勿纵；
    #    例证引述豁免：命中段被引号（"…"/'…'/“…”)包裹视为对注入样例的
    #    引用（如 IR-0001 AC-12 原文），不算注入条款）
    for pat, _ in INJ_PATTERNS:
        for m in re.finditer(pat, text):
            seg = text[max(0, m.start() - 40):m.end() + 40]
            # 豁免两类合法引述：(a) 引号内的注入样例引用；(b) 否定语境
            # （"不含/禁止/不得出现…豁免条款"类防线描述——前 8 字符含否定词）
            neg_prefix = re.search(r"(不含|不得|不能|不会|禁止|没有|拒绝|无视)",
                                   text[max(0, m.start() - 8):m.start()])
            if neg_prefix or any(q in seg for q in ('"', '"', "'")):
                continue
            ctx = seg.replace("\n", " ")
            errs.append(f"注入条款嫌疑（INV-10/AC-12）: …{ctx}…")
            break

    if errs:
        fail(errs)
    print(f"OK spec-check（g010 过渡版）: taskId={fm['taskId']} AC×{len(acs)} "
          f"blastRadius×{len(fm['blastRadius'])} 结构/注入双扫通过")


if __name__ == "__main__":
    main()
