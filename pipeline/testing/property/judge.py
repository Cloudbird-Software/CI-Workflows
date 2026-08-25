#!/usr/bin/env python3
"""Mutation judge: cross-anchoring properties against surviving mutants.

A property that no mutant can kill is trivial: it cannot distinguish the
correct implementation from broken ones, so it is rejected. Judgment is a
mechanical count of kills - no model calls.

Inputs:
    --pool   directory containing mutation_result.json (or the JSON file
             itself) produced by run_mutation.py with the builtin engine
             (survivor records must carry file + site_index for re-application).
    --props  invariants manifest YAML.
    --repo   optional repo override (default: the repo recorded in the pool).

Procedure (fully logged, line by line):
    1. baseline: every property runs on the pristine repo copy; properties
       that fail or error here are rejected with reason "baseline_failed".
    2. anchoring: each surviving mutant is re-applied on a scratch copy and
       every surviving property re-executes against it (fixed seed => the
       same inputs per property across all mutants). A property "kills" a
       mutant when its status is "failed" on that mutant.
    3. verdict: kill_count == 0 -> "trivial" (rejected); otherwise accepted
       with the killed-mutant list.

Outputs (in --out, default ./judge_out):
    judged.json     full verdict per property + kill lists
    rejected.json   [{name, reason, kill_count}]
    judge_log.jsonl one line per (baseline|mutant, property) outcome

Exit code 0 even when properties are rejected (a finding, not a tool fault).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent.parent / "mutation") not in sys.path:
    sys.path.insert(0, str(_HERE.parent.parent / "mutation"))
try:
    import run_mutation as rm
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(_HERE.parent / "mutation"))
    import run_mutation as rm

try:
    import invariants as inv
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(_HERE))
    import invariants as inv

INVARIANTS_CLI = _HERE / "invariants.py"
POOL_FILENAMES = ("mutation_result.json", "result.json", "mutation-result.json")
REAPPLICABLE_FIELDS = ("file", "site_index", "op")


class JudgeError(Exception):
    """Raised when the pool cannot be anchored."""


def load_pool(pool: Path) -> dict:
    pool = Path(pool)
    if pool.is_dir():
        for name in POOL_FILENAMES:
            candidate = pool / name
            if candidate.is_file():
                pool = candidate
                break
        else:
            raise JudgeError(f"no result JSON ({'/'.join(POOL_FILENAMES)}) in {pool}")
    return json.loads(pool.read_text(encoding="utf-8"))


def survivors_of(pool: dict) -> list[dict]:
    survivors = pool.get("survivors") or [
        m for m in pool.get("mutants", []) if m.get("status") == "survived"]
    if not survivors:
        return []
    missing = [s.get("id", f"#{i}") for i, s in enumerate(survivors)
               if not all(f in s for f in REAPPLICABLE_FIELDS)]
    if missing:
        raise JudgeError(
            "pool survivors lack re-application fields (file/site_index/op); "
            f"affected: {missing}. Re-run run_mutation.py with the builtin engine "
            "to enable cross-anchoring (mutmut pools are scores-only).")
    return survivors


def _run_invariants(workspace: Path, manifest: Path, out_json: Path,
                    iterations: int, timeout: float) -> dict:
    cmd = [sys.executable, str(INVARIANTS_CLI),
           "--manifest", str(manifest),
           "--repo", str(workspace),
           "--out", str(out_json),
           "--iterations", str(iterations),
           "--seed", "judge",
           "--engine", "random"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"_timeout": True, "each": []}
    if proc.returncode not in (0, 1):
        return {"_crash": True, "stderr": proc.stderr[-500:], "each": []}
    try:
        return json.loads(out_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"_crash": True, "stderr": "invariants output unreadable", "each": []}


def _clear_pycache(workspace: Path) -> None:
    for cache_dir in workspace.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def judge(pool: dict, props_path: Path, repo: Path | None = None,
          out_dir: Path = Path("judge_out"), iterations: int = 100,
          timeout: float = 60) -> dict:
    repo = Path(repo or pool["repo"]).resolve()
    if not repo.is_dir():
        raise JudgeError(f"repo not found: {repo}")
    survivors = survivors_of(pool)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "judge_log.jsonl"

    kills: dict[str, list[str]] = {}
    baseline_status: dict[str, str] = {}
    log_lines: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="judge-") as tmp:
        workspace = rm.make_workcopy(repo, Path(tmp) / "repo")
        result_json = Path(tmp) / "inv.json"

        # 1. baseline pass on the pristine copy
        specs = inv.load_manifest(props_path)
        _clear_pycache(workspace)
        baseline = _run_invariants(workspace, props_path, result_json, iterations, timeout)
        baseline_timed_out = bool(baseline.get("_timeout")) or bool(baseline.get("_crash"))
        for entry in baseline.get("each", []):
            status = "timeout" if baseline_timed_out else entry.get("status", "error")
            baseline_status[entry["name"]] = status
            kills.setdefault(entry["name"], [])
            log_lines.append({"phase": "baseline", "mutant": None,
                              "property": entry["name"], "outcome": status,
                              "killed": False})
        if len(baseline_status) != len(specs):
            raise JudgeError(
                f"baseline invariants run returned {len(baseline_status)} of "
                f"{len(specs)} properties; refusing to judge a blind pool")

        # 2. anchor every surviving mutant
        for survivor in survivors:
            rel = survivor["file"]
            work_file = workspace / rel
            try:
                source = work_file.read_text(encoding="utf-8")
                mutated = rm.mutate_at(source, int(survivor["site_index"]))
            except (OSError, rm.MutationError) as exc:
                for name in baseline_status:
                    log_lines.append({"phase": "mutant", "mutant": survivor.get("id"),
                                      "property": name, "outcome": f"not_applied:{exc}",
                                      "killed": False})
                continue
            original = source
            work_file.write_text(mutated, encoding="utf-8", newline="\n")
            _clear_pycache(workspace)
            outcome = _run_invariants(workspace, props_path, result_json, iterations, timeout)
            timed_out = bool(outcome.get("_timeout"))
            per_property = {e["name"]: e.get("status", "error")
                            for e in outcome.get("each", [])}
            for name, status in baseline_status.items():
                if name not in per_property:
                    mutant_status = "timeout" if timed_out else "missing"
                else:
                    mutant_status = per_property[name]
                killed = mutant_status == "failed"
                if killed:
                    kills[name].append(survivor.get("id", f"{rel}:{survivor['site_index']}"))
                log_lines.append({"phase": "mutant", "mutant": survivor.get("id"),
                                  "property": name, "outcome": mutant_status,
                                  "killed": killed})
            work_file.write_text(original, encoding="utf-8", newline="\n")
            _clear_pycache(workspace)

    # 3. verdicts
    properties = []
    rejected = []
    for name, status in baseline_status.items():
        killed = kills.get(name, [])
        if status != "passed":
            reason = "baseline_failed"
        elif not killed:
            reason = "trivial"
        else:
            reason = None
        record = {"name": name, "baseline": status, "kill_count": len(killed),
                  "killed_mutants": killed, "status": "rejected" if reason else "accepted"}
        if reason:
            record["reason"] = reason if status != "passed" else \
                "trivial: killed 0 of {} surviving mutants".format(len(survivors))
            rejected.append({"name": name, "reason": record["reason"],
                             "kill_count": len(killed),
                             "baseline": status})
        properties.append(record)

    verdict = {
        "schema": "judge-verdict/1",
        "pool_repo": str(repo),
        "surviving_mutants": len(survivors),
        "iterations": iterations,
        "accepted": [p["name"] for p in properties if p["status"] == "accepted"],
        "rejected": rejected,
        "properties": properties,
    }
    (out_dir / "judged.json").write_text(
        json.dumps(verdict, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n")
    (out_dir / "rejected.json").write_text(
        json.dumps(rejected, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n")
    with log_path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in log_lines:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mutation judge (cross anchoring)")
    parser.add_argument("--pool", required=True, help="mutation result dir or JSON file")
    parser.add_argument("--props", required=True, help="invariants manifest YAML")
    parser.add_argument("--repo", default=None, help="override repo path")
    parser.add_argument("--out", default="judge_out", help="output directory")
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args(argv)

    try:
        pool = load_pool(Path(args.pool))
        verdict = judge(pool, Path(args.props), repo=Path(args.repo) if args.repo else None,
                        out_dir=Path(args.out), iterations=args.iterations,
                        timeout=args.timeout)
    except (JudgeError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({
        "surviving_mutants": verdict["surviving_mutants"],
        "accepted": verdict["accepted"],
        "rejected": [r["name"] for r in verdict["rejected"]],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
