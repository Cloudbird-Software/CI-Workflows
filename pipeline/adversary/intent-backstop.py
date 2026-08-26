#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""intent-backstop.py —— 意图层探索道闸 S6-S8（ISSUE-263 AC-16 / ADR-0067 / ADR-0079）

对每张卡的 spec.md 做确定性探索，只报人不阻断：
  S6 重复已有功能（扫描 scripts/ 能力目录）
  S7 违反治理约束（模式命中）
  S8 blastRadius 集合比对（声明集合 vs 全文推断集合；可脱离 LLM 独立运行）

产物（schemas 互异，缺失 = 未运行）：
  intent-backstop.<card>.hit.json      schema=intent-backstop/hit/v1
  intent-backstop.<card>.no-hit.json   schema=intent-backstop/no-hit/v1
  intent-backstop.<card>.skipped.json  schema=intent-backstop/skipped/v1

退出码：0=正常完成（无论命中/无命中）| 2=环境/参数或 S8 判定作废（void）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from typing import Any

try:
    import yaml
except ImportError:  # noqa: BLE001
    print("FATAL: 需要 PyYAML", file=sys.stderr)
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
STRATEGIES_PATH = os.path.join(HERE, "attack-strategies.yaml")

# S8 blastRadius 已知仓全集——对齐 REPOS.yaml status:active 清单（ADR-0085 退役仓不在列）；
# 新仓入图后须同步此处，否则 spec 提及该仓而未声明 blastRadius 会漏报
KNOWN_REPOS = [".github", "CI-Workflows", "template-service", "archive", "arbiter",
               "holdout", "cnb-bridge", "Shorts_Director", "Script_Writer",
               "Use-up-Plan", "AI_Web_School", "mutual", "QW_Arena1",
               "Viral_Radar", "Media-Monitor"]
GOVERNANCE_PATTERNS = [
    (r"新增\s*App\s*身份", "新增 App 身份（与 nonGoals/AG-1 冲突）"),
    (r"new\s+GitHub\s+App\s+identity", "new GitHub App identity（与 AG-1 冲突）"),
    (r"third\s+app\s+identity", "third app identity（与 AG-1 冲突）"),
    (r"绕过\s*红队", "试图绕过红队审计"),
    (r"跳过\s*红队\s*审计", "试图跳过红队审计"),
    (r"每.*PR.*红队审计", "要求每 PR 走红队审计（与 ADR-0067/AC-14 豁免范围冲突）"),
    (r"every\s+PR\s+must\s+be\s+audited\s+by\s+red\s+team", "every-PR red-team audit（与 ADR-0067 scope 冲突）"),
]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_yaml(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_strategies() -> dict[str, dict]:
    data = load_yaml(STRATEGIES_PATH) or {}
    return {s["id"]: s for s in (data.get("strategies") or []) if s.get("id")}


def parse_spec(path: str) -> tuple[dict, str, list[str], int]:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        raise ValueError("spec 缺 YAML frontmatter")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        raise ValueError("spec frontmatter 未闭合")
    fm_text = "\n".join(lines[1:end_idx])
    fm = yaml.safe_load(fm_text) or {}
    body_start = end_idx + 2
    body = "\n".join(lines[end_idx + 1 :])
    return fm, body, lines, body_start


def line_number_for_pos(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def find_occurrences(body: str, body_start: int, pattern: str | re.Pattern) -> list[dict]:
    out = []
    for m in re.finditer(pattern, body):
        line = line_number_for_pos(body, m.start()) + body_start - 1
        snippet = body[m.start() : m.end()]
        out.append({"line": line, "snippet": snippet})
    return out


def build_capability_catalog(repo_root: str) -> dict[str, str]:
    scripts_dir = os.path.join(repo_root, "scripts")
    catalog: dict[str, str] = {}
    if not os.path.isdir(scripts_dir):
        return catalog
    for name in os.listdir(scripts_dir):
        if not name.endswith(".sh"):
            continue
        stem = name[:-3]
        catalog[stem] = os.path.join(scripts_dir, name)
    return catalog


def s6_duplicate_existing(spec_path: str, body: str, body_start: int, repo_root: str) -> list[dict]:
    catalog = build_capability_catalog(repo_root)
    hits = []
    for cap, cap_path in sorted(catalog.items()):
        pattern = r"(?<!\w)" + re.escape(cap) + r"(?!\w)"
        occs = find_occurrences(body, body_start, pattern)
        if occs:
            hits.append({
                "strategy": "S6",
                "name": "重复已有功能",
                "capability": cap,
                "capability_file": cap_path,
                "capability_line": 1,
                "evidence": [{"file": spec_path, "line": o["line"], "snippet": o["snippet"]} for o in occs[:3]],
                "note": f"spec 提到既有能力 '{cap}'，与 {cap_path} 重复",
            })
    return hits


def s7_governance_violation(spec_path: str, body: str, body_start: int) -> list[dict]:
    hits = []
    for pat, note in GOVERNANCE_PATTERNS:
        occs = find_occurrences(body, body_start, pat)
        if occs:
            hits.append({
                "strategy": "S7",
                "name": "违反治理约束",
                "pattern": pat,
                "note": note,
                "evidence": [{"file": spec_path, "line": o["line"], "snippet": o["snippet"]} for o in occs[:3]],
            })
    return hits


def s8_blast_radius(spec_path: str, fm: dict, full_text: str, lines: list[str]) -> tuple[list[dict], list[dict] | None]:
    br = fm.get("blastRadius")
    if not isinstance(br, list):
        return [], [{"strategy": "S8", "name": "blastRadius 集合失真", "reason": "frontmatter 缺 blastRadius 列表", "void": True}]

    declared: set[str] = set()
    for item in br:
        if isinstance(item, dict):
            repo = item.get("repo")
            if repo:
                declared.add(repo)
        elif isinstance(item, str):
            declared.add(item.split(":", 1)[0].split("/", 1)[0])

    expected: set[str] = set()
    for repo in KNOWN_REPOS:
        pat = r"(?<!\w)" + re.escape(repo) + r"(?!\w)"
        if re.search(pat, full_text):
            expected.add(repo)

    def run_once(order: list[str]) -> dict:
        exp = {r for r in order if r in expected}
        missing = sorted(exp - declared)
        extra = sorted(declared - exp)
        return {"missing": missing, "extra": extra, "declared": sorted(declared), "expected": sorted(exp)}

    r1 = run_once(KNOWN_REPOS)
    r2 = run_once(list(reversed(KNOWN_REPOS)))
    if r1 != r2:
        return [], [{"strategy": "S8", "name": "blastRadius 集合失真", "reason": "两次 S8 集合比对结果不一致（void）", "void": True, "run1": r1, "run2": r2}]

    missing, extra = r1["missing"], r1["extra"]
    if missing or extra:
        br_line = next((i + 1 for i, ln in enumerate(lines) if ln.strip().startswith("blastRadius:")), 1)
        hits = [{
            "strategy": "S8",
            "name": "blastRadius 集合失真",
            "missing": missing,
            "extra": extra,
            "declared": r1["declared"],
            "expected": r1["expected"],
            "evidence": [{"file": spec_path, "line": br_line, "snippet": f"blastRadius 声明仓库 {r1['declared']}"}],
            "note": f"声明集合与全文推断集合不一致：缺 {missing} / 多 {extra}",
        }]
        return hits, []
    return [], []


def write_artifact(out_dir: str, card: str, suffix: str, data: dict) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"intent-backstop.{card}.{suffix}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="意图层探索道闸 S6-S8")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--out-dir", default="./intent-backstop-reports")
    ap.add_argument("--card-id", default="")
    args = ap.parse_args()

    if not os.path.isfile(args.spec):
        print(f"FATAL: spec 文件不存在：{args.spec}", file=sys.stderr)
        return 2
    if not os.path.isdir(args.repo_root):
        print(f"FATAL: repo-root 不存在：{args.repo_root}", file=sys.stderr)
        return 2

    strategies = load_strategies()
    for sid in ("S6", "S7", "S8"):
        if sid not in strategies:
            print(f"FATAL: attack-strategies.yaml 缺 {sid}", file=sys.stderr)
            return 2

    try:
        fm, body, lines, body_start = parse_spec(args.spec)
    except Exception as e:  # noqa: BLE001
        print(f"FATAL: 解析 spec 失败：{e}", file=sys.stderr)
        return 2

    card = args.card_id or str(fm.get("taskId") or "unknown")
    ts = now_iso()

    hits: list[dict] = []
    skipped: list[dict] = []

    hits.extend(s6_duplicate_existing(args.spec, body, body_start, args.repo_root))
    hits.extend(s7_governance_violation(args.spec, body, body_start))

    full_text = "\n".join(lines)
    s8_hits, s8_skipped = s8_blast_radius(args.spec, fm, full_text, lines)
    hits.extend(s8_hits)
    if s8_skipped:
        skipped.extend(s8_skipped)

    written = []
    if hits:
        written.append(write_artifact(args.out_dir, card, "hit", {
            "schema": "intent-backstop/hit/v1",
            "ts": ts,
            "card": card,
            "strategies_run": ["S6", "S7", "S8"],
            "hit_count": len(hits),
            "hits": hits,
        }))
    else:
        written.append(write_artifact(args.out_dir, card, "no-hit", {
            "schema": "intent-backstop/no-hit/v1",
            "ts": ts,
            "card": card,
            "strategies_run": ["S6", "S7", "S8"],
            "note": "S6-S8 均未发现意图层问题",
        }))

    if skipped:
        written.append(write_artifact(args.out_dir, card, "skipped", {
            "schema": "intent-backstop/skipped/v1",
            "ts": ts,
            "card": card,
            "skipped": skipped,
        }))

    for w in written:
        print(w)

    print(f"\n意图道闸完成：card={card} hits={len(hits)} skipped={len(skipped)}", file=sys.stderr)

    if any(s.get("void") for s in skipped):
        print("ERROR: S8 判定作废（两次运行结果不一致或 frontmatter 缺失）", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
