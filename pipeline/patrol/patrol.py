#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patrol.py —— patrol 巡逻服务核心（W3-C2 .github#219，ADR-0065）

宪法 §3 patrol 三纪律的机内实现：只在机器可判定 oracle 违约时开单；LLM
"看着不对"只进 observation 桶两次独立出现才升级；抓到真 bug 的场景毕业进
CI 回归并离开语料（防刷熟）。

子命令：
  run       一次巡逻：三源场景生成（AC 注册表派生 / 历史逃逸模式攻击语法 /
            LLM 前沿探索+metamorphic）→ 探针执行 → oracle 分级判定 →
            指纹去重 → 频控 → 开单（draft|gh）→ yield 指标 + SNR 降频
  verdict   登记三值判定（ADR-0064：reproduced|falsified|undetermined）
  graduate  毕业机制（AC-3）：verdict=reproduced 的指纹 → 场景转 CI 回归
            （输出回归测试文件供人审入库）+ 从 patrol 语料移除
  metrics   从状态目录出 yield 报告（AC-4：每百次唯一真 bug 数/开单复现
            存活率/信噪比）

权限铁律（ADR-0065 决策 5）：本服务只读运行 + 开 issue；无任何改代码/
push/PR 写路径。政策文件须显式声明 forbidden 含 push/pr-write/label-write
（缺声明=fail-closed 拒跑）。

fail-closed：政策/语料/状态不可读或非法 → 非零退出；LLM 源无凭据时诚实
降级（计数 skipped，不伪装生成过）。

退出码：0=成功 | 2=参数/政策/环境错误 | 3=状态/判定物无效
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
POLICY_SCHEMA = "patrol-policy/v1"
AC_REGISTRY_SCHEMA = "patrol-ac-registry/v1"
ESCAPES_SCHEMA = "patrol-escapes/v1"
# 五类机器可判定 oracle（宪法 §3 / ADR-0065 决策 2——闭集，新类=ADR 修订）
ORACLE_CLASSES = ("crash", "http-5xx", "schema", "invariant", "perf-budget")
VERDICTS = ("reproduced", "falsified", "undetermined")


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def read_jsonl(path):
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                die(3, f"FATAL: {path}:{i + 1} 非法 JSON（状态不可信）：{e}")
    return out


def append_jsonl(path, rec):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(canonical(rec) + "\n")


def read_json(path, default):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            die(3, f"FATAL: {path} 不可解析（状态不可信）：{e}")


def write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def load_yaml(path, what):
    try:
        import yaml  # runner 预装 PyYAML（spec-check.py/postprocess.py 同款约定）
    except ImportError:
        die(2, "FATAL: PyYAML 不可用（政策/语料为 YAML——fail-closed 拒绝盲跑）")
    if not os.path.isfile(path):
        die(2, f"FATAL: {what} 不存在：{path}")
    with open(path, encoding="utf-8") as f:
        try:
            doc = yaml.safe_load(f)
        except yaml.YAMLError as e:
            die(2, f"FATAL: {what} 不可解析：{path}: {e}")
    if not isinstance(doc, dict):
        die(2, f"FATAL: {what} 顶层应为映射：{path}")
    return doc


# ---------------- 政策（.github governance/policy/patrol.yaml——阈值唯一真源） ----------------
def load_policy(path):
    p = load_yaml(path, "patrol 政策")
    if p.get("schema") != POLICY_SCHEMA:
        die(2, f"FATAL: 政策 schema 头应为 {POLICY_SCHEMA}（got {p.get('schema')!r}）")
    for key in ("rate_limit", "observation", "yield", "sources", "issue_mode", "permissions", "targets"):
        if key not in p:
            die(2, f"FATAL: 政策缺段 {key}——阈值集中政策文件（ADR-0065 决策 3），缺段拒绝运行")
    rl = p["rate_limit"]
    for k in ("max_issues_per_repo_per_hour", "max_issues_per_repo_per_day"):
        if not isinstance(rl.get(k), int) or rl[k] < 0:
            die(2, f"FATAL: rate_limit.{k} 应为非负整数")
    if not isinstance(p["targets"], list) or not p["targets"]:
        die(2, "FATAL: targets 为空——patrol 无巡逻面")
    for t in p["targets"]:
        for k in ("repo", "service", "ac_registry", "escapes"):
            if k not in t:
                die(2, f"FATAL: targets 条目缺 {k}（{t}）")
    if p["observation"].get("escalate_after_independent") != 2:
        # 宪法 §3 钉死"两次独立"；放宽=修宪级变更，先修 ADR-0065 再改此值
        die(2, "FATAL: observation.escalate_after_independent 须为 2")
    for k in ("snr_threshold", "snr_window_issues", "downshift_daily_issue_cap"):
        if k not in p["yield"]:
            die(2, f"FATAL: yield.{k} 缺失（AC-4 阈值集中政策文件）")
    if p["issue_mode"] not in ("draft", "gh"):
        die(2, "FATAL: issue_mode 应为 draft|gh（shadow 起步=draft，切 gh=C1 政策变更）")
    # 权限铁律自检（ADR-0065 决策 5）：政策必须显式把写路径列为 forbidden
    forbidden = set(p["permissions"].get("forbidden") or [])
    need = {"push", "pr-write", "label-write"}
    if not need <= forbidden:
        die(2, f"FATAL: permissions.forbidden 须包含 {sorted(need)}（patrol 只读运行+开 issue；"
               "状态标签写归 arbiter，INV-02 一致）")
    allowed = set(p["permissions"].get("allowed") or [])
    if not allowed <= {"read-run", "open-issue"}:
        die(2, "FATAL: permissions.allowed 只可为 read-run/open-issue")
    return p


# ---------------- 状态目录（指纹台账/observation 桶/判定/毕业/运行史） ----------------
class State:
    """跨 run 持久状态（CI 以 artifact 留存，下轮 run 前恢复）：
    fingerprints.jsonl 已开单指纹（同指纹不重复开单，AC-2）
    observations.jsonl observation 桶原始出现记录（run_id+seed 独立性判定）
    verdicts.jsonl     ADR-0064 三值判定事件
    runs.jsonl         每 run 一条（yield 分母/分子）
    graduations.jsonl  毕业实录（AC-3 证据）
    corpus-state.json  {graduated: [...], downshift: null|{...}}"""

    def __init__(self, d):
        self.dir = d
        os.makedirs(d, exist_ok=True)
        self.fp = os.path.join(d, "fingerprints.jsonl")
        self.obs = os.path.join(d, "observations.jsonl")
        self.verd = os.path.join(d, "verdicts.jsonl")
        self.runs = os.path.join(d, "runs.jsonl")
        self.grad = os.path.join(d, "graduations.jsonl")
        self.corpus = os.path.join(d, "corpus-state.json")

    def opened(self):
        return read_jsonl(self.fp)

    def observations(self):
        return read_jsonl(self.obs)

    def verdicts(self):
        return read_jsonl(self.verd)

    def run_records(self):
        return read_jsonl(self.runs)

    def graduations(self):
        return read_jsonl(self.grad)

    def corpus_state(self):
        return read_json(self.corpus, {"graduated": [], "downshift": None})

    def save_corpus(self, cs):
        write_json(self.corpus, cs)


def fingerprint_of(repo, scenario_id, symptom):
    """指纹 = repo + 场景 ID + 症状 的 sha256（ADR-0065 决策 3；沿用 drift-report
    症状签名去重模式——symptom 只取跨 run 稳定字段，traceback 明细不进指纹）"""
    return "sha256:" + sha256_text(f"{repo}\n{scenario_id}\n{symptom}")


# ---------------- 三源场景生成 ----------------
def _mk(sid, source, payloads, oracle, meta):
    return {"id": sid, "source": source, "payloads": payloads, "oracle": oracle, "meta": meta}


def _valid_oracle(oracle, where):
    if not isinstance(oracle, dict) or oracle.get("class") not in ORACLE_CLASSES:
        die(2, f"FATAL: {where} oracle.class 须为五类之一 {list(ORACLE_CLASSES)}"
               "（闭集——LLM 主观怀疑不属 oracle，走 observation 桶）")


def scenarios_from_ac_registry(path, graduated):
    """源 (a)：AC 注册表派生——注册表把卡/spec 的 AC 文本映射为机器可判定探针
    （声明 vs 实际行为对账）。每条 AC → 一个场景；oracle 不可判定 → fail-closed。"""
    doc = load_yaml(path, "AC 注册表")
    if doc.get("schema") != AC_REGISTRY_SCHEMA:
        die(2, f"FATAL: AC 注册表 schema 头应为 {AC_REGISTRY_SCHEMA}")
    out = []
    for e in doc.get("entries") or []:
        uid = e.get("ac_uid") or ""
        if not uid or not e.get("text") or "probe" not in e or "oracle" not in e:
            die(2, f"FATAL: AC 注册表条目缺 ac_uid/text/probe/oracle（{uid!r}）")
        _valid_oracle(e["oracle"], f"AC {uid}")
        out.append(_mk(f"ac-{uid}", "ac-registry", [e["probe"]], e["oracle"],
                       {"ac_uid": uid, "text": e["text"], "card_ref": e.get("card_ref", "")}))
    return [s for s in out if s["id"] not in graduated]


def _variant_key(grammar):
    return "amount" if grammar.get("amount_variants") else "b"


def scenarios_from_escapes(path, seed, per_pattern, graduated):
    """源 (b)：历史逃逸模式攻击语法——逃逸模式库（drift/红队/演习沉淀）参数化
    生成变体再攻击。seed 驱动的确定性采样（同 seed 同变体，可复现可审计）。"""
    doc = load_yaml(path, "逃逸模式库")
    if doc.get("schema") != ESCAPES_SCHEMA:
        die(2, f"FATAL: 逃逸模式库 schema 头应为 {ESCAPES_SCHEMA}")
    out = []
    for pat in doc.get("patterns") or []:
        pid = pat.get("pattern_id") or ""
        g = pat.get("grammar") or {}
        variants = g.get("amount_variants") or g.get("b_variants") or []
        if not pid or not variants or "op" not in g or "oracle" not in pat:
            die(2, f"FATAL: 逃逸模式 {pid!r} 缺 pattern_id/grammar.op/变体/oracle")
        _valid_oracle(pat["oracle"], f"逃逸模式 {pid}")
        rng = random.Random(f"{pid}:{seed}")
        # 变体排序键用 canonical 形态：变体空间可含混合类型（0 与 "0"）——
        # 直接 sorted 会 TypeError，且 canonical 保采样确定性可审计
        picked = sorted(rng.sample(variants, min(per_pattern, len(variants))),
                        key=lambda v: json.dumps(v, ensure_ascii=False, default=str))
        payloads = [dict({k: v for k, v in g.items() if not k.endswith("_variants")},
                         **{_variant_key(g): v}) for v in picked]
        out.append(_mk(f"esc-{pid}", "escape-pattern", payloads, pat["oracle"],
                       {"pattern_id": pid, "history_ref": pat.get("history_ref", ""),
                        "variants": picked}))
    return [s for s in out if s["id"] not in graduated]


# 源 (c) metamorphic：等价变换下 oracle 不变式必须保持（ADR-0065 决策 1c）。
# 变换表内置（新增变换=代码评审范围）；负控制：demo 目标 add(a==7) 播种缺陷
# 必须被 mt-commute 抓到（ADR-0065 风险缓解条款的机器执法面，pairs 强制含 [7,11]）。
META_POOL = [0, 1, 2, 5, 7, 11]


def scenarios_metamorphic(seed, pairs_per_run, graduated):
    rng = random.Random(f"mt:{seed}")
    pairs = [sorted(rng.sample(META_POOL, 2)) for _ in range(max(2, pairs_per_run))]
    if [7, 11] not in pairs:  # 确定性播种含已知缺陷触发对——负控制不靠运气
        pairs[0] = [7, 11]
    out = [
        _mk("mt-commute-add", "llm-metamorphic",
            [{"op": "add", "a": a, "b": b} for a, b in pairs],
            {"class": "invariant", "metamorphic": "commute-add"},
            {"transform": "commute-add", "relation": "add(a,b) 与 add(b,a) 结果必须等价"}),
        _mk("mt-numform", "llm-metamorphic",
            [{"op": "add", "a": a, "b": b} for a, b in pairs[: max(2, pairs_per_run // 2)]],
            {"class": "invariant", "metamorphic": "numform"},
            {"transform": "numform", "relation": "7 与 7.0 数值形态等价——结果必须数值相等"}),
    ]
    return [s for s in out if s["id"] not in graduated]


def frontier_via_llm(role_model, replay, metering_dir, workdir):
    """源 (c) LLM 前沿探索：经 metering wrapper（ADR-0062 一次 invoke 恰一条聚合
    记录）生成新探针。返回 (payloads, suspicious, status)；无凭据 →
    (None, [], "skipped-no-creds")——诚实降级计数，绝不伪装生成过。"""
    wrapper = os.path.join(HERE, "..", "metering", "metering-wrapper.sh")
    if not os.path.isfile(wrapper):
        return None, [], "skipped-no-wrapper"
    if not replay and not os.environ.get("LLM_API_KEY"):
        return None, [], "skipped-no-creds"
    os.makedirs(metering_dir, exist_ok=True)
    os.makedirs(workdir, exist_ok=True)
    prompt = os.path.join(workdir, "frontier-prompt.txt")
    with open(prompt, "w", encoding="utf-8", newline="\n") as f:
        f.write("你是 patrol 前沿探索器。针对一个 JSON 计算服务（op: add/sub/mul/div/"
                "transfer/fetch/report/search），生成 4 个新颖且互不重复的探针 payload"
                "（JSON 对象数组，键 payloads）；再把运行后『看着不对』的观察写成"
                " {suspicious:[{note,payload}]}（可为空数组）。只输出一个 JSON 对象。\n")
    args = ["bash", wrapper, "--model", role_model, "--prompt-file", prompt,
            "--role", "patrol-frontier", "--max-tokens", "512", "--thinking", "disabled",
            "--tag", "patrol"]
    env = dict(os.environ, GATE_METERING_DIR=metering_dir)
    if replay:
        args += ["--replay-file", replay]
    try:
        r = subprocess.run(args, capture_output=True, text=True, env=env, timeout=180)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, [], f"skipped-error:{type(e).__name__}"
    if r.returncode != 0:
        return None, [], f"skipped-error:wrapper-rc={r.returncode}"
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None, [], "skipped-error:content-not-json"
    payloads = [p for p in (obj.get("payloads") or []) if isinstance(p, dict) and "op" in p]
    susp = [s for s in (obj.get("suspicious") or []) if isinstance(s, dict) and s.get("note")]
    return payloads, susp, "ok"


# ---------------- 探针执行 + oracle 分级判定 ----------------
def run_probes(service, payloads, py=sys.executable):
    """探针执行（CLI 适配器）：stdin 逐 payload JSON，stdout JSON 包络。
    只读——patrol 不 import 目标代码，进程边界即权限边界（ADR-0065 决策 5）。
    单探针超时按 crash 记（不中断 run）。"""
    res = []
    for p in payloads:
        t0 = time.monotonic()
        try:
            r = subprocess.run([py, service], input=json.dumps(p).encode("utf-8"),
                               capture_output=True, timeout=30)
            res.append({"exit": r.returncode, "stdout": r.stdout.decode("utf-8", "replace"),
                        "stderr": r.stderr.decode("utf-8", "replace"),
                        "elapsed_ms": int((time.monotonic() - t0) * 1000)})
        except subprocess.TimeoutExpired:
            res.append({"exit": -1, "stdout": "", "stderr": "patrol 探针超时(30s)",
                        "elapsed_ms": 30000})
    return res


def _envelope(pr):
    """解析探针响应包络 {ok,http_status,data}；非 JSON → None（crash 类证据）。"""
    try:
        obj = json.loads(pr["stdout"]) if pr["stdout"] else None
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def grade_payload(payload, pr, oracle):
    """五类 oracle 分级判定（闭集）。返回 violation dict 或 None。"""
    env = _envelope(pr)
    if oracle["class"] == "crash" or env is None:
        if pr["exit"] != 0 or env is None:
            return {"class": "crash", "symptom": f"exit={pr['exit']}",
                    "detail": (pr["stderr"] or pr["stdout"])[:600]}
        return None
    if oracle["class"] == "http-5xx":
        st = int(env.get("http_status") or 200)
        if st >= 500:
            return {"class": "http-5xx", "symptom": f"http_status={st}",
                    "detail": canonical(env)[:600]}
        return None
    if oracle["class"] == "schema":
        data = env.get("data")
        if not isinstance(data, dict):
            return {"class": "schema", "symptom": "data:missing", "detail": canonical(env)[:600]}
        missing = [k for k in (oracle.get("required") or []) if k not in data]
        if missing:
            return {"class": "schema", "symptom": "missing=" + ",".join(sorted(missing)),
                    "detail": "data keys=" + ",".join(sorted(data))}
        return None
    if oracle["class"] == "invariant":
        # 声明式不变量：受控 eval（语料经仓内评审，非外部输入；无 builtins）
        ctx = dict(payload)
        if isinstance(env.get("data"), dict):
            ctx.update(env["data"])
        try:
            ok = bool(eval(oracle["expr"], {"__builtins__": {}}, dict(ctx)))  # noqa: S307
        except Exception as e:  # noqa: BLE001 —— 表达式自身错误=不可判定，按违约保守上报
            return {"class": "invariant", "symptom": f"expr-error:{type(e).__name__}",
                    "detail": oracle["expr"]}
        if not ok:
            return {"class": "invariant", "symptom": "violated:" + oracle["expr"],
                    "detail": canonical(ctx)[:600]}
        return None
    if oracle["class"] == "perf-budget":
        # 度量优先取包络 service_ms（服务内耗时）：CLI 适配器的进程墙钟含解释器
        # 启动开销（Windows 可达 250ms），会把预算判定污染成环境噪音；无
        # service_ms 的目标（如 HTTP 探针）回落墙钟
        sm = env.get("service_ms")
        measured = sm if isinstance(sm, (int, float)) else pr["elapsed_ms"]
        if measured > oracle["budget_ms"]:
            # 指纹只含稳定症状（预算值）：实测耗时逐 run 波动，进指纹会让
            # 同一预算超限 bug 每轮换哈希绕过去重（AC-2 失效）——波动值进 detail
            return {"class": "perf-budget",
                    "symptom": f"over-budget(>{oracle['budget_ms']}ms)",
                    "detail": f"measured_ms={measured}"
                              f"(service_ms={sm!r},wall={pr['elapsed_ms']}): "
                              + canonical(payload)}
        return None
    return None  # 闭集已在校验期拦截


def grade_scenario(scenario, prs):
    """场景级判定：首个违约即成 finding（一场景一单——多 payload 聚合，防同场景
    变体灌水开单）。trace=探针完整证据（开单必附）。"""
    for payload, pr in zip(scenario["payloads"], prs):
        v = grade_payload(payload, pr, scenario["oracle"])
        if v:
            return {"violation": v, "payload": payload,
                    "trace": {"exit": pr["exit"], "elapsed_ms": pr["elapsed_ms"],
                              "stdout": pr["stdout"][:800], "stderr": pr["stderr"][:800]}}
    return None


def _num_eq(x, y):
    try:
        return abs(float(x) - float(y)) < 1e-9
    except (TypeError, ValueError):
        return x == y


def _mt_transform(tf, pair):
    a, b = pair
    if tf == "commute-add":
        return {"op": "add", "a": b, "b": a}
    if tf == "numform":  # 整型换浮点形态：数值语义必须不变
        return {"op": "add", "a": float(a), "b": float(b)}
    raise ValueError(f"未知 metamorphic 变换 {tf}")


def grade_metamorphic(scenario, prs, service):
    """metamorphic 等价判定：base 与变换向响应必须等价（ok/result）。违约 class=
    invariant（等价关系被破坏=不变量违约——机器可判定，直接开单，不入桶）。"""
    tf = scenario["oracle"]["metamorphic"]
    for payload, pr in zip(scenario["payloads"], prs):
        base = _envelope(pr)
        if base is None or pr["exit"] != 0:
            v = grade_payload(payload, pr, {"class": "crash"})
            if v:
                return {"violation": v, "payload": payload,
                        "trace": {"transform": tf, "exit": pr["exit"],
                                  "stdout": pr["stdout"][:800], "stderr": pr["stderr"][:800]}}
            continue
        tp = _mt_transform(tf, (payload["a"], payload["b"]))
        tr = run_probes(service, [tp])[0]
        tenv = _envelope(tr)
        fa = (base.get("data") or {}).get("result")
        fb = ((tenv or {}).get("data") or {}).get("result")
        if base.get("ok") != (tenv or {}).get("ok") or not _num_eq(fa, fb):
            return {"violation": {"class": "invariant",
                                  "symptom": f"metamorphic:{tf}:result-mismatch",
                                  "detail": f"base={fa!r} transformed={fb!r}"},
                    "payload": payload,
                    "trace": {"transform": tf, "base": canonical(base)[:400],
                              "transformed": canonical(tenv or {})[:400]}}
    return None


# ---------------- observation 桶（两次独立才升级） ----------------
def record_observation(state, repo, scenario_label, note, run_id, seed, ts):
    """LLM"看着不对"只进桶；独立=不同 run 且不同 seed（宪法 §3）。
    返回 (fingerprint, escalated, occurrences)。已开过单 → escalated=False（去重）。"""
    fp = fingerprint_of(repo, scenario_label, "looks-wrong:" + sha256_text(note)[:16])
    occ = [o for o in state.observations() if o["fingerprint"] == fp]
    rec = {"fingerprint": fp, "repo": repo, "scenario": scenario_label, "note": note[:300],
           "run_id": run_id, "seed": seed, "ts": ts}
    append_jsonl(state.obs, rec)
    occ.append(rec)
    independent = any(o1["run_id"] != o2["run_id"] and o1["seed"] != o2["seed"]
                      for i, o1 in enumerate(occ) for o2 in occ[i + 1:])
    if not independent:
        return fp, False, occ
    if fp in {r["fingerprint"] for r in state.opened()}:
        return fp, False, occ  # 升级后同指纹复现 → 指纹层去重（AC-2 同语义）
    return fp, True, occ


# ---------------- 频控 + 开单 ----------------
def opened_since(state, repo, hours, clock):
    lo = dt.datetime.strptime(clock, "%Y-%m-%dT%H:%M:%SZ") - dt.timedelta(hours=hours)
    return sum(1 for r in state.opened()
               if r["repo"] == repo
               and dt.datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ") >= lo)


def allow_open(state, policy, repo, clock, downshift):
    """频控：每仓每小时/每日上限（政策可配）；降频激活时收敛到
    downshift_daily_issue_cap——SNR 过低=减少开单量而非停止巡逻（AC-4）。"""
    rl = policy["rate_limit"]
    cap_h, cap_d = rl["max_issues_per_repo_per_hour"], rl["max_issues_per_repo_per_day"]
    if downshift:
        cap_h = min(cap_h, 1)
        cap_d = min(cap_d, policy["yield"]["downshift_daily_issue_cap"])
    if opened_since(state, repo, 1, clock) >= cap_h:
        return False, f"hourly-cap({cap_h})"
    if opened_since(state, repo, 24, clock) >= cap_d:
        return False, f"daily-cap({cap_d})"
    return True, ""


def render_issue_body(repo, finding, scenario):
    v, m = finding["violation"], scenario.get("meta", {})
    lines = [
        f"- 指纹：`{finding['fingerprint']}`（repo+场景+症状 sha256——同指纹不重复开单）",
        f"- oracle：**{v['class']}**（机器可判定——宪法 §3 patrol 纪律）",
        f"- 场景：`{scenario['id']}`（源：{scenario['source']}）",
        f"- run：`{finding['run_id']}` seed=`{finding['seed']}` ts={finding['ts']}",
    ]
    if m.get("ac_uid"):
        lines.append(f"- AC：{m['ac_uid']}（{m.get('card_ref', '')}）——声明：「{m.get('text', '')}」")
    if m.get("history_ref"):
        lines.append(f"- 逃逸史：{m['history_ref']}（参数化变体再攻击）")
    if m.get("transform"):
        lines.append(f"- metamorphic：{m['transform']}——{m.get('relation', '')}")
    lines += ["", "## 症状", f"`{v['symptom']}`", "", "## 探针 payload", "```json",
              json.dumps(finding["payload"], ensure_ascii=False), "```", "", "## trace",
              "```", json.dumps(finding["trace"], ensure_ascii=False)[:1500], "```", "",
              "> 复现判定走 ADR-0064 三值协议（reproduced / falsified / undetermined）；",
              "> 复现成功 → `patrol.py graduate`（场景毕业进 CI 回归并离开 patrol 语料）。",
              "", "Card: Cloudbird-Software/.github#219（ADR-0065）",
              f"<!-- run_id={finding['run_id']} -->"]
    return "\n".join(lines)


def open_issue(mode, repo, finding, scenario, out_dir):
    """开单：draft 模式落 markdown artifact（shadow 起步——线上噪音为零）；
    gh 模式 gh issue create 打 bug 标签（patrol 唯一写面）。"""
    fp12 = finding["fingerprint"].replace("sha256:", "")[:12]
    title = f"[patrol] {finding['violation']['class']} 违约：{scenario['id']}（{repo}）"
    body = render_issue_body(repo, finding, scenario)
    if mode == "draft":
        path = os.path.join(out_dir, "issues", f"{repo.replace('/', '__')}-{fp12}.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"# 草稿（issue_mode=draft——未开线上单，artifact 即证据）\n\n"
                    f"**标题**：{title}\n\n---\n{body}\n")
        return f"draft:{os.path.basename(path)}"
    r = subprocess.run(["gh", "issue", "create", "--repo", repo, "--title", title,
                        "--body", body, "--label", "bug"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return f"gh-error:{r.stderr.strip()[:200]}"
    return r.stdout.strip()


# ---------------- yield 指标 + SNR 降频（AC-4） ----------------
def compute_metrics(state, policy):
    runs = state.run_records()
    opened = state.opened()
    verdicts = {v["fingerprint"]: v["verdict"] for v in state.verdicts()}
    reproduced = [r for r in opened if verdicts.get(r["fingerprint"]) == "reproduced"]
    unique_confirmed = len({r["fingerprint"] for r in reproduced})
    n_runs = len(runs)
    window = [r for r in opened][-policy["yield"]["snr_window_issues"]:]
    conf_w = sum(1 for r in window if verdicts.get(r["fingerprint"]) == "reproduced")
    snr = (conf_w / len(window)) if window else None
    return {
        "runs": n_runs,
        "probes_total": sum(r.get("probes", 0) for r in runs),
        "issues_opened": len(opened),
        "issues_reproduced": len(reproduced),
        "issue_reproduction_survival_rate":
            round(len(reproduced) / len(opened), 4) if opened else None,
        "unique_real_bugs": unique_confirmed,
        "yield_per_100_runs": round(100.0 * unique_confirmed / n_runs, 4) if n_runs else None,
        "snr_window": {"opened": len(window), "confirmed": conf_w,
                       "threshold": policy["yield"]["snr_threshold"]},
        "snr": round(snr, 4) if snr is not None else None,
    }


def maybe_downshift(state, policy, metrics):
    """信噪比（真 bug/开单数）低于阈值 → 自动降频 + needs-human 复核标记。"""
    cs = state.corpus_state()
    m = metrics["snr_window"]
    tripped = (metrics["snr"] is not None
               and m["opened"] >= policy["yield"]["snr_window_issues"]
               and metrics["snr"] < policy["yield"]["snr_threshold"])
    if tripped and not cs.get("downshift"):
        cs["downshift"] = {"active": True, "since": now_iso(),
                           "reason": f"snr={metrics['snr']} < {policy['yield']['snr_threshold']}"
                                     f"（window={m['opened']}）", "needs_human": True}
        state.save_corpus(cs)
    elif not tripped and cs.get("downshift"):
        # 窗口恢复（含人工调参后）自动解除——yield 数据驱动调参（ADR-0065 决策 6）
        cs["downshift"] = None
        state.save_corpus(cs)
    return state.corpus_state().get("downshift")


# ---------------- 回归测试生成（毕业，AC-3） ----------------
# check 代码块首行不带缩进（模板在 16 空格层注入），续行统一 16 空格对齐
I = " " * 16


def _reg_check(oracle):
    if oracle.get("metamorphic"):
        return ('pa = {"op": "add", "a": p["a"], "b": p["b"]}\n'
                f'{I}pb = {{"op": "add", "a": p["b"], "b": p["a"]}}\n'
                f'{I}ra, rb = run(pa), run(pb)\n'
                f'{I}va = json.loads(ra["stdout"]); vb = json.loads(rb["stdout"])\n'
                f'{I}self.assertEqual(va.get("ok"), vb.get("ok"), "metamorphic ok 等价被破坏")\n'
                f'{I}self.assertAlmostEqual((va.get("data") or {{}}).get("result"),\n'
                f'{I}                       (vb.get("data") or {{}}).get("result"),\n'
                f'{I}                       places=9, msg="metamorphic 结果等价被破坏")')
    return {
        "crash": 'self.assertEqual(pr["exit"], 0, "崩溃（应返回错误包络而非崩溃）")\n'
                 f'{I}self.assertTrue(pr["stdout"].strip(), "包络须为非空 JSON")',
        "http-5xx": 'env = json.loads(pr["stdout"])\n'
                    f'{I}self.assertLess(int(env.get("http_status") or 200), 500, "不得 5xx")',
        "schema": 'env = json.loads(pr["stdout"])\n'
                  f'{I}data = env.get("data") or {{}}\n'
                  f'{I}missing = [k for k in {oracle.get("required") or []} if k not in data]\n'
                  f'{I}self.assertEqual(missing, [], "schema 契约字段缺失")',
        "invariant": 'env = json.loads(pr["stdout"])\n'
                     f'{I}ctx = dict(p); ctx.update(env.get("data") or {{}})\n'
                     f'{I}self.assertTrue(eval({oracle.get("expr", "True")!r}, '
                     '{"__builtins__": {}}, ctx), "不变量违约")',
        "perf-budget": 'env = json.loads(pr["stdout"])\n'
                       f'{I}ms = env.get("service_ms", pr["elapsed_ms"])\n'
                       f'{I}self.assertLessEqual(ms, %d, "性能预算超限")'
                       % oracle.get("budget_ms", 0),
    }[oracle["class"]]


def emit_regression_test(rec, out_dir):
    """场景毕业 → CI 回归测试文件（artifact 供人审入库）。fail-before（ADR-0061）：
    生成的测试在缺陷修复前必须红（钉住缺陷），修复后转绿。"""
    sid = rec["scenario_id"].replace(":", "_").replace("/", "_")
    ident = re.sub(r"\W", "_", sid)  # 场景 ID 可含连字符——类名/标识符须净化
    path = os.path.join(out_dir, "regression", f"test_{sid}.py")
    body = json.dumps(rec["payloads"], ensure_ascii=False, indent=2)
    check = _reg_check(rec["oracle"])
    reg_dir = os.path.join(out_dir, "regression")
    try:
        service = os.path.relpath(rec["target_service"], reg_dir)
    except ValueError:  # Windows 跨盘（temp 在 C: 仓在 D:）——退绝对路径，测试语义不变
        service = rec["target_service"]
    os.makedirs(reg_dir, exist_ok=True)
    tmpl = REG_TMPL.format(sid=sid, ident=ident, service=service.replace("\\", "/"),
                           source=rec["source"], payloads=body, check=check)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(tmpl)
    return path


REG_TMPL = '''# -*- coding: utf-8 -*-
"""patrol 毕业回归（AC-3 / ADR-0065）：场景 {sid}（源 {source}）抓到真 bug 且复现
成功，按毕业机制从 patrol 语料转入 CI 回归套件。fail-before（ADR-0061）：
缺陷修复前本测试必须红——负控制即其存在意义。人审通过后入库；
场景已在 patrol corpus-state 标记 graduated（防刷熟——patrol 不再消费）。"""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.normpath(os.path.join(HERE, {service!r}))
PAYLOADS = {payloads}


def run(payload):
    import time as _t
    t0 = _t.monotonic()
    r = subprocess.run([sys.executable, SERVICE], input=json.dumps(payload).encode("utf-8"),
                       capture_output=True, timeout=30)
    return {{"exit": r.returncode, "stdout": r.stdout.decode("utf-8", "replace"),
            "stderr": r.stderr.decode("utf-8", "replace"),
            "elapsed_ms": int((_t.monotonic() - t0) * 1000)}}


class TestGraduated_{ident}(unittest.TestCase):
    def test_oracle(self):
        for p in PAYLOADS:
            with self.subTest(payload=p):
                pr = run(p)
                {check}


if __name__ == "__main__":
    unittest.main()
'''


# ---------------- run：一次巡逻 ----------------
def _try_open(state, policy, repo, clock, downshift, finding, scenario, out):
    """开单公共路径：指纹去重（AC-2）→ 频控 → 开单 → 落台账。
    返回 ("opened", ref) | ("deduped", fp) | ("deferred", reason)。"""
    fp = finding["fingerprint"]
    if fp in {r["fingerprint"] for r in state.opened()}:
        return "deduped", fp
    ok, why = allow_open(state, policy, repo, clock, downshift)
    if not ok:
        return "deferred", why
    ref = open_issue(policy["issue_mode"], repo, finding, scenario, out)
    append_jsonl(state.fp, {"fingerprint": fp, "repo": repo, "scenario_id": scenario["id"],
                            "source": scenario["source"], "oracle": scenario["oracle"],
                            "payloads": scenario["payloads"], "symptom": finding["violation"]["symptom"],
                            "target_service": finding["target_service"], "ts": clock,
                            "run_id": finding["run_id"], "issue_ref": ref})
    return "opened", ref


def cmd_run(a):
    policy = load_policy(a.policy)
    state = State(a.state)
    cs = state.corpus_state()
    graduated = set(cs.get("graduated") or [])
    downshift = cs.get("downshift")
    target = next((t for t in policy["targets"] if t.get("repo") == a.repo), None)
    if target is None:
        die(2, f"FATAL: repo {a.repo} 不在政策 targets（巡逻面由政策声明——未声明不巡逻）")
    base = a.target_base or os.getcwd()
    service = target["service"] if os.path.isabs(target["service"]) \
        else os.path.normpath(os.path.join(base, target["service"]))

    def rp(rel):
        return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(base, rel))

    src = policy["sources"]
    clock = a.clock or now_iso()

    scenarios = []
    scenarios += scenarios_from_ac_registry(rp(target["ac_registry"]), graduated)
    scenarios += scenarios_from_escapes(rp(target["escapes"]), a.seed,
                                        src.get("escape", {}).get("variants_per_pattern", 4), graduated)
    scenarios += scenarios_metamorphic(a.seed, src.get("metamorphic", {}).get("pairs_per_run", 6),
                                       graduated)
    counts = {"ac-registry": 0, "escape-pattern": 0, "llm-metamorphic": 0}
    probes = 0
    opened, deferred, deduped = [], [], 0

    def settle(finding, scenario):
        nonlocal deduped
        status, info = _try_open(state, policy, a.repo, clock, downshift, finding, scenario, a.out)
        if status == "opened":
            opened.append({"fingerprint": finding["fingerprint"], "scenario": scenario["id"],
                           "class": finding["violation"]["class"], "issue_ref": info})
        elif status == "deduped":
            deduped += 1
        else:
            deferred.append({"fingerprint": finding["fingerprint"],
                             "scenario": scenario["id"], "reason": info})

    for sc in scenarios:
        counts[sc["source"]] += 1
        prs = run_probes(service, sc["payloads"])
        probes += len(sc["payloads"])
        finding = (grade_metamorphic(sc, prs, service) if sc["oracle"].get("metamorphic")
                   else grade_scenario(sc, prs))
        if finding:
            finding.update({"fingerprint": fingerprint_of(a.repo, sc["id"],
                                                          finding["violation"]["symptom"]),
                            "repo": a.repo, "ts": clock, "run_id": a.run_id, "seed": a.seed,
                            "target_service": service})
            settle(finding, sc)

    # 源 (c) LLM 半边：前沿探索。metamorphic 半边已在上面（确定性，恒跑）。
    # 无凭据 → 诚实降级（计数 skipped-no-creds，不伪装生成过——W2-C3 同款纪律）。
    llm_status, obs_escalated, obs_seen = "not-configured", [], 0
    if src.get("llm", {}).get("enabled", True):
        payloads, susp, llm_status = frontier_via_llm(
            src["llm"].get("model", "glm-4.5-air"), a.llm_replay,
            os.path.join(a.state, "metering"), os.path.join(a.out, "llm"))
        if payloads:
            counts["llm-metamorphic"] += 1
            sc = _mk("llm-frontier", "llm-metamorphic", payloads, {"class": "crash"},
                     {"transform": "llm-frontier", "relation": "任意 payload 不得崩溃/非 JSON"})
            prs = run_probes(service, payloads)
            probes += len(payloads)
            finding = grade_scenario(sc, prs)
            if finding:
                finding.update({"fingerprint": fingerprint_of(a.repo, sc["id"],
                                                              finding["violation"]["symptom"]),
                                "repo": a.repo, "ts": clock, "run_id": a.run_id, "seed": a.seed,
                                "target_service": service})
                settle(finding, sc)
        # LLM"看着不对"→ observation 桶：两次独立（不同 run 且不同 seed）才升级（AC-1）
        for s in susp:
            obs_seen += 1
            fp, escalated, occ = record_observation(
                state, a.repo, f"llm:{(s.get('payload') or {}).get('op', 'n/a')}",
                s["note"], a.run_id, a.seed, clock)
            if escalated:
                finding = {"fingerprint": fp,
                           "violation": {"class": "invariant",
                                         "symptom": "observation-escalated(2 次独立)",
                                         "detail": s["note"]},
                           "payload": s.get("payload") or {},
                           "trace": {"occurrences": [{k: o[k] for k in ("run_id", "seed", "ts")}
                                                     for o in occ]},
                           "repo": a.repo, "ts": clock, "run_id": a.run_id, "seed": a.seed,
                           "target_service": service}
                scenario = _mk("obs", "llm-metamorphic", [], {"class": "invariant"},
                               {"transform": "observation",
                                "relation": "LLM 主观怀疑两次独立出现升级开单"})
                before = len(opened)
                settle(finding, scenario)
                if len(opened) > before:
                    obs_escalated.append(fp)

    metrics = compute_metrics(state, policy)
    metrics.update({"run_id": a.run_id, "clock": clock, "repo": a.repo, "seed": a.seed,
                    "scenarios": counts, "probes": probes,
                    "opened_this_run": len(opened), "opened": opened,
                    "deduped": deduped, "deferred": deferred,
                    "llm_status": llm_status, "observations_seen": obs_seen,
                    "observations_escalated": obs_escalated,
                    "graduated_active": sorted(graduated), "downshift": downshift})
    metrics["downshift"] = maybe_downshift(state, policy, metrics)
    append_jsonl(state.runs, {"run_id": a.run_id, "ts": clock, "repo": a.repo,
                              "probes": probes, "opened": len(opened), "seed": a.seed})
    write_json(os.path.join(a.out, "report.json"), metrics)
    print(canonical(metrics))
    return 0


def cmd_verdict(a):
    state = State(a.state)
    if a.fingerprint not in {r["fingerprint"] for r in state.opened()}:
        die(3, f"FATAL: 指纹未开过单——无单判定是空转（{a.fingerprint}）")
    append_jsonl(state.verd, {"fingerprint": a.fingerprint, "verdict": a.verdict,
                              "ts": now_iso(), "by": a.by})
    print(f"verdict 记录：{a.fingerprint[:19]}… → {a.verdict}")
    return 0


def cmd_graduate(a):
    """毕业（AC-3）：复现成功（verdict=reproduced）→ 场景转 CI 回归 + 离开语料。"""
    state = State(a.state)
    rec = next((r for r in state.opened() if r["fingerprint"] == a.fingerprint), None)
    if rec is None:
        die(3, f"FATAL: 指纹未开过单：{a.fingerprint}")
    verdicts = {v["fingerprint"]: v["verdict"] for v in state.verdicts()}
    if verdicts.get(a.fingerprint) != "reproduced":
        die(2, f"FATAL: 该指纹最新判定为 {verdicts.get(a.fingerprint)!r}——毕业仅由"
               "『复现成功』触发（ADR-0065 决策 4 / ADR-0064 判定协议）")
    cs = state.corpus_state()
    if rec["scenario_id"] not in cs.get("graduated", []):
        cs.setdefault("graduated", []).append(rec["scenario_id"])
        state.save_corpus(cs)
    path = emit_regression_test(rec, a.out)
    record = {"fingerprint": a.fingerprint, "scenario_id": rec["scenario_id"],
              "graduated_at": now_iso(), "regression_test": path, "removed_from_corpus": True}
    append_jsonl(state.grad, record)
    print(canonical(record))
    return 0


def cmd_metrics(a):
    policy = load_policy(a.policy)
    print(canonical(compute_metrics(State(a.state), policy)))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="patrol.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run")
    p.add_argument("--policy", required=True, help="patrol 政策（.github governance/policy/patrol.yaml）")
    p.add_argument("--state", required=True, help="跨 run 状态目录（artifact 留存）")
    p.add_argument("--out", required=True, help="本轮产物目录（issue 草稿/report）")
    p.add_argument("--target-base", help="目标仓路径基目录（政策内相对路径的解析基准）")
    p.add_argument("--repo", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--llm-replay", help="离线回放文件（零真实 LLM——自测/演习）")
    p.add_argument("--clock", help="ISO 时钟覆写（频控窗口/演习确定性；缺省 now UTC）")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("verdict")
    p.add_argument("--state", required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--verdict", required=True, choices=VERDICTS)
    p.add_argument("--by", default="patrol-demo")
    p.set_defaults(fn=cmd_verdict)

    p = sub.add_parser("graduate")
    p.add_argument("--state", required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--out", required=True, help="回归测试输出目录（artifact 供人审入库）")
    p.set_defaults(fn=cmd_graduate)

    p = sub.add_parser("metrics")
    p.add_argument("--policy", required=True)
    p.add_argument("--state", required=True)
    p.set_defaults(fn=cmd_metrics)

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
