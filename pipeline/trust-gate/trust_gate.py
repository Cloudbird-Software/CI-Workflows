#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trust_gate.py —— 硬谓词信任门判定引擎（W5-C2 .github#225 / ADR-0071；宪法 §5）

替代 spec v3 risk-score 标量（spec v4 修订 1）：准入是谓词清单不是分数——
合并的充要条件 = 全关卡绿 ∧ 证据齐全 ∧ 属于已解锁域；**缺证据=拒绝，不是中性、
不是降级**（exit 1 且报告逐项列出缺失谓词键）。

零 LLM 纯确定性（宪法 §5/ADR-0071 决策 7：授权判定零 LLM）；零网络——本引擎
只读输入产判定，不调用任何 API（shadow 记录/抽审同样确定性，种子可注入）。

子命令（各自独立可测，fixture 驱动零网络）：
  adjudicate       证据 bundle + predicates.yaml + unlock-state.yaml → 判定 JSON
  shadow-record    判定 JSON → trust-shadow/<date>.jsonl 追加（AC-2 纯记录不执行）
  reconcile        shadow 决策 × owner 实际裁决（merged/closed）比对（AC-2）
  unlock-evaluate  连续 ≥50 例一致+零逃逸+陷阱占比 ≥10% → 解锁/重置判定（AC-3/4）
  sample           解锁后 5% 随机抽审选择（sha256 PRF，种子可注入=可复现）

退出码（arbiter allow/deny/infra 三分约定，API 失败≠通过）：
  0 = 放行形态（auto-merge / would-merge）
  1 = 不放行形态（reject / would-reject / human-sign / sample-review）
  2 = infra：策略表/bundle/记录非法或不可读（fail-closed，不是拒绝也不是放行）

YAML 解析用 runner 预装 PyYAML（pipeline/ocr/postprocess.py 同款约定），
其上做严格 schema 校验：未知键/未知值/类型不符 → exit 2 拒绝启动。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

SCHEMA = "trust-shadow/v1"
PREDICATES_SCHEMA = "trust-gate-predicates/v1"
UNLOCK_SCHEMA = "trust-gate-domains/v1"

# 宪法 §5「明确排除」硬编码底集（excluded_domains 必须至少含这四个——防策略表
# 修订时悄悄删掉排除项；测试钉死）：新功能/依赖升级/公开 API/schema/CI-Workflows
HARDCODED_EXCLUDED = {"new-feature", "dependency-upgrade", "public-api-schema", "ci-workflows"}

# 判定取值域（人类可读即机器可判）
MERGE_DECISIONS = {"auto-merge", "would-merge"}      # 放行形态
HOLD_DECISIONS = {"would-reject", "reject", "human-sign", "sample-review"}
RULINGS = {"merged": "merge", "closed": "reject"}    # owner 裁决 → 与判定同域的比较形态


class TrustGateError(Exception):
    """输入/策略表非法（infra 形态，exit 2）——不是拒绝：拒绝是正常判定结果。"""


# ---------------------------------------------------------------------------
# 加载与严格校验（fail-closed：任何非法 → TrustGateError）
# ---------------------------------------------------------------------------

def _load_yaml(path: str, what: str) -> dict:
    try:
        import yaml  # runner 预装 PyYAML（pipeline/ocr/postprocess.py 同款约定）
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001 - OSError/YAMLError 等统一折叠为 infra（exit 2）
        raise TrustGateError(f"{what} 不可读/不可解析（{path}）: {e}") from e
    if not isinstance(doc, dict):
        raise TrustGateError(f"{what} 顶层须为映射（{path}）")
    return doc


def _str_list(v, where: str) -> list:
    if not isinstance(v, list) or not v or any(not isinstance(x, str) or not x for x in v):
        raise TrustGateError(f"{where} 须为非空字符串列表")
    if len(set(v)) != len(v):
        raise TrustGateError(f"{where} 含重复项")
    return v


def load_predicates(path: str) -> dict:
    """加载并严格校验 predicates.yaml（未知键拒绝——防策略表悄悄漂移）。"""
    doc = _load_yaml(path, "predicates.yaml")
    top = {"schema", "version", "defaults", "gates", "domains", "excluded_domains"}
    unknown = set(doc) - top
    if unknown:
        raise TrustGateError(f"predicates.yaml 未知顶层键 {sorted(unknown)}（合法 {sorted(top)}）")
    if doc.get("schema") != PREDICATES_SCHEMA:
        raise TrustGateError(f"schema 须为 {PREDICATES_SCHEMA}，实际 {doc.get('schema')!r}")
    if not isinstance(doc.get("version"), int) or doc["version"] < 1:
        raise TrustGateError("version 须为 ≥1 整数")
    defaults = doc.get("defaults")
    if not isinstance(defaults, dict):
        raise TrustGateError("缺少 defaults 段")
    dkeys = {"min_consecutive_agreement", "min_trap_ratio", "post_unlock_sample_rate",
             "circuit_breaker_variable"}
    unknown = set(defaults) - dkeys
    if unknown:
        raise TrustGateError(f"defaults 未知键 {sorted(unknown)}")
    mca = defaults.get("min_consecutive_agreement")
    if not isinstance(mca, int) or isinstance(mca, bool) or mca < 1:
        raise TrustGateError("min_consecutive_agreement 须为 ≥1 整数")
    for k in ("min_trap_ratio", "post_unlock_sample_rate"):
        v = defaults.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 < v <= 1:
            raise TrustGateError(f"{k} 须为 (0,1] 数值")
    if not isinstance(defaults.get("circuit_breaker_variable"), str) or not defaults["circuit_breaker_variable"]:
        raise TrustGateError("circuit_breaker_variable 须为非空字符串")
    gates = doc.get("gates")
    if not isinstance(gates, dict) or set(gates) != {"required"}:
        raise TrustGateError("gates 段须恰含 required 键")
    _str_list(gates["required"], "gates.required")
    domains = doc.get("domains")
    if not isinstance(domains, dict) or not domains:
        raise TrustGateError("缺少 domains 段（至少一个可解锁域）")
    for name, spec in domains.items():
        if not isinstance(name, str) or not name.replace("-", "").isalnum() or name != name.lower():
            raise TrustGateError(f"域名非法 {name!r}（小写字母数字连字符）")
        if not isinstance(spec, dict) or set(spec) != {"description", "requires"}:
            raise TrustGateError(f"域 {name} 须恰含 description/requires 键")
        if not isinstance(spec["description"], str) or not spec["description"]:
            raise TrustGateError(f"域 {name}.description 须为非空字符串")
        _str_list(spec["requires"], f"域 {name}.requires")
    excluded = doc.get("excluded_domains")
    if not isinstance(excluded, list):
        raise TrustGateError("excluded_domains 须为列表")
    _str_list(excluded, "excluded_domains")
    missing_hard = HARDCODED_EXCLUDED - set(excluded)
    if missing_hard:
        raise TrustGateError(f"excluded_domains 缺宪法 §5 硬编码排除项 {sorted(missing_hard)}")
    overlap = set(domains) & set(excluded)
    if overlap:
        raise TrustGateError(f"域同时出现在可解锁与排除表 {sorted(overlap)}（语义矛盾）")
    return doc


def load_unlock_state(path: str, predicates: dict) -> dict:
    """加载 unlock-state.yaml；其域集合必须与 predicates.domains 一致（漂移=拒绝）。"""
    doc = _load_yaml(path, "unlock-state.yaml")
    top = {"schema", "updated", "domains"}
    unknown = set(doc) - top
    if unknown:
        raise TrustGateError(f"unlock-state.yaml 未知顶层键 {sorted(unknown)}")
    if doc.get("schema") != UNLOCK_SCHEMA:
        raise TrustGateError(f"schema 须为 {UNLOCK_SCHEMA}，实际 {doc.get('schema')!r}")
    if not isinstance(doc.get("updated"), str) or not doc["updated"]:
        raise TrustGateError("updated 须为非空字符串")
    domains = doc.get("domains")
    if not isinstance(domains, dict) or not domains:
        raise TrustGateError("缺少 domains 段")
    for name, spec in domains.items():
        if not isinstance(spec, dict) or set(spec) != {"status"}:
            raise TrustGateError(f"域 {name} 须恰含 status 键")
        if spec["status"] not in ("locked", "unlocked"):
            raise TrustGateError(f"域 {name}.status 须为 locked|unlocked，实际 {spec['status']!r}")
    pset, sset = set(predicates["domains"]), set(domains)
    if pset != sset:
        raise TrustGateError(f"unlock-state 域集合与 predicates 不一致（差 {sorted(pset ^ sset)}）")
    return {n: domains[n]["status"] for n in domains}


def load_bundle(path: str) -> dict:
    """加载证据 bundle（evidence manifest）并严格校验。"""
    try:
        with open(path, encoding="utf-8") as f:
            b = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise TrustGateError(f"bundle 不可读（{path}）: {e}") from e
    return validate_bundle(b)


def validate_bundle(b: dict) -> dict:
    if not isinstance(b, dict):
        raise TrustGateError("bundle 须为 JSON 对象")
    required = {"repo", "pr", "domain", "checks", "evidence", "breaker_tripped"}
    missing = required - set(b)
    if missing:
        raise TrustGateError(f"bundle 缺必需键 {sorted(missing)}")
    for k in ("repo", "domain"):
        if not isinstance(b[k], str) or not b[k]:
            raise TrustGateError(f"bundle.{k} 须为非空字符串")
    if not isinstance(b["pr"], int) or isinstance(b["pr"], bool) or b["pr"] <= 0:
        raise TrustGateError("bundle.pr 须为正整数（PR 号）")
    for k in ("checks", "evidence"):
        if not isinstance(b[k], dict):
            raise TrustGateError(f"bundle.{k} 须为对象")
        for kk, vv in b[k].items():
            if not isinstance(kk, str) or not kk:
                raise TrustGateError(f"bundle.{k} 键须为非空字符串")
    for kk, vv in b["checks"].items():
        if not isinstance(vv, str):
            raise TrustGateError(f"bundle.checks[{kk!r}] 须为字符串（check 结论）")
    for kk, vv in b["evidence"].items():
        if not isinstance(vv, bool):
            raise TrustGateError(f"bundle.evidence[{kk!r}] 须为布尔（证据键=谓词键，缺位/false=拒绝）")
    if not isinstance(b["breaker_tripped"], bool):
        raise TrustGateError("bundle.breaker_tripped 须为布尔")
    if "trap" in b and not isinstance(b["trap"], bool):
        raise TrustGateError("bundle.trap 须为布尔")
    if "head_sha" in b and not isinstance(b["head_sha"], str):
        raise TrustGateError("bundle.head_sha 须为字符串")
    return b


# ---------------------------------------------------------------------------
# 判定（AC-1：缺证据=拒绝且列出缺失谓词键；ADR-0040 熔断前置 fail-closed）
# ---------------------------------------------------------------------------

def adjudicate(bundle: dict, predicates: dict, unlock: dict,
               sample_prs: set | None = None) -> dict:
    """纯函数判定。返回判定对象（decision/reason/missing_predicates/...）。"""
    domain = bundle["domain"]
    out = {
        "domain": domain, "breaker_tripped": bundle["breaker_tripped"],
        "trap": bool(bundle.get("trap", False)),
        "required_predicates": [], "missing_predicates": [],
        "gates": dict(bundle["checks"]), "executed": False,
        "sample_review": False, "mode": None, "reason": None, "decision": None,
    }

    # 前置 0：熔断（ADR-0040 无人值守护栏——置位即直接拒绝；只准降级为人签，
    # 不准降级为少验。此检查先于一切域/证据判定，防任何路径绕过熔断）
    if bundle["breaker_tripped"]:
        out.update(decision="reject", reason="circuit-breaker-tripped")
        return _apply_mode(out, unlock)

    # 前置 1：排除域（宪法 §5 明确排除——硬编码排除，永远人签；不看证据）
    if domain in predicates["excluded_domains"]:
        out.update(decision="human-sign", reason="excluded-domain")
        return _apply_mode(out, unlock)  # mode=excluded：解锁与否都不自动合并

    # 前置 2：未知域（fail-closed：声明表外域=拒绝，不是放行）
    if domain not in predicates["domains"]:
        out.update(decision="reject", reason="unknown-domain",
                   missing_predicates=["domain.declared-in-predicates"])
        return _apply_mode(out, unlock)

    # 谓词 1：全关卡绿（gates.required 全部存在且结论恰为 success）
    required_gates = predicates["gates"]["required"]
    out["required_predicates"] = [f"gates.{g}" for g in required_gates]
    for g in required_gates:
        if bundle["checks"].get(g) != "success":
            out["missing_predicates"].append(f"gates.{g}")

    # 谓词 2：证据齐全（域 requires 每键必须存在且为 true——缺证据=拒绝）
    req_ev = predicates["domains"][domain]["requires"]
    out["required_predicates"] += list(req_ev)
    for k in req_ev:
        if bundle["evidence"].get(k) is not True:
            out["missing_predicates"].append(k)

    if out["missing_predicates"]:
        out.update(decision="reject", reason="missing-predicates")
        return _apply_mode(out, unlock)

    # 谓词全绿：按解锁状态分形态（locked=shadow 记录不执行；unlocked=auto-merge，
    # 但 5% 抽审命中的 PR 转人审——ADR-0071 决策 5，fail-closed 方向）
    if unlock.get(domain, "locked") == "unlocked":
        if sample_prs is not None and bundle["pr"] in sample_prs:
            out.update(decision="sample-review", reason="post-unlock-sampling",
                       sample_review=True)
        else:
            out.update(decision="auto-merge", reason="predicates-ok")
    else:
        out.update(decision="would-merge", reason="predicates-ok-shadow")
    return _apply_mode(out, unlock)


def _apply_mode(out: dict, unlock: dict) -> dict:
    """按域解锁状态定形态：locked 域只以 would-* 记录（本应合并/本应拒绝，但不执行）。"""
    if out["decision"] == "human-sign":
        out["mode"] = "excluded"   # 排除域：无自动化形态，永远人签
        return out
    if unlock.get(out["domain"], "locked") == "unlocked":
        out["mode"] = "enforced"
        return out
    out["mode"] = "shadow"
    if out["decision"] == "auto-merge":
        out["decision"] = "would-merge"
    elif out["decision"] == "reject":
        out["decision"] = "would-reject"
    return out


# ---------------------------------------------------------------------------
# reconcile（AC-2：shadow 决策 × owner 裁决比对）
# ---------------------------------------------------------------------------

def _read_jsonl(path: str, what: str) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            recs = [json.loads(ln) for ln in f if ln.strip()]
    except (OSError, json.JSONDecodeError) as e:
        raise TrustGateError(f"{what} 不可读/含非法 JSON 行（{path}）: {e}") from e
    for r in recs:
        if not isinstance(r, dict) or r.get("schema") != SCHEMA:
            raise TrustGateError(f"{what} 含 schema 不符记录（须 {SCHEMA}）")
    return recs


def reconcile(decisions: list, rulings: list, unlockable: set) -> tuple:
    """比对 shadow 决策与 owner 裁决 → (reconcile 记录列表, 汇总)。

    语义（卡面 AC 原文）：
    - 一致 = shadow 放行形态 ↔ owner merged，或 shadow 拒绝形态 ↔ owner closed
    - 逃逸 = owner 拒（closed）而谓词放行（would-merge/auto-merge）——零逃逸是解锁必要条件
    - 陷阱（trap=true）两类失败分记：谓词放行了已知应拒样本=trap_passed_by_predicate
      （计入逃逸）；owner 放行了陷阱=trap_released_by_owner（AC-3 计数重置事件）
    - 排除域/human-sign 形态不具可比性：counted=false，不进域计数
    """
    ruling_by_pr = {}
    for r in rulings:
        if r.get("record") != "ruling":
            raise TrustGateError("rulings 输入须为 record=ruling 行")
        ruling = r.get("ruling")
        if ruling not in RULINGS:
            raise TrustGateError(f"ruling 取值非法 {ruling!r}（须 merged|closed）")
        key = (r["repo"], r["pr"])
        if key in ruling_by_pr:
            raise TrustGateError(f"ruling 重复 {key}（append-only 流不允许改判，重开走新记录）")
        ruling_by_pr[key] = r
    records, per_domain = [], {}
    for d in decisions:
        if d.get("record") != "decision":
            raise TrustGateError("decisions 输入须为 record=decision 行")
        key = (d["repo"], d["pr"])
        if key not in ruling_by_pr:
            continue  # 尚无裁决（PR 还开着）——不比对，留待下轮 reconcile
        r = ruling_by_pr[key]
        domain = d.get("domain")
        decision = d.get("decision")
        if decision not in MERGE_DECISIONS | HOLD_DECISIONS:
            raise TrustGateError(f"decision 取值非法 {decision!r}")
        shadow = "merge" if decision in MERGE_DECISIONS else (
            "reject" if decision in ("would-reject", "reject") else None)
        owner = RULINGS[r["ruling"]]
        counted = domain in unlockable and shadow is not None
        rec = {
            "schema": SCHEMA, "record": "reconcile", "ts": r["ts"], "repo": d["repo"],
            "pr": d["pr"], "domain": domain, "shadow_decision": decision,
            "owner_ruling": r["ruling"], "counted": counted,
            "agreement": (shadow == owner) if counted else None,
            "escape": bool(counted and shadow == "merge" and owner == "reject"),
            "trap": bool(d.get("trap", False)),
            "trap_passed_by_predicate": False, "trap_released_by_owner": False,
        }
        if rec["trap"]:
            rec["trap_passed_by_predicate"] = bool(shadow == "merge")   # 已知应拒却被谓词放行
            rec["trap_released_by_owner"] = bool(owner == "merge")      # owner 放行陷阱（AC-3 重置）
            if rec["trap_passed_by_predicate"] and owner == "reject":
                rec["escape"] = True                                    # 逃逸定义覆盖陷阱形态
        records.append(rec)
        if counted:
            st = per_domain.setdefault(domain, {"pairs": 0, "agreements": 0, "escapes": 0, "traps": 0})
            st["pairs"] += 1
            st["agreements"] += 1 if rec["agreement"] else 0
            st["escapes"] += 1 if rec["escape"] else 0
            st["traps"] += 1 if rec["trap"] else 0
    counted_total = sum(s["pairs"] for s in per_domain.values())
    summary = {
        "pairs": len(records), "counted_pairs": counted_total, "per_domain": per_domain,
        "escapes": sum(s["escapes"] for s in per_domain.values()),
        "traps": sum(s["traps"] for s in per_domain.values()),
    }
    return records, summary


# ---------------------------------------------------------------------------
# unlock-evaluate（AC-3/AC-4：连续 ≥50 一致+零逃逸+陷阱 ≥10% → 解锁；逃逸/陷阱放行→重置）
# ---------------------------------------------------------------------------

def evaluate_unlock(records: list, params: dict, state: dict) -> tuple:
    """按 ts 稳定序重放 reconcile 记录 → (事件列表, 汇总, 新状态)。

    重放规则（ADR-0071 决策 3/4）：一致 → 连击+1；任一不一致（含逃逸、owner 放行
    陷阱）→ 连击清零并落 reset 事件。终态连击 ≥50 且窗口内零逃逸且陷阱占比 ≥10%
    且当前 locked → 解锁（unlocked 事件）。窗口内陷阱占比不足 10% = 样本不合格，
    不解锁（fail-closed：不能靠无陷阱的干净流自证合格——陷阱是免疫系统的疫苗）。
    """
    ordered = sorted(records, key=lambda r: r["ts"])  # ts 同值保持输入序（稳定排序）
    events, summary = [], {}
    for domain in sorted(state):
        streak, run = 0, []
        for r in ordered:
            if r.get("domain") != domain or not r.get("counted", False):
                continue
            if r.get("escape"):
                streak, run = 0, []
                events.append(_ev(domain, "reset-escape", 0, r))
            elif r.get("trap_released_by_owner"):
                streak, run = 0, []          # AC-3：owner 放行陷阱 → 计数重置并记录
                events.append(_ev(domain, "reset-trap", 0, r))
            elif r.get("agreement") is True:
                streak += 1
                run.append(r)
            else:
                streak, run = 0, []          # 普通不一致（owner 合而谓词拒等）→ 清零
                events.append(_ev(domain, "reset-disagreement", 0, r))
        traps = sum(1 for r in run if r.get("trap"))
        ratio = (traps / len(run)) if run else 0.0
        before = state[domain]
        after = before
        unlocked = (before == "locked" and streak >= params["min_consecutive_agreement"]
                    and len(run) == streak and all(not r.get("escape") for r in run)
                    and ratio >= params["min_trap_ratio"])
        if unlocked:
            after = "unlocked"
            events.append(_ev(domain, "unlocked", streak, None,
                              {"window": streak, "traps_in_window": traps,
                               "trap_ratio": round(ratio, 4)}))
        summary[domain] = {
            "status_before": before, "status_after": after, "streak": streak,
            "escapes_in_window": sum(1 for r in run if r.get("escape")),
            "traps_in_window": traps, "trap_ratio": round(ratio, 4),
            "min_consecutive_agreement": params["min_consecutive_agreement"],
            "min_trap_ratio": params["min_trap_ratio"],
        }
    return events, summary, {d: summary[d]["status_after"] for d in summary}


def _ev(domain: str, kind: str, streak: int, src: dict | None, extra: dict | None = None) -> dict:
    e = {"schema": SCHEMA, "record": "unlock", "ts": _now(), "domain": domain,
         "event": kind, "streak_after": streak}
    if src:
        e["trigger"] = {"repo": src.get("repo"), "pr": src.get("pr"), "ts": src.get("ts")}
    if extra:
        e.update(extra)
    return e


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# sample（解锁后 5% 抽审——sha256 PRF，种子可注入=判定可复现）
# ---------------------------------------------------------------------------

def select_sample(prs: list, domain: str, rate: float, seed: str) -> dict:
    """确定性抽样：sha256(seed|domain|pr) %10000 < rate*10000。

    不用 random 模块（Mersenne Twister 跨版本稳定性无承诺）；sha256 是 PRF，
    同 (seed, domain, pr) 永远同结果——审计者可用同种子复现整次抽样。
    """
    if not prs or any(not isinstance(p, int) or isinstance(p, bool) or p <= 0 for p in prs):
        raise TrustGateError("prs 须为非空正整数列表")
    if not 0 < rate <= 1:
        raise TrustGateError("rate 须为 (0,1]")
    threshold = int(rate * 10000)
    selected = [p for p in prs if int(
        hashlib.sha256(f"{seed}|{domain}|{p}".encode()).hexdigest(), 16) % 10000 < threshold]
    return {"schema": SCHEMA, "record": "sample", "ts": _now(), "domain": domain,
            "seed": seed, "rate": rate, "total": len(prs), "selected": selected,
            "selected_ratio": round(len(selected) / len(prs), 4) if prs else 0.0}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit(obj: dict, out_path: str | None) -> None:
    text = json.dumps(obj, ensure_ascii=False, sort_keys=False)
    print(text)
    if out_path:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")


def _append_jsonl(path: str, recs: list) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def cmd_adjudicate(a) -> int:
    predicates = load_predicates(a.predicates)
    unlock = load_unlock_state(a.unlock_state, predicates)
    bundle = load_bundle(a.bundle)
    sample_prs = None
    if a.sample_file:
        try:
            with open(a.sample_file, encoding="utf-8") as f:
                sample_prs = set(json.load(f)["selected"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            raise TrustGateError(f"sample 文件不可用（{a.sample_file}）: {e}") from e
    if a.trap:
        bundle["trap"] = True
    out = adjudicate(bundle, predicates, unlock, sample_prs)
    out.update(repo=bundle["repo"], pr=bundle["pr"],
               head_sha=bundle.get("head_sha", ""), ts=_now())
    _emit(out, a.out)
    if out["missing_predicates"]:
        print(f"::error::缺证据=拒绝（不是中性）——缺失谓词键: {', '.join(out['missing_predicates'])}",
              file=sys.stderr)
    return 0 if out["decision"] in MERGE_DECISIONS else 1


def cmd_shadow_record(a) -> int:
    try:
        with open(a.decision, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("decision") not in MERGE_DECISIONS | HOLD_DECISIONS:
            raise ValueError(f"decision 取值非法 {d.get('decision')!r}")
        if not str(a.pr).isdigit():
            raise ValueError("--pr 须为数字")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        raise TrustGateError(f"decision 文件不可用（{a.decision}）: {e}") from e
    date = a.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rec = {"schema": SCHEMA, "record": "decision", "ts": d.get("ts") or _now(),
           "repo": a.repo, "pr": int(a.pr), "head_sha": a.head_sha, "run_id": a.run_id,
           "event": a.event, "domain": d.get("domain"), "mode": d.get("mode"),
           "decision": d["decision"], "reason": d.get("reason"),
           "missing_predicates": d.get("missing_predicates", []),
           "required_predicates": d.get("required_predicates", []),
           "breaker_tripped": d.get("breaker_tripped", False),
           "trap": d.get("trap", False), "sample_review": d.get("sample_review", False),
           "executed": False}   # AC-2：纯记录，引擎永不执行合并
    out = os.path.join(a.out_dir, f"{date}.jsonl")
    _append_jsonl(out, [rec])
    print(f"shadow 记录 → {out}（decision={rec['decision']} executed=false，schema {SCHEMA}）")
    return 0


def cmd_reconcile(a) -> int:
    predicates = load_predicates(a.predicates)
    decisions = _read_jsonl(a.decisions, "decisions JSONL")
    rulings = _read_jsonl(a.rulings, "rulings JSONL")
    records, summary = reconcile(decisions, rulings, set(predicates["domains"]))
    if a.out:
        _append_jsonl(a.out, records)
    if a.summary:
        _write_json(a.summary, summary)
    else:
        print(json.dumps(summary, ensure_ascii=False))
    print(f"reconcile: pairs={summary['pairs']} counted={summary['counted_pairs']} "
          f"escapes={summary['escapes']} traps={summary['traps']}")
    return 0


def _write_json(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def cmd_unlock_evaluate(a) -> int:
    predicates = load_predicates(a.predicates)
    state = load_unlock_state(a.unlock_state, predicates)
    records = _read_jsonl(a.reconcile, "reconcile JSONL")
    for r in records:
        if r.get("record") != "reconcile":
            raise TrustGateError("输入须为 record=reconcile 行")
    params = predicates["defaults"]
    events, summary, new_state = evaluate_unlock(records, params, state)
    if a.out_events:
        _append_jsonl(a.out_events, events)
    _write_json(a.summary, {"evaluated_at": _now(), "domains": summary})
    if a.write_state:
        with open(a.write_state, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"schema: {UNLOCK_SCHEMA}\nupdated: \"{_now()[:10]}\"\ndomains:\n")
            for n in sorted(new_state):
                f.write(f"  {n}:\n    status: {new_state[n]}\n")
    changed = [d for d in summary if summary[d]["status_after"] != summary[d]["status_before"]]
    print(f"unlock-evaluate: streaks=" +
          ",".join(f"{d}:{summary[d]['streak']}" for d in sorted(summary)) +
          (f" 状态变更={','.join(changed)}" if changed else " 无状态变更"))
    return 0


def cmd_sample(a) -> int:
    try:
        prs = [int(x) for x in a.prs.split(",") if x.strip()]
    except ValueError as e:
        raise TrustGateError(f"--prs 须为逗号分隔 PR 号: {e}") from e
    predicates = load_predicates(a.predicates)
    rate = a.rate if a.rate is not None else predicates["defaults"]["post_unlock_sample_rate"]
    result = select_sample(prs, a.domain, rate, a.seed)
    _emit(result, a.out)
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="信任门硬谓词判定引擎（ADR-0071，零 LLM）")
    here = os.path.dirname(os.path.abspath(__file__))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("adjudicate", help="证据 bundle → 判定（缺证据=拒绝 exit 1）")
    p.add_argument("--bundle", required=True, help="evidence bundle JSON 路径")
    p.add_argument("--predicates", default=os.path.join(here, "predicates.yaml"))
    p.add_argument("--unlock-state", default=os.path.join(here, "unlock-state.yaml"))
    p.add_argument("--sample-file", help="select_sample 产出的抽审名单 JSON（解锁域命中→人审）")
    p.add_argument("--trap", action="store_true", help="标记本次判定为陷阱样本（演习注入）")
    p.add_argument("--out", help="判定 JSON 落盘路径（缺省只打 stdout）")
    p.set_defaults(fn=cmd_adjudicate)

    p = sub.add_parser("shadow-record", help="判定 JSON → trust-shadow/<date>.jsonl（纯记录）")
    p.add_argument("--decision", required=True, help="adjudicate --out 产出的判定 JSON")
    for req in ("out-dir", "repo", "pr", "head-sha", "run-id", "event"):
        p.add_argument(f"--{req}", required=True)
    p.add_argument("--date", help="记录归属日期（缺省 UTC 今日，测试可钉死）")
    p.set_defaults(fn=cmd_shadow_record)

    p = sub.add_parser("reconcile", help="shadow 决策 × owner 裁决比对（AC-2）")
    p.add_argument("--decisions", required=True, help="record=decision JSONL")
    p.add_argument("--rulings", required=True, help="record=ruling JSONL（owner merged/closed）")
    p.add_argument("--predicates", default=os.path.join(here, "predicates.yaml"))
    p.add_argument("--out", help="reconcile 记录 JSONL 追加路径")
    p.add_argument("--summary", help="汇总 JSON 落盘路径")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("unlock-evaluate", help="连续 ≥50 一致+零逃逸+陷阱占比达阈值 → 解锁判定")
    p.add_argument("--reconcile", required=True, help="record=reconcile JSONL")
    p.add_argument("--predicates", default=os.path.join(here, "predicates.yaml"))
    p.add_argument("--unlock-state", default=os.path.join(here, "unlock-state.yaml"))
    p.add_argument("--out-events", help="unlock/reset 事件 JSONL 追加路径")
    p.add_argument("--summary", required=True, help="逐域汇总 JSON 落盘路径")
    p.add_argument("--write-state", help="新 unlock-state.yaml 落盘路径（C1 PR 落盘前预览）")
    p.set_defaults(fn=cmd_unlock_evaluate)

    p = sub.add_parser("sample", help="解锁后按比例抽审选择（sha256 PRF，种子可注入）")
    p.add_argument("--prs", required=True, help="逗号分隔 PR 号")
    p.add_argument("--domain", required=True)
    p.add_argument("--seed", required=True, help="随机种子（审计可复现的注入点）")
    p.add_argument("--rate", type=float, help="缺省取 predicates post_unlock_sample_rate")
    p.add_argument("--predicates", default=os.path.join(here, "predicates.yaml"))
    p.add_argument("--out", help="抽样结果 JSON 落盘路径")
    p.set_defaults(fn=cmd_sample)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except TrustGateError as e:
        print(f"::error::infra（fail-closed，非拒绝非放行）: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
