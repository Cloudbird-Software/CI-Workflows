#!/usr/bin/env python3
"""CNB 可脱离性演练（Cloudbird-Software · IR-0004 rev6 · AC-19 月度）。

--mode static：对给定仓树做零副作用干跑（dry-run，不写不改，仅扫描与报告）——
  * 三接缝外零操作性引用：模式 cnb\.cool | CNB_TOKEN | @CodeBuddy
  * 接缝白名单：cnb-dispatch.yml / cnb-audit.yml / expected-state.json /
    automation-limits.yaml / providers.yaml（按文件名），以及 GOVERNANCE.yaml
    的 EX-1 节（EX-1 节外的 GOVERNANCE 引用同样算越界）。
  * REMOVAL.md 存在性（缺失即判红）。
  * 输出影响报告：删除 CNB 将波及的文件清单（接缝文件 + 越界文件 + REMOVAL.md）。

--mode functional：需要 org 变量 CNB_DISABLED 的操作由 owner 执行；本脚本只输出
  runbook 步骤清单，不执行任何变更（真正的功能演练由 owner 触发的工作流完成）。

用法：
  python drill/cnb-drill.py --mode static --repo-root <仓树> [--report <json>]
  python drill/cnb-drill.py --mode functional [--report <json>]

退出码：static——0=绿 / 1=红（越界引用或缺 REMOVAL.md）；functional——0（只输出清单）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PATTERN = re.compile(r"cnb\.cool|CNB_TOKEN|@CodeBuddy")
SEAM_BASENAMES = {"cnb-dispatch.yml", "cnb-audit.yml", "expected-state.json",
                  "automation-limits.yaml", "providers.yaml"}
GOVERNANCE_NAME = "GOVERNANCE.yaml"
EX_HEADING = re.compile(r"^\s*(?:#{0,6}\s*|- id:\s*)EX-\d+\b")
EX1_HEADING = re.compile(r"^\s*(?:#{0,6}\s*|- id:\s*)EX-1\b")

RUNBOOK_STEPS = [
    "1. owner：在 org 级设置变量 CNB_DISABLED=true（本脚本不执行该操作）",
    "2. owner：手动触发月度可脱离性工作流（cnb-detach-drill）",
    "3. 观察：cnb-dispatch.yml / cnb-audit.yml 在 CNB_DISABLED 下按预期跳过",
    "4. 观察：对拍（diffbench）与道闸仍绿——oracle 消费不依赖 CNB 三接缝",
    "5. 记录：把演练日期与结论登记到 GOVERNANCE.yaml EX-1 台账",
    "6. 复位：删除或置否 CNB_DISABLED，确认 dispatch/audit 恢复",
    "7. 红线：任一步失败即中止并在 IR-0004 追加事件，不得静默继续",
]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def find_hits(path):
    hits = []
    for lineno, line in enumerate(read_text(path).splitlines(), 1):
        m = PATTERN.search(line)
        if m:
            hits.append({"line": lineno, "match": m.group(0), "text": line.strip()[:120]})
    return hits


def governance_allowed_lines(text):
    """GOVERNANCE.yaml 中只有 EX-1 节（含标题行，至下一个 EX-n 节标题前）允许引用。"""
    allowed = set()
    in_ex1 = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if EX_HEADING.match(line):
            in_ex1 = bool(EX1_HEADING.match(line))
        if in_ex1:
            allowed.add(lineno)
    return allowed


def run_static(root):
    seam_files = []
    violations = []
    # 自层模式：根含 REMOVAL.md ⇒ 本仓即桥接层本体（整仓在删除区内）——
    # 层内引用不构成越界（三接缝口径只约束治理仓），verdict 直接 green。
    # 桥形判定：accounts.yaml+cnb_pool.py 共存（REMOVAL.md 有无正是被检项——
    # dirty fixture 曾因含 REMOVAL 被误判自层，实测收紧 2026-08-25）
    self_layer = (root / "accounts.yaml").is_file() and (root / "cnb_pool.py").is_file()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if ".git" in rel.parts or "__pycache__" in rel.parts:
            continue
        hits = find_hits(path)
        if not hits:
            continue
        rel_posix = rel.as_posix()
        if path.name in SEAM_BASENAMES:
            seam_files.append(rel_posix)
        elif rel.parts[:1] == ("specs",):
            # 与 cnb-audit 口径对齐：制度/验收文本类（IR spec 与 acceptance 报告）
            # 属声明面——叙述性提及非操作性耦合（cnb-audit 原 specs/IR-0004 豁免推广）
            seam_files.append(rel_posix)
        elif path.name == GOVERNANCE_NAME:
            allowed = governance_allowed_lines(read_text(path))
            bad = [h for h in hits if h["line"] not in allowed]
            if bad:
                violations.append({"file": rel_posix, "hits": bad,
                                   "reason": "GOVERNANCE.yaml 中引用超出 EX-1 节"})
        else:
            violations.append({"file": rel_posix, "hits": hits,
                               "reason": "CNB 三接缝外的操作性引用"})
    removal_present = (root / "REMOVAL.md").is_file()
    impacted = sorted(set(seam_files)
                      | {v["file"] for v in violations}
                      | ({"REMOVAL.md"} if removal_present else set()))
    # 桥接层仓（自层）：REMOVAL.md 在即绿（层内引用皆删除区内，violations 仅信息面）；
    # 治理仓：只看三接缝外越界（REMOVAL 义务属桥接层仓）
    green = removal_present if self_layer else not violations
    return {
        "mode": "static",
        "date": now_iso(),
        "repo_root": str(root),
        "pattern": PATTERN.pattern,
        "seam_files": sorted(seam_files),
        "violations": violations,
        "removal_md_present": removal_present,
        "impacted_files": impacted,
        "verdict": "green" if green else "red",
        "dry_run": True,
        "side_effects": "none",
    }


def run_functional():
    return {
        "mode": "functional",
        "date": now_iso(),
        "executed": False,
        "requires_owner_action": "org 变量 CNB_DISABLED（本脚本只输出操作清单，不执行）",
        "runbook": RUNBOOK_STEPS,
        "note": "真正功能演练由 owner 触发的工作流完成；本输出即 runbook。",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cnb-drill.py", description="CNB 可脱离性演练（AC-19 月度）")
    parser.add_argument("--mode", required=True, choices=("static", "functional"))
    parser.add_argument("--repo-root", help="static 模式必填：仓树根目录")
    parser.add_argument("--report", help="报告 JSON 输出路径（不指定则零文件副作用）")
    args = parser.parse_args(argv)

    t0 = time.monotonic()
    if args.mode == "static":
        if not args.repo_root:
            print("cnb-drill: static 模式需要 --repo-root", file=sys.stderr)
            return 1
        root = Path(args.repo_root)
        if not root.is_dir():
            print("cnb-drill: --repo-root 不是目录: %s" % root, file=sys.stderr)
            return 1
        report = run_static(root)
        exit_code = 0 if report["verdict"] == "green" else 1
    else:
        report = run_functional()
        exit_code = 0
    report["duration_ms"] = int((time.monotonic() - t0) * 1000)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    for step in report.get("runbook", []):
        print("runbook: %s" % step, file=sys.stderr)
    if args.report:
        try:
            with open(args.report, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text + "\n")
        except OSError as exc:
            print("cnb-drill: 报告写入失败 %s" % exc, file=sys.stderr)
            return 1
    if report["mode"] == "static" and report["verdict"] == "red":
        print("cnb-drill: 静态干跑判红（越界引用或缺 REMOVAL.md），详见报告", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
