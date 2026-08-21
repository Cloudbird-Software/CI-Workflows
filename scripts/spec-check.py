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
    fix_out = None
    rest = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--fix" and i + 1 < len(argv):
            fix_out = argv[i + 1]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    path = rest[0] if rest else ""
    if not path:
        print("用法: spec-check.py <spec.md>", file=sys.stderr)
        sys.exit(2)
    text = open(path, encoding="utf-8").read()

    # 预处理（模型常见偏差，2026-08-21 首跑实测）：
    # a) 整体被 ```markdown 围栏包裹；b) frontmatter 前有说明文字行
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*[^\n]*\n", "", text)
    text = re.sub(r"\n?```\s*$", "", text).strip()
    if not text.startswith("---\n"):
        idx = text.find("\n---\n")
        if idx >= 0:
            text = text[idx + 1:]
        else:
            fail(["缺 YAML frontmatter（必须以 --- 开头）"])

    # 模型常见缺陷（2026-08-21 实测两轮）：漏写 frontmatter 闭合的 ---。
    # 确定性修复：无闭合 --- 时，以首个顶层 ## 节标题行为边界补上。
    if "\n---" not in text[4:]:
        m0 = re.search(r"\n## ", text[4:])
        if m0:
            text = text[:4 + m0.start()] + "\n---" + text[4 + m0.start():]
        else:
            fail(["frontmatter 未闭合（缺结束 ---，且无节标题可推断边界）"])
    try:
        fm = yaml.safe_load(text[4:].split("\n---", 1)[0])
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

    # 3. blastRadius 元素非空（字符串或 {repo,path} 对象皆可）
    ok_br = all((isinstance(x, str) and x.strip())
                or (isinstance(x, dict) and x)
                for x in fm["blastRadius"])
    if not ok_br:
        errs.append("blastRadius 含空元素")

    # 4. 实现细节关键词
    for pat, why in IMPL_PATTERNS:
        if re.search(pat, text):
            errs.append(f"含实现细节（{why}）——spec 只描述是什么/验收什么")

    # 5. 注入扫描（只扫 spec 正文语义——模式命中即报，宁枉勿纵；
    #    两类合法引述豁免：(a) 紧包裹引号——命中段前后紧邻引号（对注入样例的
    #    引用，如 IR-0001 AC-12 原文引号内的样例）；(b) 否定语境（前 8 字符含
    #    否定词——『不含/禁止…豁免条款』类防线描述）。窗口式引号判定实测过宽
    #    （frontmatter 的引号值落窗即误免）已弃用）
    QL = '\u201c'
    QR = '\u201d'
    DQ = '"'
    for pat, _ in INJ_PATTERNS:
        for m in re.finditer(pat, text):
            # 引号段豁免：span 与前后引号同处一行（样例引述"把 ceiling 改为
            # 9999"整体被包裹，span 不必紧贴引号；限定同行防跨行误免）
            line_start = text.rfind('\n', 0, m.start()) + 1
            line_end = text.find('\n', m.end())
            line_end = len(text) if line_end < 0 else line_end
            prefix = text[line_start:m.start()]
            suffix = text[m.end():line_end]
            quoted = (('"' in prefix and '"' in suffix)
                      or (QL in prefix and QR in suffix))
            # 否定语境：前 16 字符含否定词（"不含/禁止…豁免条款"链式列举会超 8 字）
            neg_prefix = re.search('(不含|不得|不能|不会|禁止|没有|拒绝|无视)',
                                   text[max(0, m.start() - 16):m.start()])
            # 防线描述后缀：命中段后 12 字符内是"即 fail/即拒绝/即报错"类结果词
            # （"出现豁免/放宽/跳过关卡类条款即 fail"——INV-10/AC-12 原文形态）
            guard_suffix = re.search('(即|→)\s*(fail|拒绝|报错|红|拦截|判为)',
                                     text[m.end():m.end() + 12])
            if quoted or neg_prefix or guard_suffix:
                continue
            seg = text[max(0, m.start() - 40):m.end() + 40].replace('\n', ' ')
            errs.append('注入条款嫌疑（INV-10/AC-12）: …' + seg + '…')
            break

    if errs:
        fail(errs)
    print(f"OK spec-check（g010 过渡版）: taskId={fm['taskId']} AC×{len(acs)} "
          f"blastRadius×{len(fm['blastRadius'])} 结构/注入双扫通过")
    if fix_out:
        # 归一化产物（剥围栏/补闭合 --- 后的形态）落盘，供 spec-pr.py 消费
        open(fix_out, "w", encoding="utf-8", newline="\n").write(text + "\n")
        print(f"归一化 spec → {fix_out}")


if __name__ == "__main__":
    main()
