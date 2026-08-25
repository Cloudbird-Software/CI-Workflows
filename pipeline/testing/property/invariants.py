#!/usr/bin/env python3
"""Property-invariant registry and executor (IR-0004 AC-2).

Reads a YAML manifest and executes each declared property against the
target module. hypothesis is used when importable (probed); otherwise the
property runs as a seeded random loop with N draws per entry (default 200).
No third-party module is ever a hard dependency.

Manifest schema (block YAML; a bare top-level list is also accepted):
    properties:
      - name: add-scores-commutative
        target_module: graderlib        # importable from --repo
        target_func: add_scores
        property: commutative           # commutative|idempotent|roundtrip|invariant
        gen:
          type: int                     # int|float|str|bool|*_list
          params:
            min: -50
            max: 50

Extra keys:
    roundtrip: "inverse" may come from gen.params.inverse or a top-level
               "inverse:" key (function in target_module); without an
               inverse the target function must be an involution.
    invariant: gen.params.check names a function in target_module called as
               check(result, *args); the property holds while it is truthy.
    gen.params.arity overrides the argument count (default: 2 for
               commutative, else 1).
    *_list gens take min_size/max_size for the list length; an optional
               nested {item: {type, params}} overrides element generation
               (default: elements share the outer params).

Output JSON: {engine, iterations, repo, passed, failed, error,
             each: [{name, property, target, status, ...}]}
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import math
import random
import string
import sys
from pathlib import Path

PROPERTY_KINDS = ("commutative", "idempotent", "roundtrip", "invariant")
GENERATOR_TYPES = ("int", "float", "str", "bool", "int_list", "float_list", "str_list")
DEFAULT_ITERATIONS = 200


class ManifestError(Exception):
    """Raised when the manifest cannot be parsed or validated."""


# --------------------------------------------------------------------------
# YAML loading: PyYAML when present, otherwise a minimal block-style parser.
# The mini parser supports nested mappings, lists of mappings/scalars, and
# int/float/bool/null/quoted-string scalars with # comments. Flow style
# ({...}, [...]) and anchors are not supported - keep manifests block-style.
# --------------------------------------------------------------------------

def yaml_load(text: str):
    try:
        import yaml  # type: ignore
    except ImportError:
        return _mini_yaml_load(text)
    return yaml.safe_load(text)


def _strip_comment(line: str) -> str:
    out = []
    quote = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _parse_scalar(token: str):
    token = token.strip()
    if token == "":
        return None
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    low = token.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", "none"):
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _looks_like_key(token: str) -> bool:
    quote = None
    for i, ch in enumerate(token):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == ":":
            rest = token[i + 1:]
            return rest == "" or rest.startswith(" ")
    return False


def _mini_yaml_load(text: str):
    lines = []
    for raw in text.splitlines():
        stripped_comment = _strip_comment(raw.replace("\t", "    "))
        if not stripped_comment.strip():
            continue
        if stripped_comment.strip() == "---":
            continue
        indent = len(stripped_comment) - len(stripped_comment.lstrip(" "))
        lines.append([indent, stripped_comment.strip()])
    if not lines:
        return None
    value, _ = _parse_block(lines, 0, lines[0][0])
    return value


def _parse_block(lines: list, i: int, indent: int):
    if lines[i][1] in ("-",) or lines[i][1].startswith("- "):
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines: list, i: int, indent: int):
    result = {}
    while i < len(lines) and lines[i][0] == indent:
        content = lines[i][1]
        if content == "-" or content.startswith("- "):
            break
        key, sep, rest = content.partition(":")
        if not sep or not _looks_like_key(content):
            raise ManifestError(f"mini-yaml: expected 'key: value' at: {content!r}")
        key = key.strip().strip("'\"")
        rest = rest.strip()
        i += 1
        if rest:
            result[key] = _parse_scalar(rest)
        elif i < len(lines) and lines[i][0] > indent:
            value, i = _parse_block(lines, i, lines[i][0])
            result[key] = value
        elif i < len(lines) and lines[i][0] == indent and (
                lines[i][1] == "-" or lines[i][1].startswith("- ")):
            value, i = _parse_list(lines, i, indent)
            result[key] = value
        else:
            result[key] = None
    return result, i


def _parse_list(lines: list, i: int, indent: int):
    items = []
    while i < len(lines) and lines[i][0] == indent and (
            lines[i][1] == "-" or lines[i][1].startswith("- ")):
        after = lines[i][1][1:]
        lead = len(after) - len(after.lstrip(" "))
        content = after.strip()
        if not content:
            i += 1
            if i < len(lines) and lines[i][0] > indent:
                value, i = _parse_block(lines, i, lines[i][0])
                items.append(value)
            else:
                items.append(None)
        elif _looks_like_key(content):
            col = indent + 1 + lead
            saved = lines[i]
            lines[i] = [col, content]
            value, i = _parse_map(lines, i, col)
            items.append(value)
        else:
            items.append(_parse_scalar(content))
            i += 1
    return items, i


# --------------------------------------------------------------------------
# Manifest handling
# --------------------------------------------------------------------------

def load_manifest(path: Path) -> list[dict]:
    return load_manifest_from_text(Path(path).read_text(encoding="utf-8"))


def load_manifest_from_text(text: str) -> list[dict]:
    data = yaml_load(text)
    if isinstance(data, dict):
        data = data.get("properties")
    if data is None:
        raise ManifestError("manifest contains no properties")
    if not isinstance(data, list):
        raise ManifestError("manifest must be a list of properties or {properties: [...]}")
    specs = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ManifestError(f"property #{i} is not a mapping")
        missing = [k for k in ("name", "target_module", "target_func", "property", "gen")
                   if k not in entry]
        if missing:
            raise ManifestError(f"property #{i} ({entry.get('name', '?')}) missing keys {missing}")
        kind = entry["property"]
        if kind not in PROPERTY_KINDS:
            raise ManifestError(
                f"property #{i} ({entry['name']}): unknown property {kind!r}; "
                f"expected one of {PROPERTY_KINDS}")
        gen = entry["gen"]
        if not isinstance(gen, dict) or gen.get("type") not in GENERATOR_TYPES:
            raise ManifestError(
                f"property #{i} ({entry['name']}): gen.type must be one of {GENERATOR_TYPES}")
        params = dict(gen.get("params") or {})
        if not isinstance(params, dict):
            raise ManifestError(f"property #{i} ({entry['name']}): gen.params must be a mapping")
        if kind == "roundtrip" and "inverse" not in params and entry.get("inverse"):
            params["inverse"] = entry["inverse"]
        specs.append({
            "name": str(entry["name"]),
            "target_module": str(entry["target_module"]),
            "target_func": str(entry["target_func"]),
            "property": kind,
            "gen": {"type": gen["type"], "params": params},
        })
    return specs


# --------------------------------------------------------------------------
# Value generators (seeded; identical draws across mutants for the judge)
# --------------------------------------------------------------------------

def draw_value(gen: dict, rng: random.Random):
    gtype = gen["type"]
    p = gen.get("params") or {}
    if gtype == "int":
        return rng.randint(int(p.get("min", 0)), int(p.get("max", 100)))
    if gtype == "float":
        return rng.uniform(float(p.get("min", 0.0)), float(p.get("max", 1.0)))
    if gtype == "str":
        alphabet = str(p.get("alphabet", string.ascii_lowercase))
        lo = int(p.get("min_len", 1))
        hi = max(int(p.get("max_len", 8)), lo)
        n = rng.randint(lo, hi)
        return "".join(rng.choice(alphabet) for _ in range(n))
    if gtype == "bool":
        return rng.random() < 0.5
    if gtype in ("int_list", "float_list", "str_list"):
        base = gtype.rsplit("_", 1)[0]
        lo = int(p.get("min_size", 0))
        hi = max(int(p.get("max_size", 6)), lo)
        n = rng.randint(lo, hi)
        item = p.get("item")
        if isinstance(item, dict) and item.get("type"):
            sub = {"type": str(item["type"]), "params": dict(item.get("params") or {})}
        else:
            sub = {"type": base, "params": p}
        return [draw_value(sub, rng) for _ in range(n)]
    raise ManifestError(f"unknown generator type {gtype!r}")


# --------------------------------------------------------------------------
# Property checks
# --------------------------------------------------------------------------

def _eq(a, b) -> bool:
    """Equality with numeric tolerance and recursive list/tuple support."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    return a == b


def _check_property(spec: dict, module, func, args: list, resolver) -> None:
    kind = spec["property"]
    params = spec["gen"]["params"]
    if kind == "commutative":
        forward = func(*args)
        backward = func(*reversed(args))
        assert _eq(forward, backward), (
            f"commutativity violated: f({args!r})={forward!r} != f({list(reversed(args))!r})={backward!r}")
    elif kind == "idempotent":
        once = func(*args)
        twice = func(once)
        assert _eq(twice, once), (
            f"idempotence violated: f(f({args!r}))={twice!r} != f({args!r})={once!r}")
    elif kind == "roundtrip":
        inverse_name = params.get("inverse")
        if inverse_name:
            inverse_module = resolver(str(params.get("inverse_module") or spec["target_module"]))
            inverse = getattr(inverse_module, str(inverse_name))
            encoded = func(*args)
            assert _eq(inverse(encoded), args[0]), (
                f"roundtrip violated: inverse(f({args!r}))={inverse(encoded)!r} != {args[0]!r}")
        else:
            once = func(*args)
            assert _eq(func(once), args[0]), (
                f"involution violated: f(f({args!r}))={func(once)!r} != {args[0]!r}")
    elif kind == "invariant":
        checker_name = params.get("check")
        if not checker_name:
            raise ManifestError("invariant property requires gen.params.check")
        check_module = resolver(str(params.get("check_module") or spec["target_module"]))
        checker = getattr(check_module, str(checker_name))
        result = func(*args)
        assert checker(result, *args), (
            f"invariant check {checker_name} rejected result {result!r} for args {args!r}")
    else:  # pragma: no cover - closed set enforced at load time
        raise ManifestError(f"unknown property kind {kind!r}")


def _default_arity(spec: dict) -> int:
    return 2 if spec["property"] == "commutative" else 1


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------

def hypothesis_available() -> bool:
    return importlib.util.find_spec("hypothesis") is not None


def _resolve_engine(engine: str) -> str:
    if engine == "auto":
        return "hypothesis" if hypothesis_available() else "random"
    return engine


def _run_random(spec: dict, module, func, iterations: int, seed: str, resolver) -> dict:
    rng = random.Random(f"{seed}|{spec['name']}")
    params = spec["gen"]["params"]
    arity = int(params.get("arity", _default_arity(spec)))
    gen = spec["gen"]
    for _ in range(iterations):
        args = [draw_value(gen, rng) for _ in range(arity)]
        try:
            _check_property(spec, module, func, args, resolver)
        except AssertionError as exc:
            return {"name": spec["name"], "property": spec["property"],
                    "target": f"{spec['target_module']}.{spec['target_func']}",
                    "status": "failed", "iterations": iterations,
                    "counterexample": repr(args), "error": str(exc)}
        except ManifestError as exc:
            return {"name": spec["name"], "property": spec["property"],
                    "target": f"{spec['target_module']}.{spec['target_func']}",
                    "status": "error", "iterations": 0, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - property raised: counts as failed
            return {"name": spec["name"], "property": spec["property"],
                    "target": f"{spec['target_module']}.{spec['target_func']}",
                    "status": "failed", "iterations": iterations,
                    "counterexample": repr(args), "error": f"{type(exc).__name__}: {exc}"}
    return {"name": spec["name"], "property": spec["property"],
            "target": f"{spec['target_module']}.{spec['target_func']}",
            "status": "passed", "iterations": iterations}


def _run_hypothesis(spec: dict, module, func, iterations: int, resolver) -> dict:
    import hypothesis.strategies as st
    from hypothesis import given, settings, HealthCheck

    gtype = spec["gen"]["type"]
    p = spec["gen"]["params"]

    def strategy(t: str, params: dict):
        if t == "int":
            return st.integers(min_value=int(params.get("min", 0)), max_value=int(params.get("max", 100)))
        if t == "float":
            return st.floats(min_value=float(params.get("min", 0.0)),
                             max_value=float(params.get("max", 1.0)),
                             allow_nan=False, allow_infinity=False)
        if t == "str":
            alphabet = str(params.get("alphabet", string.ascii_lowercase))
            return st.text(alphabet=alphabet, min_size=int(params.get("min_len", 1)),
                           max_size=int(params.get("max_len", 8)))
        if t == "bool":
            return st.booleans()
        raise ManifestError(f"unknown strategy {t!r}")

    if gtype.endswith("_list"):
        base = gtype.rsplit("_", 1)[0]
        item = p.get("item")
        if isinstance(item, dict) and item.get("type"):
            item_strategy = strategy(str(item["type"]), dict(item.get("params") or {}))
        else:
            item_strategy = strategy(base, p)
        strat = st.lists(item_strategy, min_size=int(p.get("min_size", 0)),
                         max_size=int(p.get("max_size", 6)))
    else:
        strat = strategy(gtype, p)
    arity = int(p.get("arity", _default_arity(spec)))
    args_strategy = st.tuples(*[strat for _ in range(arity)])

    failure: list[str] = []

    @settings(max_examples=iterations, derandomize=True, database=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(args=args_strategy)
    def _run(args) -> None:
        try:
            _check_property(spec, module, func, list(args), resolver)
        except AssertionError as exc:
            failure.append(str(exc))
            raise

    try:
        _run()
    except Exception as exc:  # noqa: BLE001 - hypothesis re-raises the falsification
        return {"name": spec["name"], "property": spec["property"],
                "target": f"{spec['target_module']}.{spec['target_func']}",
                "status": "failed", "iterations": iterations,
                "error": f"{type(exc).__name__}: {exc}"}
    if failure:  # pragma: no cover - defensive
        return {"name": spec["name"], "property": spec["property"],
                "target": f"{spec['target_module']}.{spec['target_func']}",
                "status": "failed", "iterations": iterations,
                "error": failure[0]}
    return {"name": spec["name"], "property": spec["property"],
            "target": f"{spec['target_module']}.{spec['target_func']}",
            "status": "passed", "iterations": iterations}


def run_property(spec: dict, repo: Path, iterations: int = DEFAULT_ITERATIONS,
                 engine: str = "auto", seed: str = "invariants",
                 module_cache: dict | None = None) -> dict:
    engine = _resolve_engine(engine)
    repo = Path(repo).resolve()
    cache = module_cache if module_cache is not None else {}
    base = {
        "name": spec["name"], "property": spec["property"],
        "target": f"{spec['target_module']}.{spec['target_func']}",
    }
    try:
        module = _load_module(spec["target_module"], repo, cache)
        func = getattr(module, spec["target_func"])
    except Exception as exc:  # noqa: BLE001 - import/attr problems are entry errors
        return {**base, "status": "error", "iterations": 0,
                "error": f"{type(exc).__name__}: {exc}"}
    def resolver(name: str):
        return _load_module(name, repo, cache)

    try:
        if engine == "hypothesis":
            return _run_hypothesis(spec, module, func, iterations, resolver)
        return _run_random(spec, module, func, iterations, seed, resolver)
    except ManifestError as exc:
        return {**base, "status": "error", "iterations": 0, "error": str(exc)}


def _load_module(name: str, repo: Path, cache: dict):
    if name in cache:
        return cache[name]
    repo_str = str(repo)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    module = importlib.import_module(name)
    cache[name] = module
    return module


def run_manifest(specs: list[dict], repo: Path, iterations: int = DEFAULT_ITERATIONS,
                 engine: str = "auto", seed: str = "invariants") -> dict:
    engine = _resolve_engine(engine)
    repo = Path(repo).resolve()
    cache: dict = {}
    each = []
    for spec in specs:
        each.append(run_property(spec, repo, iterations=iterations,
                                 engine=engine, seed=seed, module_cache=cache))
    passed = sum(1 for e in each if e["status"] == "passed")
    failed = sum(1 for e in each if e["status"] == "failed")
    error = sum(1 for e in each if e["status"] == "error")
    return {
        "engine": engine,
        "iterations": iterations,
        "seed": seed,
        "repo": str(repo),
        "passed": passed,
        "failed": failed,
        "error": error,
        "each": each,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Property-invariant executor")
    parser.add_argument("--manifest", required=True, help="YAML manifest path")
    parser.add_argument("--repo", required=True, help="repo whose root goes on sys.path")
    parser.add_argument("--out", required=True, help="result JSON path")
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--seed", default="invariants", help="random-seed namespace")
    parser.add_argument("--engine", choices=("auto", "hypothesis", "random"), default="auto")
    args = parser.parse_args(argv)

    try:
        specs = load_manifest(Path(args.manifest))
    except (ManifestError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    result = run_manifest(specs, Path(args.repo), iterations=args.iterations,
                          engine=args.engine, seed=args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8", newline="\n")
    print(json.dumps({k: result[k] for k in ("engine", "passed", "failed", "error")},
                     ensure_ascii=False))
    return 0 if (result["failed"] == 0 and result["error"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
