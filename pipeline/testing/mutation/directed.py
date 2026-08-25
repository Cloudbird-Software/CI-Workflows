#!/usr/bin/env python3
"""Directed mutation candidate pre-screen (IR-0004 AC-1, mechanical filter).

Input: a JSON candidate list produced by the LLM generation side:
    [{"target_file": "src/core/foo.py", "line_hint": 42, "operator": "arith_swap"}, ...]
Operators accept the eight built-in names (and common aliases) or "any".

Per candidate the following checks run, all mechanical:
    1. file exists inside the repo        -> reject reason "file_not_found"
    2. file parses as Python (AST)        -> reject reason "ast_error"
    3. a mutable operator site exists at
       line_hint +/- window lines with a
       matching operator                  -> reject reason "no_mutation_site"
    4. preview: the single mutation is
       applied on a scratch copy and the
       existing suite runs once; the
       killed/survived outcome is
       recorded ("preview_killed")        -> optional policy filter

CLI:
    python directed.py --repo <path> --candidates <in.json> --out <out.json>
        [--mode any|survived|killed] [--window 2] [--runner auto|pytest|minimal]

Output: {"repo", "mode", "accepted": [...], "rejected": [...],
         "reasons": [{"index", "reason"}], "totals": {...}}
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

try:  # allow both `python directed.py` and package-style imports
    import run_mutation as rm
except ImportError:  # pragma: no cover - script-mode path adjustment
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import run_mutation as rm

REJECT_FILE = "file_not_found"
REJECT_AST = "ast_error"
REJECT_SITE = "no_mutation_site"
REJECT_OPERATOR = "unknown_operator"
REJECT_PREVIEW = "preview_policy"


def normalize_operator(name: str | None) -> str:
    key = (name or "").strip().lower()
    if key not in rm.OPERATOR_ALIASES:
        raise KeyError(name)
    return rm.OPERATOR_ALIASES[key]


def _resolve_inside_repo(repo: Path, target_file: str) -> Path | None:
    candidate = Path(target_file)
    if candidate.is_absolute():
        return None
    resolved = (repo / candidate).resolve()
    try:
        resolved.relative_to(repo)
    except ValueError:
        return None
    return resolved


def find_site(source: str, line_hint: int, operator: str, window: int) -> dict | None:
    """Closest mutable site within +/- window lines (ties: lowest index)."""
    try:
        sites = rm.enumerate_sites(source)
    except SyntaxError:
        return None
    candidates = [s for s in sites if abs(s["line"] - line_hint) <= window]
    if operator != "any":
        candidates = [s for s in candidates if s["op"] == operator]
    if not candidates:
        return None
    candidates.sort(key=lambda s: (abs(s["line"] - line_hint), s["index"]))
    return candidates[0]


class _Workspace:
    """Lazily created scratch copy of the repo, shared by all previews."""

    def __init__(self, repo: Path) -> None:
        self._repo = repo
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._path: Path | None = None

    def get(self) -> Path:
        if self._tmp is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="directed-screen-")
            self._path = rm.make_workcopy(self._repo, Path(self._tmp.name) / "repo")
        return self._path

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
            self._path = None


def screen_candidates(repo: Path, candidates: list[dict], mode: str = "any",
                      window: int = 2, runner: str = "auto") -> dict:
    repo = Path(repo).resolve()
    accepted: list[dict] = []
    rejected: list[dict] = []
    reasons: list[dict] = []
    workspace = _Workspace(repo)
    try:
        for index, cand in enumerate(candidates):
            verdict = _screen_one(repo, cand, index, mode, window, workspace, runner)
            if verdict["ok"]:
                accepted.append(verdict["entry"])
            else:
                rejected.append(verdict["entry"])
                reasons.append({"index": index, "reason": verdict["reason"]})
    finally:
        workspace.close()
    return {
        "repo": str(repo),
        "mode": mode,
        "accepted": accepted,
        "rejected": rejected,
        "reasons": reasons,
        "totals": {"accepted": len(accepted), "rejected": len(rejected)},
    }


def _screen_one(repo: Path, cand: dict, index: int, mode: str, window: int,
                workspace: "_Workspace", runner: str) -> dict:
    target_file = str(cand.get("target_file") or "")
    line_hint = int(cand.get("line_hint") or 0)
    raw_operator = str(cand.get("operator") or "any")

    try:
        operator = normalize_operator(raw_operator)
    except KeyError:
        return {"ok": False, "reason": REJECT_OPERATOR,
                "entry": _base(index, cand, REJECT_OPERATOR,
                               detail=f"unknown operator {raw_operator!r}")}

    resolved = _resolve_inside_repo(repo, target_file)
    if resolved is None or not resolved.is_file():
        return {"ok": False, "reason": REJECT_FILE,
                "entry": _base(index, cand, REJECT_FILE,
                               detail=f"no such file in repo: {target_file!r}")}

    try:
        source = resolved.read_text(encoding="utf-8")
        rm.enumerate_sites(source)  # AST-parseability check
    except (SyntaxError, UnicodeDecodeError) as exc:
        return {"ok": False, "reason": REJECT_AST,
                "entry": _base(index, cand, REJECT_AST, detail=f"AST parse failed: {exc}")}

    site = find_site(source, line_hint, operator, window)
    if site is None:
        return {"ok": False, "reason": REJECT_SITE,
                "entry": _base(index, cand, REJECT_SITE,
                               detail=(f"no {operator} site within +-{window} lines of {line_hint}"))}

    # Preview: apply the single mutation on a scratch copy, run the suite once.
    work_root = workspace.get()
    rel = resolved.relative_to(repo).as_posix()
    work_file = work_root / rel
    mutated = rm.mutate_at(source, site["index"])
    original = work_file.read_text(encoding="utf-8")
    work_file.write_text(mutated, encoding="utf-8", newline="\n")
    run = rm.run_test_suite(work_root, runner=runner)
    work_file.write_text(original, encoding="utf-8", newline="\n")
    preview_killed = run["returncode"] != 0

    entry = _base(index, cand, None)
    entry.update({
        "site": {"line": site["line"], "col": site["col"], "op": site["op"],
                 "index": site["index"], "file": rel},
        "preview_killed": bool(preview_killed),
        "preview": {"runner": run["runner"], "returncode": run["returncode"],
                    "timed_out": run["timed_out"]},
    })

    if mode == "survived" and preview_killed:
        return {"ok": False, "reason": REJECT_PREVIEW,
                "entry": {**entry, "reason": REJECT_PREVIEW,
                          "detail": "mode=survived but preview shows the suite kills this mutation"}}
    if mode == "killed" and not preview_killed:
        return {"ok": False, "reason": REJECT_PREVIEW,
                "entry": {**entry, "reason": REJECT_PREVIEW,
                          "detail": "mode=killed but preview shows the suite does not kill this mutation"}}
    return {"ok": True, "entry": entry}


def _base(index: int, cand: dict, reason: str | None, detail: str | None = None) -> dict:
    entry = {"index": index, "target_file": cand.get("target_file"),
             "line_hint": cand.get("line_hint"), "operator": cand.get("operator")}
    if reason is not None:
        entry["reason"] = reason
    if detail is not None:
        entry["detail"] = detail
    return entry


def load_candidates(path: Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("candidates", [])
    if not isinstance(data, list):
        raise ValueError("candidates JSON must be a list or {\"candidates\": [...]}")
    return [c for c in data if isinstance(c, dict)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Directed mutation candidate pre-screen")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--candidates", required=True, help="JSON file with candidate list")
    parser.add_argument("--out", required=True, help="output JSON path")
    parser.add_argument("--mode", choices=("any", "survived", "killed"), default="any")
    parser.add_argument("--window", type=int, default=2)
    parser.add_argument("--runner", choices=("auto", "pytest", "minimal"), default="auto")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"error: repo not found: {repo}", file=sys.stderr)
        return 3
    try:
        candidates = load_candidates(Path(args.candidates))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: cannot read candidates: {exc}", file=sys.stderr)
        return 3

    result = screen_candidates(repo, candidates, mode=args.mode,
                               window=args.window, runner=args.runner)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8", newline="\n")
    print(json.dumps(result["totals"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
