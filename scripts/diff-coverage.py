#!/usr/bin/env python3
"""diff-coverage.py — PR 变更行覆盖率（diff coverage）门禁工具。

ADR-0037（P2-3，.github issue #88）：本次变更行（unified diff 新增行，含修改行，
删除行不计）中被测试执行到的比例必须 ≥ policy 阈值——而非全局覆盖率（全局口径
按 .github governance/policy/testing.yaml X-01 继续拒绝）。

覆盖率数据格式（受管仓语言盘点，ADR-0037 决策 1）:
  lcov       node 仓 vitest --coverage（coverage/lcov.info）
  istanbul   node 仓 istanbul JSON（coverage/coverage-final.json）
  cobertura  python 仓 pytest-cov --cov-report=xml（coverage.xml）
  go         go 仓 go test -coverprofile（coverage.out）

用法（CI，见 .github/workflows/diff-coverage.yml）:
  git diff --no-color --no-ext-diff -M -U0 "$BASE_SHA" HEAD > diff.patch
  python3 diff-coverage.py --diff-file diff.patch --policy policy-testing.yaml \
      --repo "$REPO_NAME" [--threshold 80] [--format auto] [--coverage-file ...]

fail-closed（ADR-0037 决策 5）: 存在非豁免变更行但覆盖率数据缺失/不可解析、
policy 拉取失败或缺段、显式阈值与 policy repo_overrides 登记不符——一律非零退出。

自测（#88 T6）: python3 diff-coverage.py --self-test
预标注 fixture 见 scripts/diff-coverage-fixtures/，断言与人工计算值完全一致。

退出码: 0=达标  1=门槛红（阈值未达/fail-closed 违规）  2=工具/配置错误
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

EPS = 1e-9  # 浮点边界：≥ threshold-EPS 绿（等值绿，#88 T4）
ANNOTATION_CAP = 10  # GitHub 每 step 注解上限

# 覆盖率文件自动发现顺序（--coverage-file 未给时，相对 --coverage-root 逐个探测）
AUTO_CANDIDATES = [
    "coverage/lcov.info",
    "coverage/coverage-final.json",
    "coverage.xml",
    "reports/coverage.xml",
    "coverage.out",
]


class ToolError(Exception):
    """工具/配置错误（exit 2）——与门槛红（exit 1）区分"""


# ---------------------------------------------------------------- diff 解析
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _git_unquote(path: str) -> str:
    """git diff 路径反引号转义还原（"a/sp ace" 与带 \305\245 八进制 UTF-8）。

    不处理则含空格/非 ASCII 文件名静默失去覆盖要求——门禁被文件名绕过。
    """
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        out = bytearray()
        body = path[1:-1]
        i = 0
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                n = body[i + 1]
                if n in ('"', "\\"):
                    out.append(ord(n)); i += 2
                elif n == "n":
                    out.append(10); i += 2
                elif n == "t":
                    out.append(9); i += 2
                elif n in "01234567" and i + 3 < len(body):
                    out.append(int(body[i + 1:i + 4], 8)); i += 4
                else:
                    out.append(ord(c)); i += 1
            else:
                out.extend(c.encode("utf-8")); i += 1
        return out.decode("utf-8", errors="replace")
    return path


def _strip_ab(path: str) -> str:
    """去掉 diff 头的 a/ b/ 前缀（new file 的 +++ b/x；--- /dev/null 已在外层排除）"""
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def parse_unified_diff(text: str) -> dict[str, list[int]]:
    """解析 unified diff → {新路径: [新增行号]}（含修改行=+新侧；删除行不计）。

    兼容任意 -U 上下文宽度（CI 用 -U0，但本地/防御性解析不依赖它）。
    纯 rename（-M 相似度 100%）无 hunk → 不产生条目（无行变更）。
    """
    result: dict[str, list[int]] = {}
    current: str | None = None
    new_lineno = 0
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("+++ "):
            raw = line[4:]
            if not raw.startswith('"') and raw.endswith("\t"):
                raw = raw[:-1]  # git 对含空格文件名的 ---/+++ 行补一个 TAB 界定
            p = _git_unquote(raw)
            if p == "/dev/null":
                current = None  # 文件被删除——无新行
                in_hunk = False
                continue
            current = _strip_ab(p)
            result.setdefault(current, [])
            in_hunk = False
            continue
        if line.startswith("--- "):
            continue
        m = _HUNK_RE.match(line)
        if m:
            if current is None:
                raise ToolError(f"hunk 出现在未识别文件头之后: {line!r}")
            new_lineno = int(m.group(1))
            in_hunk = True
            continue
        if not in_hunk or current is None:
            continue
        if line.startswith("+"):  # 新增行（含修改行的新侧）
            result[current].append(new_lineno)
            new_lineno += 1
        elif line.startswith("-"):
            continue  # 旧行不计入新侧行号推进
        elif line.startswith("\\"):  # "\ No newline at end of file"
            continue
        else:  # 上下文行
            new_lineno += 1
    return {p: sorted(ls) for p, ls in result.items() if ls}


# ------------------------------------------------------------ 覆盖率解析
def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./") if path.startswith("./") \
        else path.replace("\\", "/")


def parse_lcov(text: str) -> dict[str, tuple[set[int], set[int]]]:
    """lcov（SF/DA/end_of_record）→ {路径: (计量行, 覆盖行)}"""
    out: dict[str, tuple[set[int], set[int]]] = {}
    sf = None
    measured: set[int] = set()
    covered: set[int] = set()
    for line in text.splitlines():
        if line.startswith("SF:"):
            sf = _norm(line[3:])
            measured, covered = set(), set()
        elif line.startswith("DA:") and sf is not None:
            parts = line[3:].split(",")
            if len(parts) >= 2 and parts[0].isdigit():
                ln = int(parts[0])
                hits = int(parts[1]) if parts[1].lstrip("-").isdigit() else 0
                measured.add(ln)
                if hits > 0:
                    covered.add(ln)
        elif line.startswith("end_of_record") and sf is not None:
            out[sf] = (measured, covered)
            sf = None
    if sf is not None:  # 截断的 lcov——末记录仍可用
        out[sf] = (measured, covered)
    return out


def parse_istanbul(text: str) -> dict[str, tuple[set[int], set[int]]]:
    """istanbul JSON（coverage-final.json）→ 语句起始行级（无语句的纯语法行不计分母）"""
    data = json.loads(text)
    out: dict[str, tuple[set[int], set[int]]] = {}
    for path, entry in data.items():
        smap = entry.get("statementMap") or {}
        counts = entry.get("s") or {}
        measured: set[int] = set()
        covered: set[int] = set()
        for idx, stmt in smap.items():
            try:
                ln = int(stmt["start"]["line"])
            except (KeyError, TypeError, ValueError):
                continue
            measured.add(ln)
            try:
                if int(counts.get(idx, 0)) > 0:
                    covered.add(ln)
            except (TypeError, ValueError):
                continue
        if measured:
            out[_norm(path)] = (measured, covered)
    return out


def parse_cobertura(text: str) -> dict[str, tuple[set[int], set[int]]]:
    """Cobertura XML（coverage.xml）→ <class filename>/<line number hits>"""
    root = ET.fromstring(text)
    out: dict[str, tuple[set[int], set[int]]] = {}
    # sources 前缀（filename 相对 source 根；取最短 source 作仓库根近似，后缀匹配兜底）
    sources = [(_norm(s.text or "").rstrip("/") + "/") for s in root.iter("source")]
    for cls in root.iter("class"):
        fname = cls.get("filename")
        if not fname:
            continue
        measured: set[int] = set()
        covered: set[int] = set()
        for ln in cls.iter("line"):
            num, hits = ln.get("number"), ln.get("hits")
            if num is None or not num.isdigit():
                continue
            n = int(num)
            measured.add(n)
            try:
                if int(hits or 0) > 0:
                    covered.add(n)
            except ValueError:
                continue
        if measured:
            key = _norm(fname)
            for pref in sources:  # 相对 source 根的仓相对路径优先
                if key.startswith(pref):
                    key = key[len(pref):]
                    break
            out[key] = (measured, covered)
    return out


def parse_gocov(text: str) -> dict[str, tuple[set[int], set[int]]]:
    """go cover profile: name.go:startLine.startCol,endLine.endCol numStmt count
    行级近似（ADR-0037 决策 1）：块内 [start,end] 闭区间行，count>0 记覆盖。
    """
    out: dict[str, tuple[set[int], set[int]]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("mode:"):
            continue
        m = re.match(r"^(.+\.go):(\d+)\.\d+,(\d+)\.\d+\s+\d+\s+(\d+)$", line)
        if not m:
            raise ToolError(f"go covprofile 行无法解析: {line!r}")
        path, s, e, count = _norm(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        lines = set(range(s, e + 1))
        measured, covered = out.get(path, (set(), set()))
        measured |= lines
        if count > 0:
            covered |= lines
        out[path] = (measured, covered)
    return out


def sniff_format(text: str, explicit: str) -> str:
    fmt = (explicit or "auto").lower()
    if fmt != "auto":
        return fmt
    head = text.lstrip()[:512]
    if head.startswith("mode:"):
        return "go"
    if "end_of_record" in text[:4096]:
        return "lcov"
    if head.startswith("{"):
        return "istanbul"
    if head.startswith("<"):
        return "cobertura"
    raise ToolError(f"覆盖率格式自动识别失败（--format 显式指定: lcov|istanbul|cobertura|go）")


def load_coverage(path: Path, explicit: str) -> tuple[dict[str, tuple[set[int], set[int]]], str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fmt = sniff_format(text, explicit)
    parser = {"lcov": parse_lcov, "istanbul": parse_istanbul,
              "cobertura": parse_cobertura, "go": parse_gocov}[fmt]
    data = parser(text)
    if not data:
        raise ToolError(f"覆盖率文件解析结果为空（format={fmt}）: {path}")
    return data, fmt


# ------------------------------------------------------------ policy 与豁免
def load_policy(path: Path) -> dict:
    try:
        import yaml  # CI 内经 requirements-diff-coverage.txt 哈希锚定安装
    except ImportError as e:
        raise ToolError(f"policy 解析需要 PyYAML（workflow 内 pip --require-hashes 安装）: {e}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("diff_coverage"), dict):
        raise ToolError(f"policy 缺 diff_coverage 段（ADR-0037）: {path}")
    sec = data["diff_coverage"]
    if "threshold_pct" not in sec:
        raise ToolError("policy diff_coverage.threshold_pct 缺失")
    return sec


def _as_list(v) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in v]


def build_exemption(sec: dict):
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in _as_list(sec.get("exempt_extensions"))}
    names = set(_as_list(sec.get("exempt_filenames")))
    paths = [p.replace("\\", "/") for p in _as_list(sec.get("exempt_paths"))]
    return exts, names, paths


def is_exempt(path: str, exts: set[str], names: set[str], paths: list[str]) -> bool:
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    if base in names:
        return True
    if "." in base and ("." + base.rsplit(".", 1)[-1].lower()) in exts:
        return True
    for pat in paths:
        if pat.endswith("/") and (p + "/").startswith(pat):
            return True
        if not pat.endswith("/") and fnmatch.fnmatch(p, pat):
            return True
    return False


# ------------------------------------------------------------- 路径匹配
def match_cov_path(diff_path: str, cov_paths: list[str]) -> str | None:
    """精确 → 最长后缀匹配（覆盖率工具常写绝对路径 /home/runner/work/repo/repo/src/x）"""
    dp = _norm(diff_path)
    if dp in cov_paths:
        return dp
    best, best_len = None, -1
    for cp in cov_paths:
        if dp.endswith("/" + cp) or cp.endswith("/" + dp):
            l = min(len(dp), len(cp))
            if l > best_len:
                best, best_len = cp, l
    return best


# ------------------------------------------------------------- 评估
def evaluate(diff_text: str, coverage: dict[str, tuple[set[int], set[int]]] | None,
             sec: dict, repo: str, cli_threshold: float | None):
    """返回 dict: pass, pct, denominator, covered, exempt_files, files_detail,
    no_data（有变更行但无覆盖率数据的文件→fail-closed）, uncovered 列表"""
    threshold = float(sec["threshold_pct"])
    overrides = sec.get("repo_overrides") or {}
    declared = float(overrides[repo]) if repo in overrides else threshold
    if cli_threshold is not None and not math.isclose(cli_threshold, declared, abs_tol=1e-6):
        raise ToolError(
            f"显式阈值 {cli_threshold} 与 policy 登记值 {declared} 不符（repo={repo!r} "
            f"{'repo_overrides 未登记该覆盖' if repo not in overrides else 'repo_overrides 登记=' + str(overrides[repo])}"
            "——阈值不得由业务仓 PR 自行放宽/收紧，ADR-0037 决策 3）")
    eff = declared

    changed = parse_unified_diff(diff_text)
    exts, names, paths = build_exemption(sec)
    cov_paths = sorted(coverage) if coverage else []

    detail, no_data = {}, {}
    exempt_files = 0
    for path, lines in sorted(changed.items()):
        if is_exempt(path, exts, names, paths):
            exempt_files += 1
            continue
        if not cov_paths:
            no_data[path] = lines  # 覆盖率工件整体缺失
            continue
        cp = match_cov_path(path, cov_paths)
        if cp is None:
            no_data[path] = lines  # 该文件零覆盖数据
            continue
        measured, covered = coverage[cp]
        unc = [ln for ln in lines if ln in measured and ln not in covered]
        cnt = sum(1 for ln in lines if ln in measured)
        detail[path] = {"changed": len(lines), "measured": cnt,
                        "covered": cnt - len(unc), "uncovered": unc}
    denom = sum(d["measured"] for d in detail.values())
    cov_cnt = sum(d["covered"] for d in detail.values())
    pct = (cov_cnt / denom * 100.0) if denom else 0.0
    passed = denom == 0 or (pct >= eff - EPS)
    if no_data:
        passed = False  # fail-closed：非豁免变更行存在但无覆盖率数据
    return {"pass": passed, "threshold": eff, "pct": pct, "denominator": denom,
            "covered": cov_cnt, "exempt_files": exempt_files, "files": detail,
            "no_data": no_data, "changed_files": len(changed)}


def report(res: dict) -> int:
    print(f"== diff coverage（ADR-0037，口径=本次变更行，非全局） ==")
    print(f"阈值={res['threshold']:.1f}%（等值绿；< 即红）  "
          f"变更文件={res['changed_files']}（豁免 {res['exempt_files']}）  "
          f"计量变更行={res['denominator']}  覆盖={res['covered']}")
    for path, d in res["files"].items():
        unc = ("  未覆盖行:" + ",".join(map(str, d["uncovered"]))) if d["uncovered"] else ""
        print(f"  {path}: 变更 {d['changed']} 计量 {d['measured']} 覆盖 {d['covered']}{unc}")
    rc = 0
    if res["no_data"]:
        rc = 1
        for path, lines in res["no_data"].items():
            print(f"::error file={path}::存在变更行 {len(lines)} 行但无任何覆盖率数据"
                  f"（fail-closed：没测过≠测了没覆盖，ADR-0037 决策 5）——行号: "
                  f"{','.join(map(str, lines[:30]))}{'…' if len(lines) > 30 else ''}")
    if res["denominator"] == 0:
        if not res["no_data"]:
            print("无计量变更行（全部豁免或零源码变更）→ PASS（分母为空不执法）")
            return 0
        return rc  # 分母为 0 但存在无数据变更行——fail-closed 已在上方报错，不再给 0/0 汇总
    pct_s = f"{res['pct']:.4f}".rstrip("0").rstrip(".")
    met = res["pct"] >= res["threshold"] - EPS
    if met:
        print(f"diff coverage = {res['covered']}/{res['denominator']} = {pct_s}% ≥ {res['threshold']:.1f}%"
              f"（{'整体红：存在无覆盖率数据的变更文件，见上' if rc else '达标'}）")
    else:
        rc = 1
        print(f"::error::diff coverage {res['covered']}/{res['denominator']} = {pct_s}% < {res['threshold']:.1f}%——本次变更行未达标（全局覆盖率不参与口径，ADR-0037）")
        n = 0
        for path, d in res["files"].items():
            for ln in d["uncovered"]:
                if n >= ANNOTATION_CAP:
                    print(f"  （未覆盖行清单过长，注解截断至 {ANNOTATION_CAP}，完整清单见上）")
                    break
                print(f"::error file={path},line={ln}::变更行未覆盖")
                n += 1
    return rc


# ------------------------------------------------------------- 自测（T6）
def self_test() -> int:
    base = Path(__file__).resolve().parent / "diff-coverage-fixtures"
    cases = sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    if not cases:
        raise ToolError(f"未找到 fixture 目录: {base}")
    failures = 0
    for case in cases:
        exp = json.loads((case / "expected.json").read_text(encoding="utf-8"))
        cov_file = next((case / f) for f in
                        ["coverage.lcov", "coverage.json", "coverage.xml", "coverage.gocov"]
                        if (case / f).exists())
        coverage, fmt = load_coverage(cov_file, exp.get("format", "auto"))
        sec = load_policy(case / "policy.yaml")
        res = evaluate((case / "diff.patch").read_text(encoding="utf-8"), coverage, sec,
                       exp.get("repo", "demo"), exp.get("threshold_input"))
        got = {"pass": res["pass"], "pct": round(res["pct"], 4),
               "denominator": res["denominator"], "covered": res["covered"],
               "exempt_files": res["exempt_files"], "changed_files": res["changed_files"],
               "uncovered": {p: d["uncovered"] for p, d in res["files"].items() if d["uncovered"]},
               "no_data": sorted(res["no_data"]), "format": fmt}
        bad = [k for k in ("pass", "pct", "denominator", "covered", "exempt_files",
                           "uncovered", "no_data") if got.get(k) != exp.get(k)]
        if bad:
            failures += 1
            print(f"FAIL {case.name}: 字段 {bad}\n  期望={ {k: exp.get(k) for k in bad} }\n  实得={ {k: got.get(k) for k in bad} }")
        else:
            print(f"OK   {case.name}: pass={got['pass']} pct={got['pct']}% "
                  f"({got['covered']}/{got['denominator']}, 格式 {fmt}, 豁免文件 {got['exempt_files']})")
    if failures:
        print(f"::error::diff-coverage 自测 {failures}/{len(cases)} fixture 不一致（#88 T6）")
        return 1
    print(f"自测通过: {len(cases)} 组 fixture 与预标注值完全一致（#88 T6）")
    return 0


# ------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--diff-file", help="unified diff（git diff -M -U0 BASE HEAD 产出）")
    ap.add_argument("--policy", help="governance/policy/testing.yaml（真源 .github main）")
    ap.add_argument("--repo", default="", help="caller 仓库名（repo_overrides 对账键）")
    ap.add_argument("--threshold", type=float, default=None,
                    help="caller 显式声明阈值——须与 policy repo_overrides 登记一致")
    ap.add_argument("--format", default="auto",
                    choices=["auto", "lcov", "istanbul", "cobertura", "go"])
    ap.add_argument("--coverage-file", default="", help="覆盖率文件路径（空=自动发现）")
    ap.add_argument("--coverage-root", default=".", help="自动发现的根目录（默认 .）")
    ap.add_argument("--self-test", action="store_true", help="T6 fixture 自测")
    args = ap.parse_args()

    try:
        if args.self_test:
            return self_test()
        if not args.diff_file or not args.policy:
            raise ToolError("--diff-file 与 --policy 必填（--self-test 除外）")
        sec = load_policy(Path(args.policy))
        diff_text = Path(args.diff_file).read_text(encoding="utf-8", errors="replace")
        cov_path = None
        if args.coverage_file:
            cov_path = Path(args.coverage_file)
        else:
            root = Path(args.coverage_root)
            for cand in AUTO_CANDIDATES:
                if (root / cand).is_file():
                    cov_path = root / cand
                    break
        coverage = None
        if cov_path is not None and cov_path.is_file():
            coverage, fmt = load_coverage(cov_path, args.format)
            print(f"覆盖率数据: {cov_path}（format={fmt}）")
        else:
            print("覆盖率数据: 未找到（AUTO_CANDIDATES 或 --coverage-file）")
        res = evaluate(diff_text, coverage, sec, args.repo, args.threshold)
        return report(res)
    except ToolError as e:
        print(f"::error::diff-coverage 工具/配置错误（fail-closed，ADR-0037 决策 5）: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
