#!/usr/bin/env python3
"""pilot —— 符号执行试点评估器（IR-0004 AC-5，rev6：仪器+机械判定）。

流程（纯机械）：
  1. 探测 pynguin 可用性（`python -m pynguin --version` 子进程，只探测不硬依赖）；
  2. 可用 → 对目标模块跑 pynguin（子进程，默认 300s 超时）；
  3. 不可用/跑挂 → 内置静态近似（AST 路径计数：分支数/循环深度/可达路径上界估计），
     此时所有代理指标显式标注 proxy=true；
  4. 输出三段式证据报告（markdown）+ JSON 指标；结论只允许 adopt|reject 二值+理由，
     reject 必带 revisit_when；附「复算命令」行（ADR-0085 复算锚点）。

判定规则（机械，写死阈值）：
  - proxy 模式（pynguin 不可用）：reject —— 证据是静态近似，不足以采纳试点；
  - pynguin 成功完成且产出统计：adopt；
  - pynguin 运行失败/超时：reject（带 revisit_when）。

CLI：
  python pilot.py --target module.py --out-dir reports/ [--timeout 300]
  python pilot.py --target src/ --force-proxy   # 跳过探测，强制静态近似
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import subprocess
import sys
import time
from pathlib import Path

try:  # 双模式导入：包内 / 独立脚本
    from pipeline.testing import _yamlmini  # noqa: F401  (确保包路径可用)
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

EXEC_BUDGET_PER_FUNCTION = 200   # 每函数假定执行预算（机械常数，报告中披露）
PATH_CAP = 10 ** 18              # 路径上界数值封顶（防爆 int 表示）
DECISION_NODES = (ast.If, ast.While, ast.For, ast.AsyncFor, ast.IfExp, ast.ExceptHandler)
LOOP_NODES = (ast.While, ast.For, ast.AsyncFor)


# ---------------------------------------------------------------- 静态近似


def _iter_functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _count_decisions(func):
    count = 0
    for node in ast.walk(func):
        if isinstance(node, DECISION_NODES):
            count += 1
        elif isinstance(node, ast.BoolOp):
            count += len(node.values) - 1
        elif isinstance(node, ast.Match):
            count += max(0, len(node.cases) - 1)
        elif isinstance(node, ast.comprehension):
            count += 1 + len(node.ifs)
    return count


def _max_loop_depth(func):
    depth = 0

    def visit(node, cur):
        nonlocal depth
        if isinstance(node, LOOP_NODES):
            cur += 1
            depth = max(depth, cur)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not func:
            return
        for child in ast.iter_child_nodes(node):
            visit(child, cur)

    visit(func, 0)
    return depth


def analyze_module(path):
    """单文件 AST 路径统计（机械，无启发式评分）。"""
    source = Path(path).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source, filename=str(path))
    functions = []
    for func in _iter_functions(tree):
        decisions = _count_decisions(func)
        functions.append(
            {
                "function": func.name,
                "decisions": decisions,
                "loop_depth": _max_loop_depth(func),
                "paths_upper_bound": min(2 ** min(decisions, 60), PATH_CAP),
            }
        )
    return {"file": str(path), "functions": functions}


def static_approximation(target):
    """目录/文件级静态近似指标。"""
    path = Path(target)
    files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
    modules = []
    started = time.monotonic()
    for f in files:
        try:
            modules.append(analyze_module(f))
        except SyntaxError as exc:
            modules.append({"file": str(f), "parse_error": str(exc), "functions": []})
    analysis_seconds = max(time.monotonic() - started, 1e-6)

    total_functions = sum(len(m["functions"]) for m in modules)
    total_decisions = sum(f["decisions"] for m in modules for f in m["functions"])
    max_loop_depth = max([f["loop_depth"] for m in modules for f in m["functions"]] or [0])
    product = 1
    for m in modules:
        for f in m["functions"]:
            product = min(product * max(1, f["paths_upper_bound"]), PATH_CAP)
    coverage_per_function = []
    risky = 0
    for m in modules:
        for f in m["functions"]:
            if f["decisions"] == 0:
                coverage_per_function.append(1.0)
                continue
            est = min(1.0, EXEC_BUDGET_PER_FUNCTION / f["paths_upper_bound"])
            coverage_per_function.append(est)
            if f["paths_upper_bound"] > EXEC_BUDGET_PER_FUNCTION:
                risky += 1
    path_coverage_estimate = (
        sum(coverage_per_function) / len(coverage_per_function) if coverage_per_function else 1.0
    )
    return {
        "modules": modules,
        "metrics": {
            "files": len(modules),
            "functions": total_functions,
            "branches": total_decisions,
            "max_loop_depth": max_loop_depth,
            "path_upper_bound": product,
            "log2_path_upper_bound": round(math.log2(product), 3) if product > 0 else 0.0,
            "path_coverage_estimate": round(path_coverage_estimate, 6),
            "risky_functions_gt_budget": risky,
            "exec_budget_per_function": EXEC_BUDGET_PER_FUNCTION,
            "analysis_seconds": round(analysis_seconds, 6),
            "findings_per_second": round(risky / analysis_seconds, 6) if analysis_seconds else 0.0,
        },
    }


# ---------------------------------------------------------------- pynguin


def probe_pynguin(timeout=15):
    """探测 pynguin 是否可执行（只探测调用，失败不算错误）。"""
    cmd = [sys.executable, "-m", "pynguin", "--version"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()[:200]


def run_pynguin(target, timeout):
    """对目标跑 pynguin（子进程）。返回 (ok, detail)。"""
    path = Path(target)
    if path.is_dir():
        project, module = str(path), path.name
    else:
        project, module = str(path.parent), path.stem
    cmd = [
        sys.executable, "-m", "pynguin",
        "--project-path", project,
        "--module-name", module,
        "--output-path", str(path.parent / ".pynguin-out"),
        "--no-error-on-fail",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "pynguin 超时（>%ss）" % timeout
    except OSError as exc:
        return False, "pynguin 启动失败: %s" % exc
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-2000:]
    return proc.returncode == 0, tail


# ---------------------------------------------------------------- 报告


def build_report(target, force_proxy=False, pynguin_timeout=300, probe_timeout=15):
    tool = "static-approx"
    proxy = True
    pynguin_detail = None
    if not force_proxy:
        available, version = probe_pynguin(timeout=probe_timeout)
        if available:
            tool = "pynguin"
            ok, tail = run_pynguin(target, pynguin_timeout)
            pynguin_detail = {"version": version, "ok": ok, "output_tail": tail[-500:]}
            if not ok:
                tool = "static-approx"
                proxy = True

    static = static_approximation(target)
    metrics = dict(static["metrics"])
    metrics["tool"] = tool
    metrics["proxy"] = proxy
    # 求解超时率：proxy 模式不可测（null，显式 proxy=true）；pynguin 模式=运行超时占比
    if tool == "pynguin":
        metrics["solver_timeout_rate"] = 0.0 if pynguin_detail["ok"] else 1.0
    else:
        metrics["solver_timeout_rate"] = None

    # 机械二值判定
    if tool == "pynguin" and pynguin_detail and pynguin_detail["ok"]:
        decision, rationale = "adopt", (
            "pynguin 可用且在 %ss 预算内完成目标模块（静态基线：函数 %d / 分支 %d / "
            "路径上界 %s）；试点证据真实（proxy=false），建议采纳并扩面。"
            % (pynguin_timeout, metrics["functions"], metrics["branches"], metrics["path_upper_bound"])
        )
        revisit_when = None
    elif tool == "pynguin":
        decision = "reject"
        rationale = "pynguin 运行失败或超时，试点证据不可用；静态基线路径上界 %s。" % metrics["path_upper_bound"]
        revisit_when = (
            "目标模块消除 IO 副作用/外部依赖后，或单模块预算提升至 ≥600s；"
            "复算命令见下（ADR-0085 锚点）。"
        )
    else:
        decision = "reject"
        rationale = (
            "pynguin 不可用，证据仅为静态近似（proxy=true：分支 %d，路径上界 %s，"
            "预算内覆盖率估计 %.2e）——不足以支撑采纳符号执行试点。"
            % (metrics["branches"], metrics["path_upper_bound"], metrics["path_coverage_estimate"])
        )
        revisit_when = (
            "CI 镜像安装 pynguin（pip install pynguin）且目标模块可离线导入时；"
            "预算 ≥300s/模块。复算命令见下（ADR-0085 锚点）。"
        )

    recompute = [
        sys.executable, str(Path(__file__).resolve()),
        "--target", str(target), "--out-dir", "<OUT_DIR>",
    ]
    if force_proxy:
        recompute.append("--force-proxy")
    return {
        "schema": "cloudbird/symbolic-pilot/1",
        "tool": tool,
        "proxy": proxy,
        "pynguin": pynguin_detail,
        "target": str(target),
        "metrics": metrics,
        "modules": static["modules"],
        "conclusion": {"decision": decision, "rationale": rationale, "revisit_when": revisit_when},
        "recompute_command": " ".join(recompute),
    }


def to_markdown(report):
    m = report["metrics"]
    lines = ["# 符号执行试点评估报告", ""]
    lines.append("- 目标: `%s`" % report["target"])
    lines.append("- 工具: %s（proxy=%s）" % (report["tool"], str(report["proxy"]).lower()))
    lines.append("")
    lines.append("## 1. 证据 (Evidence)")
    lines.append("")
    if report["pynguin"] is not None:
        lines.append("- pynguin 探测: 可用，version=%s" % report["pynguin"]["version"])
        lines.append("- pynguin 运行: %s" % ("成功" if report["pynguin"]["ok"] else "失败"))
    else:
        lines.append("- pynguin 探测: 不可用（或 --force-proxy），回落静态近似")
    lines.append("- 静态基线（AST，机械计数）:")
    lines.append("  - 文件数 %d / 函数 %d / 分支(判定节点) %d / 最大循环深度 %d" % (m["files"], m["functions"], m["branches"], m["max_loop_depth"]))
    lines.append("  - 可达路径上界 %s（log2=%.3f）" % (m["path_upper_bound"], m["log2_path_upper_bound"]))
    lines.append("  - 预算内覆盖率估计 %.6e（预算 %d 次执行/函数）" % (m["path_coverage_estimate"], m["exec_budget_per_function"]))
    lines.append("  - 超预算风险函数 %d 个，单位时间发现数 %.4f/s" % (m["risky_functions_gt_budget"], m["findings_per_second"]))
    lines.append("")
    lines.append("## 2. 指标 (Metrics)")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## 3. 结论 (Conclusion)")
    lines.append("")
    c = report["conclusion"]
    lines.append("- decision: **%s**" % c["decision"])
    lines.append("- rationale: %s" % c["rationale"])
    if c["revisit_when"]:
        lines.append("- revisit_when: %s" % c["revisit_when"])
    lines.append("")
    lines.append("复算命令: `%s`" % report["recompute_command"])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="符号执行试点评估器（AC-5）")
    parser.add_argument("--target", required=True, help="目标 .py 文件或目录")
    parser.add_argument("--out-dir", default="symbolic-reports", help="报告输出目录")
    parser.add_argument("--timeout", type=int, default=300, help="pynguin 运行超时（秒）")
    parser.add_argument("--probe-timeout", type=int, default=15, help="pynguin 探测超时（秒）")
    parser.add_argument("--force-proxy", action="store_true", help="跳过探测，强制静态近似")
    args = parser.parse_args(argv)

    target = Path(args.target)
    if not target.exists():
        print("FATAL: 目标不存在: %s" % target, file=sys.stderr)
        return 2

    report = build_report(
        target, force_proxy=args.force_proxy, pynguin_timeout=args.timeout, probe_timeout=args.probe_timeout
    )
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    (out / ("pilot-report-%s.json" % stamp)).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (out / ("pilot-report-%s.md" % stamp)).write_text(
        to_markdown(report), encoding="utf-8", newline="\n"
    )
    print(json.dumps({"decision": report["conclusion"]["decision"], "proxy": report["proxy"], "metrics": report["metrics"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
