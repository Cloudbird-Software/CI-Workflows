#!/usr/bin/env python3
"""Generic mutation executor (IR-0004 AC-1).

CLI:
    python run_mutation.py --repo <path> --module <glob> --out <result.json>
        [--engine auto|builtin|mutmut] [--runner auto|pytest|minimal]
        [--timeout SECONDS_PER_MUTANT] [--max-mutants N] [--summary FILE.md]

Engines:
    mutmut   - used when importable (probed, never a hard dependency).
    builtin  - 8 self-contained AST operators applied to a scratch copy of
               the repo; each mutant re-runs the test suite once. This path
               works with the standard library alone and is the fallback.

Every accept/reject decision is a mechanical comparison of exit codes; no
LLM is involved in scoring.

Output JSON (schema mutation-result/1):
    {schema, engine, engine_note, runner, repo, module_glob, timestamp,
     baseline_passed, total_mutants, killed, survived, score_pct,
     mutants: [{id, seq, file, site_index, line, col, op, status}],
     survivors: [...], skipped_files: [...]}

Exit codes: 0 scored, 2 baseline test failure, 3 engine error.
"""

from __future__ import annotations

import argparse
import ast
import glob as globmod
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "mutation-result/1"

# --------------------------------------------------------------------------
# Built-in mutation operators (8)
# --------------------------------------------------------------------------

COMP_SWAP = {
    ast.Lt: ast.GtE,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.GtE: ast.Lt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
}
IN_IS_SWAP = {
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}
ARITH_SWAP = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
}

OPERATOR_NAMES = (
    "comp_swap",      # < <-> >=, > <-> <=, == <-> !=
    "arith_swap",     # + <-> -, * <-> /
    "int_bump",       # numeric constant n -> n + 1
    "bool_flip",      # True <-> False
    "str_mutate",     # "s" -> "XXsXX"
    "and_or_swap",    # and <-> or
    "not_removal",    # not x -> x
    "in_is_swap",     # in <-> not in, is <-> is not
)

OPERATOR_ALIASES = {
    "": "any",
    "any": "any",
    "comp_swap": "comp_swap",
    "compare": "comp_swap",
    "compare_swap": "comp_swap",
    "comparison_swap": "comp_swap",
    "arith": "arith_swap",
    "arithmetic": "arith_swap",
    "arith_swap": "arith_swap",
    "const_int": "int_bump",
    "int_bump": "int_bump",
    "const_bool": "bool_flip",
    "bool_flip": "bool_flip",
    "const_str": "str_mutate",
    "str_mutate": "str_mutate",
    "and_or": "and_or_swap",
    "and_or_swap": "and_or_swap",
    "not_removal": "not_removal",
    "not": "not_removal",
    "in_is_swap": "in_is_swap",
    "membership_swap": "in_is_swap",
}


class MutationError(Exception):
    """Raised when a mutation cannot be derived or applied."""


class _SiteCollector(ast.NodeVisitor):
    """Collects mutable sites in DFS field order (deterministic)."""

    def __init__(self) -> None:
        self.sites: list[dict] = []
        self._parent: dict[int, ast.AST] = {}

    def generic_visit(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self._parent[id(child)] = node
            self.visit(child)

    def _add(self, op: str, node: ast.AST, entry: int | None) -> None:
        parent = self._parent.get(id(node))
        if op in ("bool_flip", "int_bump", "str_mutate") and isinstance(parent, ast.Expr):
            return  # docstring / bare constant statement: not a behavior site
        self.sites.append(
            {
                "op": op,
                "node": node,
                "entry": entry,
                "parent": parent,
                "line": getattr(node, "lineno", 0),
                "col": getattr(node, "col_offset", 0),
            }
        )

    def visit_Compare(self, node: ast.Compare) -> None:
        for i, op in enumerate(node.ops):
            if type(op) in COMP_SWAP:
                self._add("comp_swap", node, i)
            elif type(op) in IN_IS_SWAP:
                self._add("in_is_swap", node, i)
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if type(node.op) in ARITH_SWAP:
            self._add("arith_swap", node, None)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if isinstance(value, bool):
            self._add("bool_flip", node, None)
        elif isinstance(value, (int, float)):
            self._add("int_bump", node, None)
        elif isinstance(value, str):
            self._add("str_mutate", node, None)
        # bytes / ellipsis: no built-in operator.

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self._add("and_or_swap", node, None)
        self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if isinstance(node.op, ast.Not):
            self._add("not_removal", node, None)
        self.generic_visit(node)


def enumerate_sites(source: str) -> list[dict]:
    """Parse source and return mutable sites (node refs, deterministic order)."""
    tree = ast.parse(source)
    collector = _SiteCollector()
    collector.visit(tree)
    for index, site in enumerate(collector.sites):
        site["index"] = index
    return collector.sites


def _apply_site(site: dict) -> None:
    node, op, entry = site["node"], site["op"], site["entry"]
    if op == "comp_swap":
        node.ops[entry] = COMP_SWAP[type(node.ops[entry])]()
    elif op == "in_is_swap":
        node.ops[entry] = IN_IS_SWAP[type(node.ops[entry])]()
    elif op == "arith_swap":
        node.op = ARITH_SWAP[type(node.op)]()
    elif op == "int_bump":
        node.value = node.value + 1
    elif op == "bool_flip":
        node.value = not node.value
    elif op == "str_mutate":
        node.value = "XX" + node.value + "XX"
    elif op == "and_or_swap":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif op == "not_removal":
        parent, replacement = site["parent"], site["node"].operand
        replaced = False
        for field, value in ast.iter_fields(parent):
            if value is node:
                setattr(parent, field, replacement)
                replaced = True
            elif isinstance(value, list):
                for j, item in enumerate(value):
                    if item is node:
                        value[j] = replacement
                        replaced = True
        if not replaced:
            raise MutationError("cannot detach 'not' node from parent")
    else:  # pragma: no cover - operator table is closed
        raise MutationError(f"unknown operator {op!r}")


def mutate_at(source: str, index: int) -> str:
    """Return source with the site at `index` (per-file enumeration) mutated."""
    tree = ast.parse(source)
    collector = _SiteCollector()
    collector.visit(tree)
    sites = collector.sites
    if not 0 <= index < len(sites):
        raise MutationError(f"site index {index} out of range (0..{len(sites) - 1})")
    for i, site in enumerate(sites):
        site["index"] = i
    _apply_site(sites[index])
    return ast.unparse(tree)


def describe_sites(source: str) -> list[dict]:
    """Serializable site list (no AST references) for reports and screening."""
    out = []
    for site in enumerate_sites(source):
        out.append({"index": site["index"], "line": site["line"], "col": site["col"], "op": site["op"]})
    return out


# --------------------------------------------------------------------------
# Target selection and scratch workspace
# --------------------------------------------------------------------------

_TESTFILE_NAMES = ("conftest.py", "setup.py")


def select_targets(repo: Path, pattern: str) -> list[Path]:
    """Python files matched by the module glob, minus tests and tooling."""
    matches = globmod.glob(str(Path(repo) / pattern), recursive=True)
    targets: list[Path] = []
    for raw in sorted(set(matches)):
        path = Path(raw)
        if not path.is_file() or path.suffix != ".py":
            continue
        name = path.name
        if name.startswith("test_") or name.endswith("_test.py") or name in _TESTFILE_NAMES:
            continue
        rel_parts = path.relative_to(repo).parts
        if any(part in ("tests", "test", "__pycache__", ".git", ".venv", "venv") for part in rel_parts[:-1]):
            continue
        targets.append(path)
    return targets


_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".mutmut-cache", "*.pyc",
    ".venv", "venv", "node_modules", ".mypy_cache", ".ruff_cache",
)


def make_workcopy(repo: Path, dest: Path) -> Path:
    shutil.copytree(repo, dest, ignore=_COPY_IGNORE)
    return dest


# --------------------------------------------------------------------------
# Test runners
# --------------------------------------------------------------------------

def resolve_runner(runner: str) -> str:
    if runner == "auto":
        return "pytest" if importlib.util.find_spec("pytest") is not None else "minimal"
    return runner


def run_test_suite(repo_dir: Path, runner: str = "auto", timeout: float = 180) -> dict:
    """Run the repo suite once; return {returncode, runner, timed_out, tail}."""
    runner = resolve_runner(runner)
    if runner == "pytest":
        cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--internal-run-repo", str(repo_dir)]
    try:
        proc = subprocess.run(
            cmd, cwd=str(repo_dir), capture_output=True, text=True, timeout=timeout
        )
        return {
            "returncode": proc.returncode,
            "runner": runner,
            "timed_out": False,
            "tail": (proc.stdout + proc.stderr)[-2000:],
        }
    except subprocess.TimeoutExpired as exc:
        tail = (exc.stdout or b"")
        if isinstance(tail, bytes):
            tail = tail.decode("utf-8", "replace")
        return {"returncode": None, "runner": runner, "timed_out": True, "tail": str(tail)[-2000:]}


def _minimal_runner_main(repo: Path) -> int:
    """Stdlib-only pytest substitute: unittest classes + plain test functions."""
    repo = repo.resolve()
    sys.path.insert(0, str(repo))
    import importlib.util
    import inspect

    files = sorted(p for p in repo.rglob("test_*.py"))
    files = [
        p for p in files
        if not any(part in (".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules")
                   for part in p.relative_to(repo).parts)
    ]
    failures: list[str] = []
    checked = 0
    for test_file in files:
        modname = f"_minirun_{len(files)}_{uuid.uuid4().hex[:8]}_{test_file.stem}"
        spec = importlib.util.spec_from_file_location(modname, test_file)
        if spec is None or spec.loader is None:  # pragma: no cover
            failures.append(f"{test_file}: cannot load")
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - any import error is a failure
            failures.append(f"{test_file}: import error: {exc!r}")
            continue
        for attr in sorted(dir(module)):
            obj = getattr(module, attr)
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase):
                suite = unittest.TestLoader().loadTestsFromTestCase(obj)
                result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
                checked += result.testsRun
                if not result.wasSuccessful():
                    failures.append(f"{test_file}:{obj.__name__}: {len(result.failures + result.errors)} failed")
            elif (callable(obj) and attr.startswith("test_")
                  and getattr(obj, "__module__", "") == modname):
                try:
                    if len(inspect.signature(obj).parameters) == 0:
                        checked += 1
                        obj()
                    else:
                        failures.append(f"{test_file}:{attr}: fixture args unsupported by minimal runner")
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{test_file}:{attr}: {exc!r}")
    if failures:
        print("MINIMAL-RUNNER FAILURES:")
        for line in failures:
            print(" -", line)
        return 1
    print(f"minimal runner: {checked} checks passed ({len(files)} files)")
    return 0


# --------------------------------------------------------------------------
# Built-in engine
# --------------------------------------------------------------------------

def run_builtin(repo: Path, module_glob: str, out_path: Path, runner: str = "auto",
                per_mutant_timeout: float = 60, max_mutants: int | None = None,
                summary: Path | None = None) -> dict:
    repo = Path(repo).resolve()
    targets = select_targets(repo, module_glob)
    mutants: list[dict] = []
    skipped_files: list[dict] = []
    baseline: dict | None = None
    seq = 0
    with tempfile.TemporaryDirectory(prefix="mutation-run-") as tmp:
        work = make_workcopy(repo, Path(tmp) / "repo")
        baseline = run_test_suite(work, runner=runner)
        if baseline["returncode"] != 0:
            result = {
                "schema": SCHEMA, "engine": "builtin", "engine_note": None,
                "runner": baseline["runner"], "repo": str(repo),
                "module_glob": module_glob,
                "timestamp": _utcnow(), "baseline_passed": False,
                "total_mutants": 0, "killed": 0, "survived": 0, "score_pct": 0.0,
                "mutants": [], "survivors": [], "skipped_files": [],
                "error": "baseline test suite failed; mutation run aborted",
                "baseline_tail": baseline["tail"],
            }
            _write_result(out_path, result, summary)
            return result
        for target in targets:
            rel = target.relative_to(repo).as_posix()
            try:
                source = target.read_text(encoding="utf-8")
                sites = enumerate_sites(source)
            except (SyntaxError, UnicodeDecodeError) as exc:
                skipped_files.append({"file": rel, "reason": f"parse error: {exc}"})
                continue
            work_file = work / rel
            for site in sites:
                if max_mutants is not None and seq >= max_mutants:
                    break
                mutated = mutate_at(source, site["index"])
                original = work_file.read_text(encoding="utf-8")
                work_file.write_text(mutated, encoding="utf-8", newline="\n")
                run = run_test_suite(work, runner=runner, timeout=per_mutant_timeout)
                work_file.write_text(original, encoding="utf-8", newline="\n")
                status = "killed" if (run["returncode"] not in (0,)) else "survived"
                record = {
                    "id": f"M{seq:04d}", "seq": seq, "file": rel,
                    "site_index": site["index"], "line": site["line"],
                    "col": site["col"], "op": site["op"], "status": status,
                }
                if run["timed_out"]:
                    record["note"] = "timeout treated as killed"
                mutants.append(record)
                seq += 1
            if max_mutants is not None and seq >= max_mutants:
                break
    killed = sum(1 for m in mutants if m["status"] == "killed")
    survived = len(mutants) - killed
    score = round(killed / len(mutants) * 100, 2) if mutants else 0.0
    result = {
        "schema": SCHEMA, "engine": "builtin", "engine_note": None,
        "runner": baseline["runner"], "repo": str(repo), "module_glob": module_glob,
        "timestamp": _utcnow(), "baseline_passed": True,
        "total_mutants": len(mutants), "killed": killed, "survived": survived,
        "score_pct": score, "mutants": mutants,
        "survivors": [m for m in mutants if m["status"] == "survived"],
        "skipped_files": skipped_files,
    }
    _write_result(out_path, result, summary)
    return result


# --------------------------------------------------------------------------
# mutmut engine (probed; installed by the execution environment, never vendored)
# --------------------------------------------------------------------------

_MUTMUT_COUNTERS = {
    "killed": re.compile(r"\U0001F389\s*(\d+)"),      # confetti ball
    "timeout": re.compile(r"\u23F0\s*(\d+)"),         # alarm clock
    "suspicious": re.compile(r"\U0001F914\s*(\d+)"),  # thinking face
    "survived": re.compile(r"\U0001F615\s*(\d+)"),    # sad face
}


def mutmut_available() -> bool:
    return importlib.util.find_spec("mutmut") is not None


def run_mutmut(repo: Path, module_glob: str, out_path: Path, overall_timeout: float = 1500,
               summary: Path | None = None) -> dict:
    repo = Path(repo).resolve()
    if not mutmut_available():
        raise MutationError("mutmut is not importable")
    with tempfile.TemporaryDirectory(prefix="mutation-mutmut-") as tmp:
        work = make_workcopy(repo, Path(tmp) / "repo")
        cmd = [sys.executable, "-m", "mutmut", "run",
               "--paths-to-mutate", module_glob, "--no-progress"]
        proc = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True,
                              timeout=overall_timeout)
        stdout = proc.stdout + "\n" + proc.stderr
        counts: dict[str, int] = {}
        for name, rx in _MUTMUT_COUNTERS.items():
            match = rx.search(stdout)
            counts[name] = int(match.group(1)) if match else 0
        if proc.returncode != 0 and not any(counts.values()):
            result = {
                "schema": SCHEMA, "engine": "mutmut", "engine_note": None,
                "repo": str(repo), "module_glob": module_glob,
                "timestamp": _utcnow(), "baseline_passed": None,
                "total_mutants": 0, "killed": 0, "survived": 0, "score_pct": 0.0,
                "survivors": [], "skipped_files": [], "mutants": [],
                "error": f"mutmut run failed rc={proc.returncode}",
                "mutmut_output_tail": stdout[-2000:],
            }
            _write_result(out_path, result, summary)
            return result
        killed = counts["killed"] + counts["timeout"] + counts["suspicious"]
        survived = counts["survived"]
        total = killed + survived
        survivor_ids = _mutmut_survivor_ids(work)
        survivors = [{"id": sid, "note": "mutmut id; not re-applicable by judge"}
                     for sid in survivor_ids]
        score = round(killed / total * 100, 2) if total else 0.0
        result = {
            "schema": SCHEMA, "engine": "mutmut", "engine_note": None,
            "runner": "pytest", "repo": str(repo), "module_glob": module_glob,
            "timestamp": _utcnow(), "baseline_passed": True,
            "total_mutants": total, "killed": killed, "survived": survived,
            "score_pct": score, "mutants": [], "survivors": survivors,
            "skipped_files": [],
        }
        _write_result(out_path, result, summary)
        return result


def _mutmut_survivor_ids(work: Path) -> list[str]:
    try:
        proc = subprocess.run([sys.executable, "-m", "mutmut", "results"],
                              cwd=str(work), capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return []
    match = re.search(r"survived[^:\n]*:\s*([0-9,\s]+)", proc.stdout)
    if not match:
        return []
    return [tok.strip() for tok in match.group(1).split(",") if tok.strip()]


# --------------------------------------------------------------------------
# Shared helpers and CLI
# --------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_result(out_path: Path, result: dict, summary: Path | None) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
    if summary is not None:
        summary = Path(summary)
        summary.parent.mkdir(parents=True, exist_ok=True)
        repo_name = Path(result["repo"]).name if result.get("repo") else "?"
        summary.write_text(_summary_markdown(repo_name, result), encoding="utf-8", newline="\n")


def _summary_markdown(repo_name: str, result: dict) -> str:
    rows = [
        "| metric | value |",
        "| --- | --- |",
        f"| repo | {repo_name.replace('|', '/')} |",
        f"| engine | {result.get('engine', '?')} |",
        f"| total mutants | {result.get('total_mutants', 0)} |",
        f"| killed | {result.get('killed', 0)} |",
        f"| survived | {result.get('survived', 0)} |",
        f"| score | {result.get('score_pct', 0.0)}% |",
        f"| timestamp | {result.get('timestamp', '?')} |",
    ]
    return "## Mutation score\n\n" + "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generic mutation executor")
    parser.add_argument("--repo", help="path to the target repository")
    parser.add_argument("--module", help="glob for modules to mutate, e.g. 'src/**/*.py'")
    parser.add_argument("--out", help="result JSON path")
    parser.add_argument("--engine", choices=("auto", "builtin", "mutmut"), default="auto")
    parser.add_argument("--runner", choices=("auto", "pytest", "minimal"), default="auto")
    parser.add_argument("--timeout", type=float, default=60, help="per-mutant test timeout seconds")
    parser.add_argument("--max-mutants", type=int, default=None)
    parser.add_argument("--summary", default=None, help="optional markdown summary output path")
    parser.add_argument("--internal-run-repo", help=argparse.SUPPRESS, default=None)
    args = parser.parse_args(argv)

    if args.internal_run_repo is not None:
        return _minimal_runner_main(Path(args.internal_run_repo))

    missing = [flag for flag, value in (("--repo", args.repo), ("--module", args.module),
                                        ("--out", args.out)) if not value]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"error: repo not found: {repo}", file=sys.stderr)
        return 3

    engine = args.engine
    if engine == "auto":
        engine = "mutmut" if mutmut_available() else "builtin"
    try:
        if engine == "mutmut":
            result = run_mutmut(repo, args.module, Path(args.out), summary=Path(args.summary) if args.summary else None)
        else:
            result = run_builtin(repo, args.module, Path(args.out), runner=args.runner,
                                 per_mutant_timeout=args.timeout, max_mutants=args.max_mutants,
                                 summary=Path(args.summary) if args.summary else None)
    except MutationError as exc:
        if args.engine == "auto":
            print(f"note: mutmut engine failed ({exc}); falling back to builtin", file=sys.stderr)
            result = run_builtin(repo, args.module, Path(args.out), runner=args.runner,
                                 per_mutant_timeout=args.timeout, max_mutants=args.max_mutants,
                                 summary=Path(args.summary) if args.summary else None)
            result["engine_note"] = f"mutmut unavailable ({exc}); builtin operators used"
        else:
            print(f"error: {exc}", file=sys.stderr)
            return 3
    except subprocess.TimeoutExpired:
        print("error: mutmut run exceeded overall timeout", file=sys.stderr)
        return 3

    print(json.dumps({k: result[k] for k in
                      ("engine", "total_mutants", "killed", "survived", "score_pct")
                      if k in result}, ensure_ascii=False))
    if result.get("error"):
        return 2 if result.get("baseline_passed") is False else 3
    if result.get("baseline_passed") is False:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
