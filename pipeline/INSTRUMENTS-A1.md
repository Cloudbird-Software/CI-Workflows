# pipeline/testing - test-effectiveness instruments (IR-0004 AC-1/AC-2)

Three mechanical instruments measuring whether a test suite detects
behavior changes. Python 3.11, stdlib-only at runtime; `mutmut` and
`hypothesis` are probed when installed, with built-in fallbacks.

## 1. Mutation executor - `pipeline/testing/mutation/run_mutation.py`

```
python pipeline/testing/mutation/run_mutation.py --repo repo --module "src/**/*.py" \
    --out result.json [--engine auto|builtin|mutmut] [--runner auto|pytest|minimal]
```

- Engine `auto` uses mutmut when importable, else the builtin engine.
- Builtin engine: 8 AST operators (`comp_swap`, `arith_swap`, `int_bump`,
  `bool_flip`, `str_mutate`, `and_or_swap`, `not_removal`, `in_is_swap`);
  each mutant is written to a scratch copy of the repo and the suite
  re-run once. Survivors carry `file` + `site_index` (re-applicable).
- Runner `auto` prefers pytest; `minimal` is the stdlib fallback (also
  runs plain `test_*` functions). The suite must pass the pristine-copy
  baseline or the run aborts with exit code 2.
- Output JSON: `total_mutants, killed, survived, score_pct, timestamp,
  engine` + `mutants`/`survivors` lists; score = killed/total*100.

## 2. Directed candidate pre-screen - `pipeline/testing/mutation/directed.py`

The LLM side proposes `[{target_file, line_hint, operator}]`; the filter
is purely mechanical. Per candidate: file exists inside the repo -> AST
parses -> a matching operator site sits near `line_hint` (`--window`, 2)
-> the mutation is previewed once against the existing suite
(`preview_killed` recorded). `--mode any|survived|killed` turns the
preview into a policy; ghosts, broken syntax, empty sites and unknown
operators land in `rejected` with reason codes.

## 3. Properties and the judge - `pipeline/testing/property/`

Usage: `invariants.py --manifest props.yaml --repo repo --out out.json
[--iterations 200]`; `judge.py --pool <dir> --props props.yaml`.

Manifest (block YAML; PyYAML optional - a mini parser ships in the module):
entries carry `name, target_module, target_func, property, gen`; `property`
is `commutative` (f(a,b)==f(b,a)), `idempotent` (f(f(x))==f(x)), `roundtrip`
(optional `inverse:`, else involution) or `invariant` (`gen.params.check`
names `check(result, *args)`; `check_module` may point elsewhere - keep
checkers outside the mutated surface). `gen` types: `int, float, str, bool,
int_list, float_list, str_list` (lists take `min_size/max_size`, optional
nested `item`). hypothesis when importable; else a seeded random loop with
N=200 samples per entry (fixed seed => same inputs across mutants).

The judge cross-anchors: every surviving mutant is re-applied on a
scratch copy and each baseline-passing property re-executes against it.
A property kills a mutant when it fails on it; `kill_count == 0` means
the property is trivial - it cannot tell correct code from broken code -
and is rejected into `rejected.json` with the reason. `judge_log.jsonl`
records every (baseline|mutant, property) outcome.

## 4. Score ledger - `pipeline/testing/mutation/ledger.py`

`ledger.py append --ledger scores.jsonl --repo org/x --score-pct 82.14
--engine builtin --survived 4` / `ledger.py verify --ledger scores.jsonl`.
JSONL append-only; each record carries `prev_hash` + `hash` (sha256 over
the canonical record including `prev_hash`); `verify()` pinpoints the
exact tampered line.

## 5. Weekly workflow - `.github/workflows/mutation-weekly.yml`

Reusable (`workflow_call` inputs `target_repo`, `tier`) and self-scheduled
(Sundays 06:17 UTC). Checks out CI-Workflows pinned to `main` plus the
target repo, installs `mutmut==2.4.4`/`pytest==8.3.4`, runs the mutation
executor, appends + verifies the ledger, writes a job-summary table and
uploads score JSON + synced ledger as artifacts. Minimal permissions
(`contents: read`), 30-min timeout, zizmor-safe: no `pull_request_target`,
inputs reach scripts only via `env:`. Note: upload-artifact is also
SHA-pinned (artifacts were a hard requirement); drop it if the
two-action whitelist is absolute.

## IR-0004 acceptance criteria mapping

- AC-1 (mutation signal): `run_mutation.py` computes kill scores;
  `directed.py` filters LLM-proposed candidates; the weekly workflow
  records both in the tamper-evident ledger. All mechanically.
- AC-2 (property signal): `invariants.py` executes properties
  deterministically; `judge.py` rejects trivial ones (zero kills) with
  traceable logs.

## Boundary: LLM proposes, machines decide

The generation side may only produce *candidates*: mutation targets for
`directed.py`, property drafts for `invariants.py`. Every decision - file
existence, AST parseability, kill counts, scores, hash-chain validity,
triviality - is made by deterministic stdlib code. No model call sits on
any judgment path.

## Offline self-tests

`python -m unittest discover -s tests -v`: 55 checks - operators change
behavior; kill-rate arithmetic over the mini fixture repo (strong tests
kill, weak leave survivors); directed rejects ghosts/broken/empty/unknown;
the four generators pass on good code, fail on `buggylib`; the judge
rejects tautologies, accepts killers; the ledger detects tampering.
