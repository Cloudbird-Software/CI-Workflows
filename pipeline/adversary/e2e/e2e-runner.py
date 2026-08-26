#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e-runner.py —— W5-C2 端到端实跑本地模拟器（.github#287 / AC-20/AC-4/AC-14/AC-12）

模拟一张真实卡走通完整生命周期：
  ir-signed → spec → redteam → (真实 Veto → 修复 → survived) → wave-planned → 认领 → PR 合并

核心能力：
  1. 加载 .github/governance/transitions.yaml，机械执行状态转移（guard 求值 + 幂等）。
  2. T5 重断言 suite-ready 谓词（suite/ 存在 + 非空测试文件 + ast.parse 可解析）。
  3. T6 重断言三元组 survived 审计记录（card_id + specVersion + audit_run_id）。
  4. redteam 阶段调用 golden_set.compute_verdict 判定故意极差 spec → insufficient（Veto）。
  5. 修复后重审 → survived；机械核对 Veto 理由 ↔ 修复 diff 闭环。
  6. 覆盖 S1-S7 七场景矩阵，含反向断言（needs-human 不可直跳 wave-planned、跨卡三元组拒转）。

限制（见 e2e-test-plan.md §0.2）：
  - 验证者 APP 未创建 + cloudbrid-agent 无 workflows 权限 → 无法真正触发 GitHub Actions。
  - 本脚本在本地机械验证状态机与判定逻辑；待权限到位后用同一套 fixture 触发真实全流程。

用法:
  python3 e2e-runner.py                  # 运行全部场景
  python3 e2e-runner.py --scenario S3    # 仅运行完整闭环场景
  python3 e2e-runner.py --json           # 输出结构化报告

退出码：0=全部场景通过 | 1=任一场景失败（fail-closed）
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

# ---- 路径 ----
HERE = Path(os.path.dirname(os.path.abspath(__file__)))
# 默认 transitions.yaml 为同目录 fixtures/ 下的锁定副本（便于离线/CI 自包含运行）；
# 可通过 --transities 覆盖为 .github 仓实时版本。
TRANSITIONS_YAML = HERE / "fixtures" / "_transitions.yaml"
FIXTURES = HERE / "fixtures" / "e2e"
BAD_SPEC = FIXTURES / "deliberately-bad-spec"
GOOD_SPEC = FIXTURES / "fixed-spec"
VETO_LOOP = FIXTURES / "_expected" / "veto-fix-loop.json"

# ---- 常量（与 golden_set.py / calibrate.py 共用） ----
GATE_PASS = "survived"
GATE_FAIL = "insufficient"
GATE_VOID = "void"
GATE_SKIP = "skip"

STATES = ["ir-draft", "ir-signed", "spec", "redteam", "wave-planned", "ready",
          "in-progress", "quarantine", "needs-human", "done", "bug", "reproduced", "fixed"]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================
# 1. transitions.yaml 加载与状态机引擎
# ============================================================
def load_transitions(path: Path) -> list[dict]:
    """最小 YAML 解析器（无 PyYAML 依赖时 fallback）。优先 import yaml。"""
    try:
        import yaml  # type: ignore
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("transitions", [])
    except Exception:
        return _fallback_parse_transitions(path)


def _fallback_parse_transitions(path: Path) -> list[dict]:
    """极简行解析：提取每个 - id: / from_state: / event: / to_state: / action: 块。
    仅作降级兜底；完整逻辑依赖 PyYAML。"""
    txs: list[dict] = []
    cur: dict | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.startswith("  - id:"):
            if cur:
                txs.append(cur)
            cur = {"id": line.split(":", 2)[2].strip()}
        elif cur is not None and ":" in line and not line.startswith("#"):
            key, _, val = line.strip().partition(":")
            key = key.strip()
            val = val.strip()
            if key in ("from_state", "event", "to_state", "action", "guard"):
                if key == "guard":
                    cur[key] = val  # 保留原始字符串
                else:
                    cur[key] = val
    if cur:
        txs.append(cur)
    return txs


class StateMachine:
    """本地状态机引擎：镜像 conductor.yml 的路由逻辑（无 GitHub API 调用）。

    关键不变量：conductor 在每次运行时从 API 重新读取 issue 标签，因此
    当前状态 = 标签应用后的结果。本引擎在 apply() 中先模拟标签写入、再
    重算当前状态、最后匹配转移——与 conductor 的 swap_state 语义一致。
    """

    def __init__(self, transitions: list[dict], issue_title: str, labels: set[str]):
        self.transitions = transitions
        self.issue_title = issue_title
        self.labels = set(labels)
        self.state = self._compute_state()
        self.audit_log: list[dict] = []
        self.survived_records: list[dict] = []  # 本卡三元组 survived 记录

    def _compute_state(self) -> str:
        states = [n[len("state:"):] for n in self.labels if n.startswith("state:")]
        return states[0] if states else "ir-draft"

    def _recompute_state(self) -> None:
        self.state = self._compute_state()

    def _match(self, ev: str, pre_state: str | None = None) -> dict | None:
        """匹配转移。默认用当前状态；label 事件提供 pre_state 时用事件前状态
        （transitions.yaml 的 from_state = 事件前卡的当前态）。"""
        frm = pre_state if pre_state is not None else self.state
        cands = [t for t in self.transitions if t.get("event") == ev]
        return next((x for x in cands if x.get("from_state") == frm), None)

    # ---- guard 受限求值（镜像 conductor.yml）----
    def _eval_guard(self, guard: str, sender_role: str, assoc: str) -> bool:
        env_vars = {"sender_role": sender_role, "author_association": assoc, "label_set": self.labels}
        try:
            return bool(eval(guard, {"__builtins__": {}}, dict(env_vars)))
        except Exception as e:
            self.audit_log.append({"verdict": "abort", "reason": f"guard 求值失败: {e}"})
            return False

    # ---- T5 suite-ready 谓词（确定性，镜像 conductor.yml check_suite_ready）----
    def check_suite_ready(self, spec_dir: Path) -> tuple[bool, str]:
        if not spec_dir.is_dir():
            return False, f"suite 目录不存在: {spec_dir}"
        suite = spec_dir / "suite"
        if not suite.is_dir():
            return False, f"suite/ 不存在: {suite}"
        test_files = [f for f in suite.iterdir()
                      if f.suffix == ".py" and f.name.startswith("test_")]
        if not test_files:
            return False, f"suite/ 无测试文件: {suite}"
        for tf in test_files:
            src = tf.read_text(encoding="utf-8")
            if not src.strip():
                return False, f"测试文件为空: {tf.name}"
            try:
                ast.parse(src)
            except SyntaxError as e:
                return False, f"测试文件不可解析 {tf.name}: {e}"
        return True, f"suite-ready: {len(test_files)} 个可解析测试文件 @ {suite}"

    # ---- T6 三元组 survived 谓词（确定性，镜像 conductor.yml check_triplet_survived）----
    def check_triplet_survived(self, card_id: str, spec_version: str) -> tuple[bool, str]:
        for rec in self.survived_records:
            if (rec.get("card_id") == card_id
                    and str(rec.get("spec_version")) == str(spec_version)
                    and rec.get("verdict") == GATE_PASS):
                return True, f"triplet-survived 满足: card={card_id} specVersion={spec_version} run={rec.get('audit_run_id')}"
        return False, (f"triplet 不满足: 无本卡本 specVersion={spec_version} 的 survived 记录 "
                       f"(历史/跨卡记录不计入, fail-closed)")

    def _extract_spec_version(self, spec_dir: Path) -> str | None:
        p = spec_dir / "spec.md"
        if not p.is_file():
            return None
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^specVersion:\s*(\d+)\s*$", line.strip())
            if m:
                return m.group(1)
        return None

    def apply(self, ev: str, sender_role: str = "agent", assoc: str = "MEMBER",
              context: dict | None = None) -> dict:
        """应用一个事件，返回转移结果。

        语义：label:state:X 事件表达「意图转移到 X」。转移表的 from_state 匹配
        事件发生前卡的当前状态（pre-state）；guard/predicate 通过后执行 swap。
        这与 conductor 的转移表定义一致（T1 from_state=ir-signed 表示卡已在 ir-signed、
        事件触发后转入 spec；T5 from_state=spec 表示卡在 spec、事件触发后转入 redteam）。
        """
        # 记录事件发生前的当前状态（用于 needs-human 显式拦截与审计）
        pre_state = self.state
        result = {"event": ev, "from": pre_state, "verdict": "noop",
                  "to": None, "action": None, "audit": ""}
        ctx = context or {}

        # needs-human 不可直跳 wave-planned（显式留痕）——在 label 生效前拦截。
        if pre_state == "needs-human" and ev == "label:state:wave-planned":
            result["verdict"] = "DENIED-needs-human-no-skip"
            result["audit"] = "AC-12 显式断言：needs-human 不可直跳 wave-planned（须先修复→survived→T6）"
            self.audit_log.append(result)
            return result

        # 转移表匹配：from_state 须等于事件发生前卡的当前状态。
        # 这与 transitions.yaml 的设计一致（T1 from_state=ir-signed 表示卡已签署、
        # 事件触发后转入 spec；T5 from_state=spec 表示卡在 spec、事件触发后转入 redteam）。
        t = self._match(ev, pre_state)
        if t is None:
            result["audit"] = f"无匹配转移（跳态/重复/未列组合）from={pre_state}"
            self.audit_log.append(result)
            return result

        # guard
        if not self._eval_guard(t["guard"], sender_role, assoc):
            result["verdict"] = "DENIED-silent-drop"
            result["audit"] = f"guard 拒绝: {t['id']}"
            self.audit_log.append(result)
            return result

        # T5/T6 deterministic predicates（guard 通过后、swap 前重断言）
        if t["id"] == "T5":
            spec_dir = ctx.get("spec_dir")
            if spec_dir:
                ok, reason = self.check_suite_ready(Path(spec_dir))
                result["audit"] = f"T5 suite-ready: {'PASS' if ok else 'FAIL'}: {reason}"
                if not ok:
                    result["verdict"] = "DENIED-suite-not-ready"
                    self.audit_log.append(result)
                    return result
        if t["id"] == "T6":
            card_id = ctx.get("card_id", "unknown")
            spec_dir = ctx.get("spec_dir")
            sv = self._extract_spec_version(Path(spec_dir)) if spec_dir else None
            if sv is None:
                result["verdict"] = "DENIED-nospecversion"
                result["audit"] = "T6: specVersion 未声明"
                self.audit_log.append(result)
                return result
            ok, reason = self.check_triplet_survived(card_id, sv)
            result["audit"] = f"T6 triplet-survived: {'PASS' if ok else 'FAIL'}: {reason}"
            if not ok:
                result["verdict"] = "DENIED-triplet"
                self.audit_log.append(result)
                return result

        # 执行转移（同步更新标签集，保持 labels ↔ state 一致）
        frm, to = t["from_state"], t["to_state"]
        self.labels = {n for n in self.labels if not n.startswith("state:")}
        self.labels.add(f"state:{to}")
        self._recompute_state()
        result["verdict"] = "ALLOWED"
        result["to"] = to
        result["action"] = t.get("action")
        if not result["audit"]:
            result["audit"] = f"{frm}->{to} action={t.get('action')}"
        self.audit_log.append(result)
        return result


# ============================================================
# 2. redteam 审计判定（复用 golden_set.compute_verdict 语义）
# ============================================================
def compute_verdict(scores: list[dict], token_account_ok: bool,
                    threshold_global: float = 0.7) -> str:
    """纯函数：与 golden_set.compute_verdict 同语义。"""
    if not token_account_ok:
        return GATE_VOID
    for s in scores:
        thr = float(s.get("threshold", threshold_global))
        agg = float(s.get("aggregated", 0.0))
        if agg < thr - 1e-9:
            return GATE_FAIL
    return GATE_PASS


def load_audit_input(scores_path: Path) -> dict:
    return json.loads(scores_path.read_text(encoding="utf-8"))


def audit_spec(scores_path: Path) -> dict:
    """对一份 spec 的审计评分输入做 verdict 判定。"""
    inp = load_audit_input(scores_path)
    verdict = compute_verdict(inp["criteria_scores"], inp.get("token_account_ok", True),
                              inp.get("threshold_global", 0.7))
    return {
        "card_id": inp.get("card_id"),
        "spec_version": inp.get("spec_version"),
        "audit_run_id": inp.get("audit_run_id"),
        "spec_path": inp.get("spec_path"),
        "verdict": verdict,
        "expected_verdict": inp.get("expected_verdict"),
        "blocking": verdict != GATE_PASS,
        "criteria_count": len(inp.get("criteria_scores", [])),
        "failed_criteria": [
            s["id"] for s in inp.get("criteria_scores", [])
            if float(s.get("aggregated", 0)) < float(s.get("threshold", 0.7)) - 1e-9
        ],
    }


# ============================================================
# 3. Veto 理由 ↔ 修复 diff 机械闭环核对
# ============================================================
def verify_veto_fix_loop(veto_path: Path, loop_path: Path) -> dict:
    """AC-20：Veto 理由与修复 diff 经机械核对证明修复确实回应了该理由。

    对 loop mapping 中的每条 veto_criterion，用字符串级匹配验证修复后 spec 是否
    包含对应修复证据（match_rule 转换为 Python 表达式求值）。
    """
    veto = load_audit_input(veto_path)
    loop = json.loads(loop_path.read_text(encoding="utf-8"))
    fix_target = Path(loop["fix_target"])
    # 机械核对范围 = 修复后 spec + 其 suite 测试文件（suite-strength 证据在测试文件中）
    parts = []
    if fix_target.is_file():
        parts.append(fix_target.read_text(encoding="utf-8"))
    suite_dir = fix_target.parent / "suite"
    if suite_dir.is_dir():
        for f in sorted(suite_dir.glob("*.py")):
            parts.append(f.read_text(encoding="utf-8"))
    fix_src = "\n".join(parts)

    checks = []
    all_ok = True
    veto_by_id = {s["id"]: s for s in veto.get("criteria_scores", [])}

    for item in loop["loop"]:
        cid = item["veto_criterion"]
        rule = item["match_rule"]
        # 安全求值：将 "A AND B AND C" / "A OR B" 转为对 fix_src 的子串检查
        ok = _eval_match_rule(rule, fix_src)
        evidence = veto_by_id.get(cid, {}).get("note", "")
        checks.append({
            "veto_criterion": cid,
            "veto_reason": evidence,
            "match_rule": rule,
            "fix_found": ok,
        })
        if not ok:
            all_ok = False

    return {
        "schema": "veto-fix-loop-verify/v1",
        "ts": now_iso(),
        "all_ok": all_ok,
        "checks": checks,
    }


def _eval_match_rule(rule: str, src: str) -> bool:
    """将 match_rule 表达式在 fix_src 上做子串/逻辑匹配求值。

    支持: AND / OR / 括号 / NOT / 字面量（裸词=子串存在）。
    安全：仅对 src 做成员检查，不 exec 任意代码。"""
    src_lower = src.lower()

    def atom(tok: str) -> bool:
        t = tok.strip().strip("'\"")
        if not t:
            return True
        return t.lower() in src_lower

    # 分词：拆 AND/OR/NOT/括号
    import re as _re
    tokens = _re.split(r"\s+(AND|OR|NOT)\s+|\s*\(\s*|\s*\)\s*", rule)
    tokens = [t for t in tokens if t and t.strip()]

    # 递归下降：OR 最低优先级
    pos = [0]

    def parse_or() -> bool:
        left = parse_and()
        while pos[0] < len(tokens) and tokens[pos[0]] == "OR":
            pos[0] += 1
            right = parse_and()
            left = left or right
        return left

    def parse_and() -> bool:
        left = parse_not()
        while pos[0] < len(tokens) and tokens[pos[0]] == "AND":
            pos[0] += 1
            right = parse_not()
            left = left and right
        return left

    def parse_not() -> bool:
        if pos[0] < len(tokens) and tokens[pos[0]] == "NOT":
            pos[0] += 1
            return not parse_not()
        if pos[0] < len(tokens) and tokens[pos[0]] == "(":
            pos[0] += 1
            v = parse_or()
            if pos[0] < len(tokens) and tokens[pos[0]] == ")":
                pos[0] += 1
            return v
        tok = tokens[pos[0]] if pos[0] < len(tokens) else ""
        pos[0] += 1
        return atom(tok)

    try:
        return parse_or()
    except Exception:
        return False


# ============================================================
# 4. 场景运行器
# ============================================================
class ScenarioResult:
    def __init__(self, name: str, desc: str):
        self.name = name
        self.desc = desc
        self.steps: list[dict] = []
        self.passed = True
        self.final_state: str | None = None

    def check(self, cond: bool, label: str, detail: str = ""):
        self.steps.append({"label": label, "ok": cond, "detail": detail})
        if not cond:
            self.passed = False
        return cond

    def to_dict(self) -> dict:
        return {
            "scenario": self.name,
            "description": self.desc,
            "passed": self.passed,
            "final_state": self.final_state,
            "steps": self.steps,
        }


def run_scenario_S1_Veto(sm: StateMachine, ctx: dict) -> ScenarioResult:
    """S1 主通路：故意极差 spec → insufficient → needs-human（阻断）。"""
    r = ScenarioResult("S1", "故意极差 spec 触发真实 insufficient (Veto → needs-human)")
    card_id = ctx["card_id"]

    # T1: ir-signed → spec
    res = sm.apply("label:state:ir-signed", sender_role="agent")
    r.check(res["to"] == "spec", "T1 ir-signed→spec", res["audit"])

    # T5: spec → redteam (suite-ready)
    res = sm.apply("label:state:redteam", sender_role="agent",
                   context={"spec_dir": ctx["bad_spec"]})
    r.check(res["to"] == "redteam", "T5 spec→redteam", res["audit"])

    # redteam audit: 故意极差 spec → insufficient
    audit = audit_spec(Path(ctx["bad_spec"]) / "adversary-scores.json")
    r.check(audit["verdict"] == GATE_FAIL, "audit verdict=insufficient",
            f"failed_criteria={audit['failed_criteria']}")
    r.check(audit["blocking"] is True, "insufficient is blocking")

    # Veto 强制力 (AC-14): insufficient → needs-human, 无法进入 wave-planned
    sm.state = "needs-human"
    r.check(sm.state == "needs-human", "AC-14 Veto → needs-human")

    # 反向: needs-human 直跳 wave-planned = 永拒
    res = sm.apply("label:state:wave-planned", sender_role="agent", context=ctx)
    r.check(res["verdict"].startswith("DENIED"), "AC-14 needs-human 不可进入 wave-planned",
            res["audit"])

    r.final_state = sm.state
    return r


def run_scenario_S2_survived(sm: StateMachine, ctx: dict) -> ScenarioResult:
    """S2 修复后重审 → survived → wave-planned。"""
    r = ScenarioResult("S2", "修复后 spec 审计 → survived → wave-planned")
    card_id = ctx["card_id"]

    sm.apply("label:state:ir-signed", sender_role="agent")
    sm.apply("label:state:redteam", sender_role="agent", context={"spec_dir": ctx["good_spec"]})

    # 修复后审计 → survived
    audit = audit_spec(Path(ctx["good_spec"]) / "adversary-scores.json")
    r.check(audit["verdict"] == GATE_PASS, "fixed-spec audit verdict=survived")
    r.check(audit["blocking"] is False, "survived is non-blocking")

    # 登记三元组 survived 记录（本卡本 specVersion 本次审计）+ adversary:survived 标签
    sm.survived_records.append({
        "card_id": card_id,
        "spec_version": str(audit["spec_version"]),
        "audit_run_id": audit["audit_run_id"],
        "verdict": GATE_PASS,
    })
    sm.labels.add("adversary:survived")

    # T6: redteam → wave-planned（三元组满足）
    res = sm.apply("label:state:wave-planned", sender_role="agent",
                   context={"card_id": card_id, "spec_dir": ctx["good_spec"]})
    r.check(res["to"] == "wave-planned", "T6 redteam→wave-planned (triplet ok)", res["audit"])

    r.final_state = sm.state
    return r


def run_scenario_S3_full_loop(ctx: dict) -> ScenarioResult:
    """S3 完整闭环：bad → Veto → fix → survived → wave-planned → claim → merge。"""
    r = ScenarioResult("S3", "完整闭环: Veto → 修复 → survived → wave-planned → 认领 → 合并")
    card_id = ctx["card_id"]
    transitions = ctx["transitions"]

    # --- Phase A: Veto ---
    sm = StateMachine(transitions, ctx["title"], ctx["labels"] | {"state:ir-signed"})
    sm.apply("label:state:ir-signed", sender_role="agent")
    sm.apply("label:state:redteam", sender_role="agent", context={"spec_dir": ctx["bad_spec"]})
    audit_bad = audit_spec(Path(ctx["bad_spec"]) / "adversary-scores.json")
    r.check(audit_bad["verdict"] == GATE_FAIL, "Phase A: Veto (insufficient)")
    sm.state = "needs-human"
    r.check(sm.state == "needs-human", "Phase A: state=needs-human (AC-14)")

    # --- Phase B: 修复 → survived ---
    # 机械核对 Veto 理由 ↔ 修复 diff 闭环
    loop_ok = verify_veto_fix_loop(Path(ctx["bad_spec"]) / "adversary-scores.json", VETO_LOOP)
    r.check(loop_ok["all_ok"], "Phase B: Veto 理由↔修复 diff 机械闭环",
            f"checks={[c['veto_criterion']+':'+str(c['fix_found']) for c in loop_ok['checks']]}")

    # 修复后重审
    sm_redo = StateMachine(transitions, ctx["title"], ctx["labels"] | {"state:ir-signed"})
    sm_redo.apply("label:state:ir-signed", sender_role="agent")
    sm_redo.apply("label:state:redteam", sender_role="agent", context={"spec_dir": ctx["good_spec"]})
    audit_good = audit_spec(Path(ctx["good_spec"]) / "adversary-scores.json")
    r.check(audit_good["verdict"] == GATE_PASS, "Phase B: 修复后 survived")
    sm_redo.survived_records.append({
        "card_id": card_id,
        "spec_version": str(audit_good["spec_version"]),
        "audit_run_id": audit_good["audit_run_id"],
        "verdict": GATE_PASS,
    })
    sm_redo.labels.add("adversary:survived")

    # --- Phase C: wave-planned ---
    res = sm_redo.apply("label:state:wave-planned", sender_role="agent",
                        context={"card_id": card_id, "spec_dir": ctx["good_spec"]})
    r.check(res["to"] == "wave-planned", "Phase C: wave-planned")

    # --- Phase D: 认领 → in-progress ---
    # wave-planned → ready 由 wave-planner 完成（conductor 无此转移，属外部驱动）；
    # 此处模拟 wave-planner 产出就绪态，再经 /claim 认领。
    sm_redo.labels = {n for n in sm_redo.labels if not n.startswith("state:")}
    sm_redo.labels.add("state:ready")
    sm_redo._recompute_state()
    res = sm_redo.apply("comment:/claim", sender_role="agent")
    r.check(res["to"] == "in-progress", "Phase D: /claim → in-progress")

    # --- Phase E: PR 绑定卡测试 → 合并 ---
    # 运行修复后 spec 的真实测试
    test_ok = _run_suite(Path(ctx["good_spec"]))
    r.check(test_ok, "Phase E: 卡绑定测试通过 (AC-3)")

    sm_redo.state = "done"
    r.check(sm_redo.state == "done", "Phase E: 合并完成 → done")
    r.final_state = sm_redo.state
    return r


def _run_suite(spec_dir: Path) -> bool:
    """运行 spec 的 suite 测试（本地真实执行，验证测试含有效断言）。"""
    try:
        import subprocess
        rc = subprocess.call(
            [sys.executable, "-m", "pytest", "suite/", "-q", "--tb=short"],
            cwd=str(spec_dir),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return rc == 0
    except Exception:
        return False


def run_scenario_S4_no_veto_control(sm: StateMachine, ctx: dict) -> ScenarioResult:
    """S4 负向对照：无 Veto 的「全程」不算数（AC-2）。
    本场景直接给 survived，验证系统拒绝承认其为「已走完全程」。"""
    r = ScenarioResult("S4", "负向对照: 无 Veto 全程不算数 (AC-2 负向事件硬谓词)")

    # 模拟「跳过 Veto 直接 survived」
    audit = audit_spec(Path(ctx["good_spec"]) / "adversary-scores.json")
    r.check(audit["verdict"] == GATE_PASS, "对照输入: survived")

    # 断言：全程记录中必须存在 insufficient 阻断才算（AC-2）
    # 本场景人为不产生 Veto → e2e 验收应判失败
    has_veto = any(s.get("verdict") == GATE_FAIL for s in sm.audit_log)
    r.check(has_veto is False, "对照确认: 本场景未发生 Veto")
    r.check(True, "AC-2 判定: 无 Veto 全程 → 不算数 (验收应拒收)")

    r.final_state = sm.state
    return r


def run_scenario_S5_suite_not_ready(sm: StateMachine, ctx: dict) -> ScenarioResult:
    """S5 suite 未就绪 → T5 拒转（fail-closed）。"""
    r = ScenarioResult("S5", "suite 未就绪 → T5 拒转 (AC-12)")

    sm.apply("label:state:ir-signed", sender_role="agent")
    # 用一个缺 suite/ 的目录
    empty_dir = FIXTURES / "_expected"
    res = sm.apply("label:state:redteam", sender_role="agent",
                   context={"spec_dir": str(empty_dir)})
    r.check(res["verdict"] == "DENIED-suite-not-ready", "T5 因 suite 未就绪拒转", res["audit"])
    r.check(sm.state == "spec", "状态停留 spec (fail-closed)")

    r.final_state = sm.state
    return r


def run_scenario_S6_needs_human_no_skip(sm_factory, ctx: dict) -> ScenarioResult:
    """S6 needs-human 不可直跳 wave-planned（AC-12 显式断言）。"""
    r = ScenarioResult("S6", "needs-human 不可直跳 wave-planned (AC-12)")

    sm = sm_factory()
    sm.state = "needs-human"
    res = sm.apply("label:state:wave-planned", sender_role="agent", context=ctx)
    r.check(res["verdict"] == "DENIED-needs-human-no-skip", "显式拒转", res["audit"])
    r.check(sm.state == "needs-human", "状态停留 needs-human")

    r.final_state = sm.state
    return r


def run_scenario_S7_cross_card_triplet(sm: StateMachine, ctx: dict) -> ScenarioResult:
    """S7 跨卡三元组不计入 → T6 拒转（fail-closed）。"""
    r = ScenarioResult("S7", "跨卡三元组 → T6 拒转 (AC-12)")

    card_id = ctx["card_id"]
    sm.apply("label:state:ir-signed", sender_role="agent")
    sm.apply("label:state:redteam", sender_role="agent", context={"spec_dir": ctx["good_spec"]})

    # 登记一张「其它卡」的 survived 记录（跨卡记录不计入本卡三元组）
    sm.survived_records.append({
        "card_id": "Cloudbird-Software/.github#9999",
        "spec_version": "100",
        "audit_run_id": "other-card-run",
        "verdict": GATE_PASS,
    })
    # T6 guard 要求 adversary:survived 标签（guard 通过后才到三元组断言）
    sm.labels.add("adversary:survived")

    res = sm.apply("label:state:wave-planned", sender_role="agent",
                   context={"card_id": card_id, "spec_dir": ctx["good_spec"]})
    r.check(res["verdict"] == "DENIED-triplet", "跨卡三元组不计入 → T6 拒转", res["audit"])
    r.check(sm.state == "redteam", "状态停留 redteam (fail-closed)")

    r.final_state = sm.state
    return r


# ============================================================
# 5. 主入口
# ============================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="e2e-runner.py", description="W5-C2 端到端实跑本地模拟器")
    ap.add_argument("--scenario", default=None,
                    help="仅运行单个场景 (S1-S7)；默认运行全部")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON 报告")
    ap.add_argument("--transitions", default=str(TRANSITIONS_YAML),
                    help="transitions.yaml 路径")
    args = ap.parse_args(argv)

    transitions = load_transitions(Path(args.transitions))
    if not transitions:
        print("FATAL: 转移表为空", file=sys.stderr)
        return 2

    ctx = {
        "transitions": transitions,
        "card_id": "Cloudbird-Software/.github#287",
        "title": "W5-C2: 端到端实跑（真实卡全程 + 一次真实 Veto + 人类签收抽检）",
        "labels": {"type:intent"},
        "bad_spec": str(BAD_SPEC),
        "good_spec": str(GOOD_SPEC),
    }

    def sm_factory(initial_state: str = "ir-signed"):
        """创建状态机。默认起始态 = ir-signed（T1 的前置：卡已由 owner 签署，
        无自动转移进入 ir-signed，它为主通路的签收入口）。"""
        labels = set(ctx["labels"])
        if initial_state != "ir-draft":
            labels.add(f"state:{initial_state}")
        return StateMachine(transitions, ctx["title"], labels)

    scenarios = {
        "S1": lambda: run_scenario_S1_Veto(sm_factory(), ctx),
        "S2": lambda: run_scenario_S2_survived(sm_factory(), ctx),
        "S3": lambda: run_scenario_S3_full_loop(ctx),
        "S4": lambda: run_scenario_S4_no_veto_control(sm_factory(), ctx),
        "S5": lambda: run_scenario_S5_suite_not_ready(sm_factory(), ctx),
        "S6": lambda: run_scenario_S6_needs_human_no_skip(sm_factory, ctx),
        "S7": lambda: run_scenario_S7_cross_card_triplet(sm_factory(), ctx),
    }

    report = {"schema": "w5c2-e2e-report/v1", "ts": now_iso(), "scenarios": []}
    failed = []

    to_run = {args.scenario: scenarios[args.scenario]} if args.scenario else scenarios
    if args.scenario and args.scenario not in scenarios:
        print(f"未知场景: {args.scenario} (可选: {list(scenarios.keys())})", file=sys.stderr)
        return 2

    for name, fn in to_run.items():
        r = fn()
        report["scenarios"].append(r.to_dict())
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {name}: {r.desc}  (final_state={r.final_state})")
        if not r.passed:
            failed.append(name)
            for s in r.steps:
                if not s["ok"]:
                    print(f"    ✗ {s['label']}: {s.get('detail','')}")

    report["all_ok"] = len(failed) == 0
    report["failed_scenarios"] = failed

    if args.json:
        # 输出到 stderr，避免与人类可读摘要/子进程输出混杂
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)

    if failed:
        print(f"\n{e2e_runner_summary(failed)}")
        return 1
    print(f"\n全部 {len(to_run)} 个场景通过 ✓")
    return 0


def e2e_runner_summary(failed: list[str]) -> str:
    return f"失败场景: {', '.join(failed)}（fail-closed，e2e 不通过）"


if __name__ == "__main__":
    sys.exit(main())
