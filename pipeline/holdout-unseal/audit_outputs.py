#!/usr/bin/env python3
# audit_outputs.py —— 揭封链路输出面审计（W4-C3 .github#222 / ADR-0068 决策 5；宪法 §6/§4E）
#
# 两个模式（静态审 job 定义 + 运行时审日志——同一泄漏模型的两面）：
#   static  解析 workflow YAML，逐 job 审 run: 块每一行：凡含输出面命令
#           （echo/printf/cat/tee/grep/head/tail/jq——这些能把内容送进 PR 日志）
#           的行，必须带内联白名单注记 `# audit-ok: <理由>` 或命中内建安全模式
#           （注释/空行/set-/纯赋值/流程关键字/exit）。无注记的输出面=泄漏面=红。
#           保守偏置：grep -q 等实际无输出的行也要求注记——审计器分不清
#           "重定向去哪"，宁可多注记不可漏放行（宁滥勿缺，宪法 §4C 精神）。
#   scan    审运行日志文本：banned 词表（gate --banned-out 产出的测试名/文件名）+
#           canary registry markers（W1-C4 诱饵——出现在任何日志=P0，宪法 §6）+
#           通用测试节点 ID 正则。命中即红，且报警文本本身脱敏（只报 kind+位置，
#           绝不回显命中内容——审计日志不能成为二次泄漏源）。
#
# 用法:
#   audit_outputs.py static --workflow a.yml [--workflow b.yml]
#   audit_outputs.py scan --input run.log --banned banned.txt [--registry registry.yaml]
# 退出码：0=干净 | 1=发现泄漏面/泄漏 | 2=环境错误（文件/依赖缺失）
import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("FATAL: 缺 pyyaml（static 模式解析 workflow 必需）", file=sys.stderr)
    sys.exit(2)

OUT_TOKENS = re.compile(r"\b(echo|printf|cat|tee|grep|head|tail|jq)\b")
AUDIT_OK = "# audit-ok:"
SAFE_BUILTIN = [
    re.compile(p) for p in (
        r"^\s*#", r"^\s*$", r"^\s*set\s+-[euop\w\s]*$",
        r"^\s*[A-Za-z_][A-Za-z0-9_]*=(>.*)?$",  # 纯赋值（含覆盖重定向形）
        r"^\s*(if|then|else|elif|fi|for|while|do|done|case|esac|in|return)\b.*$",
        r"^\s*exit\s+\d+\s*(#.*)?$",
    )]
NODE_RE = re.compile(r"\btest_[A-Za-z0-9_]{3,}\b|\S+\.py::\S+")
MARKER_PREFIX = "CLOUDBIRD-HOLDOUT-CANARY-"  # 报警只报 kind+位置，marker 全串绝不回显


def cmd_static(args) -> int:
    violations = []
    for wf in args.workflow:
        try:
            doc = yaml.safe_load(Path(wf).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            print(f"FAIL  workflow 解析失败（fail-closed）: {wf}: {exc}", file=sys.stderr)
            return 2
        jobs = (doc or {}).get("jobs") or {}
        for jname, job in jobs.items():
            for step in job.get("steps") or []:
                run = step.get("run")
                if not run:
                    continue
                for lineno, line in enumerate(run.splitlines(), 1):
                    if not OUT_TOKENS.search(line):
                        continue
                    if AUDIT_OK in line or any(p.match(line) for p in SAFE_BUILTIN):
                        continue
                    violations.append(f"{Path(wf).name}:{jname} step[{step.get('name', '?')}] "
                                      f"L{lineno}: 输出面命令未过白名单审查: {line.strip()[:120]}")
    if violations:
        for v in violations:
            print(f"FAIL  {v}")
        print(f"::error::输出面静态审计发现 {len(violations)} 处未注记的 echo/cat/…（泄漏面）——"
              f"逐行补 `# audit-ok: <理由>` 或改写为无输出形态（ADR-0068 决策 5）")
        return 1
    print("OK    输出面静态审计干净：全部输出面命令均过白名单注记/内建安全模式")
    return 0


def cmd_scan(args) -> int:
    try:
        log = Path(args.input).read_text(encoding="utf-8", errors="replace")
        banned = [w for w in Path(args.banned).read_text(encoding="utf-8").splitlines() if w.strip()]
    except OSError as exc:
        print(f"FAIL  输入读取失败（fail-closed）: {exc}", file=sys.stderr)
        return 2
    markers = []
    if args.registry:
        try:
            reg = yaml.safe_load(Path(args.registry).read_text(encoding="utf-8")) or {}
            markers = [m.get("marker", "") for m in reg.get("markers") or []]
        except (OSError, yaml.YAMLError) as exc:
            print(f"FAIL  canary registry 读取失败（fail-closed）: {exc}", file=sys.stderr)
            return 2
    hits = []

    def hit(kind, detail):
        hits.append(f"{kind} {detail}")  # 只报位置/编号，绝不回显命中内容（防二次泄漏）

    for i, ln in enumerate(log.splitlines(), 1):
        for w in banned:
            if w in ln:
                hit("banned", f"#L{i}（词表第 {banned.index(w) + 1} 项）")
        for m in markers:
            if m and m in ln:
                hit("canary-marker", f"#L{i}（marker 已脱敏）")
        for g in NODE_RE.finditer(ln):
            col = g.start()
            hit("node-id", f"#L{i}:0+{col}")
    if hits:
        for h in hits:
            print(f"LEAK  {h}")
        print(f"::error::运行日志泄漏审计命中 {len(hits)} 处（测试名/节点 ID/canary marker）——"
              f"揭封链路日志只允许计数（ADR-0068 决策 3/5）")
        return 1
    print(f"OK    运行日志泄漏审计干净（banned {len(banned)} 项 + marker {len(markers)} 条 + 节点正则）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="揭封链路输出面审计（AC-2 / ADR-0068 决策 5）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("static", help="静态审 workflow job 定义的输出面")
    ps.add_argument("--workflow", action="append", required=True)
    pc = sub.add_parser("scan", help="运行时审日志（banned 词表+markers+节点正则）")
    pc.add_argument("--input", required=True, help="待审日志文件")
    pc.add_argument("--banned", required=True, help="禁出词表（gate --banned-out 产物）")
    pc.add_argument("--registry", default="", help="holdout canary/registry.yaml（可选）")
    args = ap.parse_args()
    if args.cmd == "static":
        return cmd_static(args)
    if args.registry:
        args.registry = Path(args.registry)
    return cmd_scan(args)


if __name__ == "__main__":
    sys.exit(main())
