#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""adversary.py —— 恶意合规 adversary 核心（W4-C2 .github#221，ADR-0067）

宪法 §4E 测试红队的机内实现：输入 spec + 完整验收套件，让 judge-deep 档模型
（配置锁定，AR-8 跨族分离）产出"通过全部测试的最偷懒实现"；产物在套件上
全绿 → 判"套件不充分"（blocking，exit 1），报告标明钻的是哪个洞（策略 ID →
套件缺口映射，供 test-author 定向补强）；攻击失败 → 套件通过考验（exit 0），
但报告必须含 ≥1 条真实攻击尝试——空输出/不可解析 = exit 3（恒绿防御：
adversary 假装攻击失败交白卷，让每个套件轻松过关）。

本模块不做网络调用（LLM 编排在 run-adversary.sh，唯一入口 = 计量 wrapper，
ADR-0062）——只做确定性部分：配置锁校验、prompt 组装、应答解析、套件执行、
判定与报告。依赖 PyYAML（CI 步骤钉版 pyyaml==6.0.3，与 metering selftest 同款）。

目标目录契约（--target）：
  spec.md       被攻 spec（全文进 prompt）
  suite/        验收套件测试文件集（全文进 prompt，至少 1 个文件）
  run-suite.sh  套件执行器：bash run-suite.sh <impl-dir>；exit 0=全绿

adversary 应答契约（prompt-v1.md 第 5 条）：单个 JSON 对象
  {"attempts":[{"strategy":"S1","rationale":"...","files":{"<文件名>":"<内容>"}}]}

安全语义：adversary 产物是故意生成的不可信代码，judge 子命令会真实执行它
（套件执行是判定本体，不可 Mock）——只允许在一次性 CI runner / 专用沙箱跑
（workflow 注释同此声明），禁止在持有凭据的长活环境执行。

子命令：
  config        打印锁定配置 JSON（任何漂移 fail-closed exit 2）
  build-prompt  组装用户 prompt（spec+套件+策略表）→ --out
  judge         应答解析 → 逐 attempt 落盘执行套件 → 判定 + 报告 → --report-out

退出码：0=套件通过考验 | 1=套件不充分（blocking）| 2=配置/环境错误
        | 3=adversary 空输出/不可解析（恒绿防御，infra）
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:  # 依赖缺失即 fail-closed，不静默降级
    print("FATAL: 需要 PyYAML（CI 已钉版 pyyaml==6.0.3；本地 pip install pyyaml）", file=sys.stderr)
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "adversary-config.yaml")
STRATEGIES_PATH = os.path.join(HERE, "attack-strategies.yaml")
MODELS_PATH = os.path.normpath(os.path.join(HERE, "..", "models.yaml"))
LOCKED_ALIAS = "judge-deep"          # ADR-0067 决策 3：档名本身是锁，换档=C1+ADR
SUITE_TIMEOUT_S = 240                # 单次尝试套件执行上限（防 adversary 产物死循环拖垮 runner）
SUITE_TAIL_CHARS = 1200              # 报告留存的套件输出尾证（判定证据）
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")  # 落盘文件名白名单（拒路径穿越）


def err(msg):
    """GitHub Actions 注解友好形态（本地为纯文本）"""
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)


def die(code, msg):
    err(msg)
    sys.exit(code)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path):
    with open(path, "rb") as f:
        return "sha256:" + hashlib.sha256(f.read()).hexdigest()


def load_yaml(path):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001 —— 配置不可读即 fail-closed
        die(2, f"YAML 不可读 {path}: {e}")


# ---------------- 配置锁（AC-3：模型/prompt 版本/采样参数全锁定，漂移即红） ----------------
def load_lock():
    """加载并校验锁定配置。任何一项漂移 → exit 2（fail-closed）：
    - alias 必须是 judge-deep（ADR-0067 决策 3 锁定档）
    - prompt 文件 sha256 必须与配置声明一致（prompt 改动=版本变更，须同步配置）
    - 攻击面清单版本与配置声明一致
    - 族分离断言（AR-8）：adversary 族 ≠ builder 族 ≠ test-author 族
    - 与 pipeline/models.yaml judge-deep 角色档交叉断言（model/temperature/max_tokens）"""
    cfg = load_yaml(CONFIG_PATH) or {}
    st = load_yaml(STRATEGIES_PATH) or {}
    adv = cfg.get("adversary") or {}
    xf = cfg.get("cross_family") or {}
    errs = []
    if cfg.get("version") != 1:
        errs.append("adversary-config version 应为 1")
    if adv.get("alias") != LOCKED_ALIAS:
        errs.append(f"alias 应锁定 {LOCKED_ALIAS}（换档=C1+ADR，勿静默偏离）")
    ppath = os.path.join(HERE, adv.get("prompt_file") or "prompt-v1.md")
    phash = sha256_file(ppath) if os.path.isfile(ppath) else None
    if phash is None:
        errs.append(f"prompt 文件缺失：{adv.get('prompt_file')}")
    elif phash != adv.get("prompt_sha256"):
        errs.append(f"prompt hash 漂移：文件={phash} 配置={adv.get('prompt_sha256')}（改 prompt 必须同步配置并引用 ADR）")
    if st.get("version") != adv.get("strategies_version"):
        errs.append(f"attack-strategies 版本 {st.get('version')!r} 与配置声明 {adv.get('strategies_version')!r} 不一致")
    strategies = st.get("strategies") or []
    ids = [s.get("id") for s in strategies]
    if not strategies or len(set(ids)) != len(ids):
        errs.append("攻击面策略表为空或 id 重复")
    for s in strategies:
        for k in ("id", "name", "tactic", "hole", "suite_gap"):
            if not s.get(k):
                errs.append(f"策略 {s.get('id')!r} 缺字段 {k}")
    fam = adv.get("family")
    bf = (xf.get("builder") or {}).get("family")
    tf = (xf.get("test_author") or {}).get("family")
    if not (fam and bf and tf):
        errs.append("cross_family 声明不全（adversary/builder/test_author 三方族都必须声明）")
    else:
        if fam == bf:
            errs.append(f"AR-8 违例：adversary 族 {fam} == builder 族（同族共谋面）")
        if fam == tf:
            errs.append(f"AR-8 违例：adversary 族 {fam} == test-author 族（同族共谋面）")
        if bf == tf:
            errs.append("AR-8 基准退化：builder 与 test-author 同族，断言失去意义")
    roles = (load_yaml(MODELS_PATH) or {}).get("roles") or {}
    role = roles.get(LOCKED_ALIAS)
    if not role:
        errs.append(f"pipeline/models.yaml 缺 {LOCKED_ALIAS} 角色档（IFACE-06 解析表）")
    else:
        smp = adv.get("sampling") or {}
        if role.get("model") != adv.get("model"):
            errs.append(f"model 与角色档不一致：config={adv.get('model')!r} models.yaml={role.get('model')!r}")
        if role.get("temperature") != smp.get("temperature"):
            errs.append(f"temperature 与角色档不一致：config={smp.get('temperature')!r} models.yaml={role.get('temperature')!r}")
        if role.get("max_tokens") != smp.get("max_tokens"):
            errs.append(f"max_tokens 与角色档不一致：config={smp.get('max_tokens')!r} models.yaml={role.get('max_tokens')!r}")
    if errs:
        die(2, "配置锁校验失败（fail-closed——考试档案语义不容漂移，ADR-0067 决策 3）：\n  " + "\n  ".join(errs))
    return {"adv": adv, "xf": xf, "strategies": strategies,
            "prompt_path": ppath, "prompt_version": phash}


def lock_report(lock):
    """锁定配置的报告形态（进每次运行报告——AC-3：模型/prompt hash/族标记全留痕）"""
    adv, xf = lock["adv"], lock["xf"]
    return {
        "alias": adv["alias"], "model": adv["model"], "family": adv["family"],
        "prompt_file": adv["prompt_file"], "prompt_version": lock["prompt_version"],
        "strategies_version": adv["strategies_version"], "strategies_count": len(lock["strategies"]),
        "sampling": adv.get("sampling") or {}, "max_attempts": adv.get("max_attempts", 8),
        "cross_family": {
            "adversary": adv["family"], "builder": xf["builder"]["family"],
            "test_author": xf["test_author"]["family"],
            "ok": adv["family"] not in (xf["builder"]["family"], xf["test_author"]["family"]),
        },
    }


def cmd_config(a):  # noqa: ARG001
    print(json.dumps(lock_report(load_lock()), ensure_ascii=False, sort_keys=True))
    return 0


# ---------------- prompt 组装（攻击面清单硬编码进策略表——可配置 attack-strategies.yaml） ----------------
def check_target(target):
    spec = os.path.join(target, "spec.md")
    suite_dir = os.path.join(target, "suite")
    runner = os.path.join(target, "run-suite.sh")
    if not (os.path.isfile(spec) and os.path.isdir(suite_dir) and os.path.isfile(runner)):
        die(2, f"目标目录契约不满足（需 spec.md + suite/ + run-suite.sh）：{target}")
    files = sorted(f for f in os.listdir(suite_dir) if os.path.isfile(os.path.join(suite_dir, f)))
    if not files:
        die(2, f"suite/ 为空：{suite_dir}")
    return spec, suite_dir, files


def cmd_build_prompt(a):
    lock = load_lock()
    spec, suite_dir, files = check_target(a.target)
    parts = ["【任务】对下面的验收套件做恶意合规攻击（宪法 §4E 测试红队 / ADR-0067）：",
             "产出\"通过全部测试的最偷懒实现\"——形式满足每条检查，实质背叛 spec 意图。", ""]
    parts.append("【SPEC】")
    with open(spec, encoding="utf-8") as f:
        parts.append(f.read().strip())
    parts.append("")
    parts.append("【验收套件】（你的实现会被落盘并由这些测试真实执行；exit 0=全绿=你得手）")
    for fn in files:
        with open(os.path.join(suite_dir, fn), encoding="utf-8") as f:
            parts.append(f"### suite/{fn}\n-----\n{f.read().rstrip()}\n-----")
    parts.append("")
    parts.append("【攻击面策略表】（只许用表内策略；每次尝试标注 strategy id；得手时表中\"洞\"即套件缺口归因）")
    parts.append("| id | 策略 | 战术 | 得手说明明的洞（suite_gap） |")
    parts.append("|---|---|---|---|")
    for s in lock["strategies"]:
        parts.append(f"| {s['id']} | {s['name']} | {s['tactic']} | {s['hole']}（{s['suite_gap']}） |")
    parts.append("")
    parts.append(f"【输出契约】只输出一个 JSON 对象（无围栏无其他文字）；尝试数上限 {lock['adv'].get('max_attempts', 8)}：")
    parts.append('{"attempts":[{"strategy":"<表内 id>","rationale":"≤120 字攻击思路",'
                 '"files":{"<文件名>":"<完整可运行文件内容>"}}]}')
    parts.append("- files 文件名只许 [A-Za-z0-9._-]（禁路径），模块名须与套件 import 一致")
    parts.append("- 全部失败是正常结论（强套件本该防住），但必须留下真实尝试——空输出=基础设施故障")
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(parts) + "\n")
    print(f"prompt → {a.out}（策略 {len(lock['strategies'])} 条，套件文件 {len(files)} 个）", file=sys.stderr)
    return 0


# ---------------- 判定（套件执行 + 钻洞归因 + 报告） ----------------
def extract_json(text):
    """adversary 应答 → JSON 对象。容忍 ```json 围栏与前后杂文字；本质非 JSON 由调用方捕获。"""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    cand = m.group(1) if m else text.strip()
    if not m:
        i, j = cand.find("{"), cand.rfind("}")
        if i >= 0 and j > i:
            cand = cand[i:j + 1]
    return json.loads(cand)


def _decode_maybe_b(x):
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")


def to_bash_path(p):
    """Windows 本地跑（Git Bash + MSYS python 探测回落）时 os.sep 反斜杠会被
    bash 当转义符吃掉——传给 bash 的路径统一正斜杠（Linux 无变化）。"""
    return p.replace("\\", "/")


def run_suite_once(target, impl_dir):
    """执行一次尝试：落盘产物 → bash run-suite.sh <impl-dir>。返回 (green, rc, tail, note)。
    cwd=target 下以裸脚本名调用（路径不带 target 前缀——双重前缀在两平台都是坏路径）。"""
    try:
        proc = subprocess.run(["bash", "run-suite.sh", to_bash_path(impl_dir)],
                              capture_output=True, text=True, cwd=target, timeout=SUITE_TIMEOUT_S)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, proc.returncode, out[-SUITE_TAIL_CHARS:].strip() or "(无输出)", ""
    except subprocess.TimeoutExpired as e:
        out = _decode_maybe_b(e.stdout) + _decode_maybe_b(e.stderr)
        return False, 124, out[-SUITE_TAIL_CHARS:].strip(), f"套件执行超时（>{SUITE_TIMEOUT_S}s，按红计）"
    except OSError as e:
        return False, 125, "", f"run-suite.sh 不可执行：{e}"


def cmd_judge(a):
    lock = load_lock()
    check_target(a.target)
    stmap = {s["id"]: s for s in lock["strategies"]}
    max_att = lock["adv"].get("max_attempts", 8)
    with open(a.response, encoding="utf-8", errors="replace") as f:
        text = f.read()
    parse_errors, attempts = [], []
    try:
        doc = extract_json(text)
        if not isinstance(doc, dict) or not isinstance(doc.get("attempts"), list):
            parse_errors.append("应答缺 attempts 数组（契约见 prompt-v1.md 第 5 条）")
        else:
            attempts = doc["attempts"]
            if not attempts:
                parse_errors.append("attempts 为空数组——白卷（恒绿防御对象）")
    except (json.JSONDecodeError, AttributeError, ValueError) as e:
        parse_errors.append(f"应答不可解析为 JSON：{e}")

    results, valid = [], 0
    for i, att in enumerate(attempts):
        sid = att.get("strategy") if isinstance(att, dict) else None
        if sid not in stmap:
            parse_errors.append(f"attempt[{i}] 策略 id 未知或缺失：{sid!r}")
            continue
        files = att.get("files") if isinstance(att, dict) else None
        if not isinstance(files, dict) or not files:
            parse_errors.append(f"attempt[{i}]（{sid}）缺 files")
            continue
        bad = [n for n in files if not NAME_RE.match(str(n))]
        if bad:
            parse_errors.append(f"attempt[{i}]（{sid}）文件名不合规（只许 [A-Za-z0-9._-]）：{bad}")
            continue
        if valid >= max_att:
            parse_errors.append(f"attempt[{i}]（{sid}）超出 max_attempts={max_att}（成本护栏 BUDGET-01）被丢弃")
            continue
        valid += 1
        impl = tempfile.mkdtemp(prefix=f"adversary-{sid}-")
        try:
            for name, content in files.items():
                with open(os.path.join(impl, str(name)), "w", encoding="utf-8", newline="\n") as f:
                    f.write(str(content))
            green, rc, tail, note = run_suite_once(a.target, impl)
        finally:
            shutil.rmtree(impl, ignore_errors=True)
        s = stmap[sid]
        results.append({"strategy": sid, "name": s["name"], "hole": s["hole"], "suite_gap": s["suite_gap"],
                        "rationale": str(att.get("rationale") or "")[:400], "files": sorted(str(n) for n in files),
                        "green": green, "suite_rc": rc, "note": note, "suite_tail": tail})

    # 恒绿防御（ADR-0067 决策 2）：零有效尝试=白卷=infra（exit 3），绝不让套件轻松过关
    if valid == 0:
        verdict, blocking = "no-attempts", False
    else:
        verdict = "insufficient" if any(r["green"] for r in results) else "survived"
        blocking = verdict == "insufficient"
    report = {
        "schema": "adversary-report/v1", "ts": now_iso(), "target": os.path.abspath(a.target),
        "verdict": verdict, "blocking": blocking, "config": lock_report(lock),
        "attempt_count": len(results), "attempts": results,
        "exploited": [r["strategy"] for r in results if r["green"]],
        "holes": [{"strategy": r["strategy"], "hole": r["hole"], "suite_gap": r["suite_gap"]}
                  for r in results if r["green"]],
        "parse_errors": parse_errors,
    }
    with open(a.report_out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print_summary(report)
    if verdict == "no-attempts":
        err("adversary 零有效攻击尝试（恒绿防御：白卷=infra exit 3）。parse_errors: "
            + "; ".join(parse_errors[:5]))
        sys.exit(3)
    sys.exit(1 if blocking else 0)


def print_summary(report):
    cfg = report["config"]
    verdict_cn = {"insufficient": "套件不充分（blocking）", "survived": "套件通过考验",
                  "no-attempts": "零有效尝试（infra）"}[report["verdict"]]
    print("== 恶意合规 adversary 判定（ADR-0067）==")
    print(f"verdict: {report['verdict']} —— {verdict_cn}")
    print(f"锁定: {cfg['alias']}@{cfg['model']} family={cfg['family']} "
          f"prompt={cfg['prompt_version'][:19]}… strategies={cfg['strategies_version']} "
          f"跨族断言 ok={cfg['cross_family']['ok']}")
    for r in report["attempts"]:
        mark = "全绿（得手）" if r["green"] else f"红（rc={r['suite_rc']}{' ' + r['note'] if r['note'] else ''}）"
        print(f"  {r['strategy']} {r['name']} → {mark}｜{r['hole']}（suite_gap={r['suite_gap']}）")
    if report["verdict"] == "insufficient":
        print(f"钻洞归因: " + "; ".join(f"{h['strategy']}→{h['suite_gap']}" for h in report["holes"])
              + "——先补强套件（加属性/负控制/边界用例）再放行实现 PR")
    elif report["verdict"] == "survived":
        print(f"攻击尝试 {report['attempt_count']} 条全败（防恒绿：留痕完毕，报告含尝试记录）")


def main():
    ap = argparse.ArgumentParser(prog="adversary.py", description="恶意合规 adversary 核心（ADR-0067）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config").set_defaults(fn=cmd_config)
    p = sub.add_parser("build-prompt")
    p.add_argument("--target", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_build_prompt)
    p = sub.add_parser("judge")
    p.add_argument("--target", required=True)
    p.add_argument("--response", required=True, help="adversary 应答正文文件（metering wrapper stdout）")
    p.add_argument("--report-out", required=True)
    p.set_defaults(fn=cmd_judge)
    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
