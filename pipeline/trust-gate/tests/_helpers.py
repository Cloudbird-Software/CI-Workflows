# -*- coding: utf-8 -*-
"""trust-gate 自测共享构件（W5-C2 .github#225 / ADR-0071）——fixture 驱动零网络。"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GATE_DIR = os.path.dirname(HERE)
PREDICATES = os.path.join(GATE_DIR, "predicates.yaml")
UNLOCK_STATE = os.path.join(GATE_DIR, "unlock-state.yaml")
ENGINE = os.path.join(GATE_DIR, "trust_gate.py")

sys.path.insert(0, GATE_DIR)
import trust_gate  # noqa: E402

PY = sys.executable or "python3"


def all_ev(domain: str, predicates: dict | None = None) -> dict:
    """某域全绿证据清单（谓词键全 true）——「证据齐全」形态的构造器。

    表外/排除域（不在可解锁表）取全部域证据键的超集——证明排除判定与证据无关。
    """
    predicates = predicates or trust_gate.load_predicates(PREDICATES)
    if domain in predicates["domains"]:
        keys = predicates["domains"][domain]["requires"]
    else:
        keys = [k for spec in predicates["domains"].values() for k in spec["requires"]]
    return {k: True for k in keys}


def bundle(domain: str = "docs-only", pr: int = 1, repo: str = "ORG/REPO",
           checks: dict | None = None, evidence: dict | None = None,
           breaker: bool = False, trap: bool = False) -> dict:
    return {
        "repo": repo, "pr": pr, "head_sha": "f" * 40, "domain": domain,
        "checks": checks if checks is not None else {"gate": "success"},
        "evidence": evidence if evidence is not None else all_ev(domain),
        "breaker_tripped": breaker, "trap": trap,
    }


def decision_rec(pr: int, domain: str, decision: str, ts: str = "2026-08-22T00:00:00Z",
                 trap: bool = False, repo: str = "ORG/REPO") -> dict:
    """record=decision JSONL 行（shadow-record 产出的形态）。"""
    return {"schema": "trust-shadow/v1", "record": "decision", "ts": ts, "repo": repo,
            "pr": pr, "head_sha": "f" * 40, "run_id": "r", "event": "pull_request",
            "domain": domain, "mode": "shadow", "decision": decision, "reason": "x",
            "missing_predicates": [], "required_predicates": [], "breaker_tripped": False,
            "trap": trap, "sample_review": False, "executed": False}


def ruling_rec(pr: int, ruling: str, ts: str = "2026-08-22T12:00:00Z",
               repo: str = "ORG/REPO") -> dict:
    """record=ruling JSONL 行（owner 实际裁决导出形态：merged/closed）。"""
    return {"schema": "trust-shadow/v1", "record": "ruling", "ts": ts, "repo": repo,
            "pr": pr, "ruling": ruling, "by": "randypanding"}


def reconciled(pr: int, domain: str, shadow: str, owner_ruling: str, trap: bool = False,
               ts: str = "2026-08-22T12:00:00Z") -> dict:
    """直接构造已比对记录（reconcile 产物形态）——解锁判定的输入单元。"""
    shadow_verdict = "merge" if shadow in ("would-merge", "auto-merge") else "reject"
    owner = "merge" if owner_ruling == "merged" else "reject"
    rec = {"schema": "trust-shadow/v1", "record": "reconcile", "ts": ts, "repo": "ORG/REPO",
           "pr": pr, "domain": domain, "shadow_decision": shadow,
           "owner_ruling": owner_ruling, "counted": True,
           "agreement": shadow_verdict == owner,
           "escape": shadow_verdict == "merge" and owner == "reject",
           "trap": trap, "trap_passed_by_predicate": False, "trap_released_by_owner": False}
    if trap:
        rec["trap_passed_by_predicate"] = shadow_verdict == "merge"
        rec["trap_released_by_owner"] = owner == "merge"
        if rec["trap_passed_by_predicate"] and owner == "reject":
            rec["escape"] = True
    return rec


def run_cli(args: list, expect_rc: set | None = None) -> subprocess.CompletedProcess:
    """跑引擎 CLI（同解释器），返回 CompletedProcess；expect_rc 断言退出码集合。"""
    cp = subprocess.run([PY, ENGINE] + args, capture_output=True, text=True,
                        encoding="utf-8", cwd=GATE_DIR)
    if expect_rc is not None:
        assert cp.returncode in expect_rc, (
            f"CLI rc={cp.returncode} 不在 {expect_rc}\nstdout={cp.stdout}\nstderr={cp.stderr}")
    return cp


def write_jsonl(path: str, recs: list) -> str:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def tmpdir() -> str:
    return tempfile.mkdtemp(prefix="trust-gate-test-")
