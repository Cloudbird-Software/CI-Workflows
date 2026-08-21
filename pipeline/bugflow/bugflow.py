#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bugflow.py —— Bug 流 reproduce 阶段核心（W3-C1 .github#218，ADR-0064）

宪法 §3 Bug 流（复现前置，签署点后移）/§9#6。判定协议 = SWT-bench F→P 扩展
（ADR-0064 决策 7 署名条款）：(base 上 fail, fix 上 pass)=复现。

子命令：
  repro        一次完整 reproduce：指纹去重 → env-gate → 哨兵自证 → 三值判定
               → （api 模式）落标签+评论（dry-run 只打印——fixture 自测面）
  sample-week  误关率周抽样（AC-4，宪法 §3"误关率每周抽样成数字"）：从判定账本
               JSONL 确定性抽 3 单 → 抽样清单 JSONL（周审计人工复核入口；
               dashboard 联动是 W5-C4 卡，本工具只产数据不动 dashboard）

三值判定（AC-1/AC-2/AC-3）：
  reproduced        base 采样×2 稳定 fail ∧（无 fix 候选 ∨ fix 上 pass）；
                    base fail ∧ fix fail = reproduced+fix-unresolved（bug 真实、
                    fix 候选未生效——入判定日志，不改判定；修 fix 是修复流的事）
  cannot-reproduce  base 采样×2 稳定 pass（不关单、不转移状态——保留人裁）
  inconclusive      环境指纹不匹配（ADR-0064 决策 2：不一致≠证伪）/ 超时 / 非断言
                    运行错误 / 翻转（两采样不一致）→ 换新环境重试一次，仍不可
                    判定 → 最终 needs-human（label inconclusive + state 转人裁）

fail-closed（AC-1）：哨兵自证证据缺失或不完整（基线不绿 / 哨兵不红 / 自证日志
写不出或读不到）→ exit 2，不做任何判定、不打任何判定标签。

repro 用例约定：用例输出一行 `REPRO_OUTCOME: pass|fail` 表达断言语义；退出码与
marker 不一致 / 无 marker / 超时 / 无法启动 = 环境语义（error/timeout），绝不进
F→P 判定——"环境坏了"与"bug 显现"是两种红（三值判定存在的前提）。

退出码：0=判定完成（三值均为合法判定）| 2=fail-closed | 3=同指纹去重绕过（非错误）
零 LLM：reproduce 阶段全程确定性（宪法 §0：判定归确定性工具）。
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # CI-Workflows 检出根
# 默认自证资产（--baseline-cmd/--sentinel-cmd 可覆写——fixture 自测用快速假件）：
# 基线 = 本流水线自带 fixture 套件全绿（证明环境能执行测试且看得见绿）；
# 哨兵 = 故意必红用例（证明环境看得见红）——两者合成"环境自证"（ADR-0064 决策 3）。
# 路径统一正斜杠：--*-cmd 经 shlex 拆参，反斜杠会被吞（Windows 本地实测 127）
DEFAULT_BASELINE = "bash " + os.path.join(HERE, "tests", "run-tests.sh").replace(os.sep, "/")
DEFAULT_SENTINEL = "python3 " + os.path.join(HERE, "sentinel", "sentinel_red.py").replace(os.sep, "/")
# bug form 字段标题（与 .github 仓 .github/ISSUE_TEMPLATE/bug.yml 逐字对齐——跨仓
# 契约，改一处须同步另一处并引用 ADR-0064）
F_REPO = "受影响的仓（owner/name）"
F_SYMPTOM = "症状签名（一句话 + 关键报错）"
F_STACK = "关键栈（可选）"
F_ENV = "环境指纹（可选）"
F_REPRO = "机器复现用例（可选）"
VERDICTS = ("reproduced", "cannot-reproduce", "inconclusive")
TERMINAL = ("reproduced", "cannot-reproduce")  # 只有终态机器判定参与同指纹绕过
MARKER_RE = re.compile(r"REPRO_OUTCOME:\s*(pass|fail)")
DEDUP_RE_TMPL = r"bugfp:%s[^|]*\|\s*verdict=(reproduced|cannot-reproduce)"


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def argv_of(cmd):
    """--*-cmd 参数统一为字符串（argparse 默认值也是字符串形态），shlex 拆参。"""
    return shlex.split(cmd) if isinstance(cmd, str) else list(cmd)


# ---------- bug form 正文解析（issue form 渲染为 "### 标题" 分节）----------
def parse_form(body):
    out, cur = {}, None
    for line in (body or "").splitlines():
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1)
            out[cur] = ""
        elif cur is not None:
            out[cur] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


# ---------- 同指纹去重（ADR-0064 决策 5：症状指纹 = 仓+症状摘要+关键栈 sha256）----------
def fingerprint(repo, symptom, stack=""):
    canon = "\n".join([repo.strip(), " ".join((symptom or "").split()), " ".join((stack or "").split())])
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def find_prior_verdict(corpus_text, fp):
    """语料（issue 评论/搜索正文）中找同指纹终态判定。搜不到≠不存在——去重面
    失明时宁可重跑全流程：重复 reproduce 只是浪费，误绕过才是漏判。"""
    m = re.search(DEDUP_RE_TMPL % fp, corpus_text or "")
    return m.group(1) if m else None


# ---------- env-gate（决策 2：镜像 digest + lockfile hash 双锁；不匹配=不可判定非证伪）----------
KNOWN_LOCKFILES = ("requirements.txt", "requirements-gate.txt", "package-lock.json",
                   "go.sum", "Cargo.lock", "poetry.lock", "uv.lock")


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def env_evidence(base_dir, env_dir=None):
    """环境可复现性证据（进判定日志）。env_dir（fixture）提供 image.ref + locks/*；
    真实模式扫 base 检出目录 lockfile 集合。镜像 digest：托管 runner 不暴露基础
    镜像 digest，取可复现环境清单（OS/内核/python）sha256 作代理度量——双锁
    证据形态不变（digest + lockfile hash 均落判定日志，ADR-0064 决策 2 意图）。"""
    ev = {"image": None, "lockfiles": {}}
    if env_dir and os.path.isfile(os.path.join(env_dir, "image.ref")):
        ev["image"] = open(os.path.join(env_dir, "image.ref"), encoding="utf-8").read().strip()
    elif not env_dir:
        manifest = json.dumps({"os": platform.system(), "release": platform.release(),
                               "python": platform.python_version(), "machine": platform.machine()},
                              sort_keys=True)
        ev["image"] = "sha256:" + hashlib.sha256(manifest.encode()).hexdigest()
    if base_dir and os.path.isdir(base_dir):
        for name in os.listdir(base_dir):
            if name in KNOWN_LOCKFILES:
                ev["lockfiles"][name] = file_sha256(os.path.join(base_dir, name))
    if env_dir and os.path.isdir(os.path.join(env_dir, "locks")):
        for name in os.listdir(os.path.join(env_dir, "locks")):
            ev["lockfiles"][name] = file_sha256(os.path.join(env_dir, "locks", name))
    return ev


def parse_reported_env(text):
    """上报环境指纹（form 可选字段）：`image: sha256:...` / `lockfile:<名>: sha256:...`。
    字段缺失 = 无对账面（只记录实际证据，不判不匹配）——环境对账是增强不是门槛。"""
    rep = {}
    for line in (text or "").splitlines():
        line = line.strip().lstrip("`-•* ").lower()
        m = re.match(r"^(image|lockfile(?::[\w.\-]+)?)\s*[:=]\s*(sha256:[0-9a-f]{8,64}|[0-9a-f]{8,64})", line)
        if m:
            rep[m.group(1)] = m.group(2) if m.group(2).startswith("sha256:") else "sha256:" + m.group(2)
    return rep


def env_gate(actual, reported):
    """对账：任一上报键与实际不一致 = mismatch（→inconclusive，绝不→证伪）。"""
    mism = []
    if reported.get("image") and reported["image"] != actual.get("image"):
        mism.append(f"image: reported={reported['image']} actual={actual.get('image')}")
    for k, v in reported.items():
        if k.startswith("lockfile") and k in actual.get("lockfiles", {}) and v != actual["lockfiles"][k]:
            mism.append(f"{k}: reported={v} actual={actual['lockfiles'][k]}")
    return (len(mism) == 0, "; ".join(mism))


# ---------- 哨兵环境自证（决策 3：基线全绿 ∧ 故意失败哨兵必须红，缺一 fail-closed）----------
class AttestationError(Exception):
    pass


def run_cmd(argv, timeout_s, cwd=None):
    """运行外部命令。返回 (returncode|None, 输出尾部, 是否超时)。
    统一按 utf-8 解码（fixture 脚本/哨兵含中文输出；Windows 本地默认 cp936 会乱码）。"""
    try:
        p = subprocess.run(argv, cwd=cwd, timeout=timeout_s, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or ""), False
    except subprocess.TimeoutExpired:
        return None, "", True
    except OSError as e:
        return None, str(e), False


SENTINEL_MARKER = "SENTINEL-RED"  # 哨兵自证标记：必红用例输出含此串且 rc!=0
# ——防"命令不存在也算红"的假阳性（如 Windows python3 存根 rc=9009：那是基础设施
# 红，不是断言红；哨兵要证明的是"这套环境能看见断言红"，ADR-0064 决策 3）


def run_sentinel(baseline_cmd, sentinel_cmd, timeout_s, log_dir, attempt):
    """自证 = 双证据 + 日志落盘。日志写不出 / 基线不绿 / 哨兵不红（缺标记或
    rc==0 或超时）→ AttestationError（上层 exit 2 fail-closed，不打任何判定标签）。"""
    rc_b, out_b, to_b = run_cmd(baseline_cmd, timeout_s)
    rc_s, out_s, to_s = run_cmd(sentinel_cmd, timeout_s)
    att = {"attempt": attempt, "ts": now_iso(),
           "baseline": {"cmd": baseline_cmd, "rc": rc_b, "timeout": to_b, "tail": out_b[-500:]},
           "sentinel": {"cmd": sentinel_cmd, "rc": rc_s, "timeout": to_s,
                        "red_marker": SENTINEL_MARKER in out_s, "tail": out_s[-500:]}}
    path = os.path.join(log_dir, f"attestation-{attempt}.json")
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(att, f, ensure_ascii=False, indent=1)
    except OSError as e:
        raise AttestationError(f"自证日志写盘失败：{e}")
    if rc_b != 0:
        raise AttestationError(f"基线套件不绿（rc={rc_b}, timeout={to_b}）——环境未自证，不做判定（AC-1 fail-closed）")
    if rc_s == 0 or to_s or SENTINEL_MARKER not in out_s:
        raise AttestationError(f"哨兵未红（rc={rc_s}, timeout={to_s}, marker={SENTINEL_MARKER in out_s}）"
                               f"——红看不见即判不了红（AC-1 fail-closed）")
    return att


def check_attestation_log(log_dir, attempt):
    """AC-1 执法点：三值判定只认落盘自证日志，不认内存状态——repro 主流程在
    判定前无条件调用本复核；日志缺失/不完整 = 拒绝判定（fail-closed）。"""
    path = os.path.join(log_dir, f"attestation-{attempt}.json")
    try:
        att = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise AttestationError(f"自证日志缺失/不可解析：{path}（{e}）——不做判定（AC-1 fail-closed）")
    if att.get("baseline", {}).get("rc") != 0:
        raise AttestationError("自证日志不完整：基线非绿（AC-1 fail-closed）")
    s = att.get("sentinel", {})
    if s.get("rc") in (None, 0) or not s.get("red_marker"):
        raise AttestationError("自证日志不完整：哨兵非断言红（AC-1 fail-closed）")
    return att


# ---------- repro 运行与三值判定 ----------
def run_repro(state_dir, repro_argv, timeout_s, workroot, pass_workdir=False):
    """在 state_dir 的【新拷贝】上运行 repro（隔离：翻转检测的两采样互不污染）。
    pass_workdir=True（fixture 形态）：workdir 路径作为 repro 脚本末位参数传入。
    status ∈ pass|fail（断言语义，进 F→P 判定）| error|timeout（环境语义，不进）。"""
    work = tempfile.mkdtemp(prefix="repro-", dir=workroot)
    shutil.copytree(state_dir, work, dirs_exist_ok=True)
    argv = repro_argv + [work] if pass_workdir else repro_argv
    rc, out, to = run_cmd(argv, timeout_s, cwd=work)
    markers = MARKER_RE.findall(out)
    if to:
        status = "timeout"
    elif rc is None:
        status = "error"
    else:
        want = markers[-1] if markers else None
        status = "pass" if (want == "pass" and rc == 0) else "fail" if (want == "fail" and rc != 0) else "error"
    return {"status": status, "rc": rc, "marker": markers[-1] if markers else None, "tail": out[-300:]}


def tri_verdict(base_a, base_b, fix_res, env_ok, env_mismatch):
    """纯函数：三值判定核心（fixture 直接断言此处——AC-1/AC-2/AC-3 的判定语义）。"""
    if not env_ok:
        return "inconclusive", f"env-gate 指纹不匹配（不可判定≠证伪，ADR-0064 决策 2）：{env_mismatch}"
    if base_a["status"] in ("error", "timeout") or base_b["status"] in ("error", "timeout"):
        bad = base_a if base_a["status"] in ("error", "timeout") else base_b
        return "inconclusive", f"复现运行非断言异常（{bad['status']}）——环境语义不进 F→P"
    if base_a["status"] != base_b["status"]:
        return "inconclusive", f"翻转异常：两采样 {base_a['status']}/{base_b['status']}（同一 base 稳定性不成立）"
    if base_a["status"] == "pass":
        return "cannot-reproduce", "base 两采样均 pass（不关单，保留人裁——ADR-0064 决策 4）"
    # base 稳定 fail：bug 在 base 上真实显现（F→P 的 F 半边成立）
    if fix_res is None:
        return "reproduced", "base 稳定 fail（F 半边成立；fix 候选未提供——修复流后续验证 P 半边）"
    if fix_res["status"] in ("error", "timeout"):
        return "inconclusive", f"fix 候选运行 {fix_res['status']}——环境语义不进 F→P"
    if fix_res["status"] == "pass":
        return "reproduced", "base fail + fix pass（SWT-bench F→P 完整成立）"
    return "reproduced", "base fail + fix fail：bug 真实（reproduced）；fix 候选未生效（fix-unresolved）"


# ---------- 标签/评论（api = gh CLI + GH_TOKEN[App 令牌]；dry-run = 只打印，自测面）----------
def gh(args, token):
    return subprocess.run(["gh"] + args, env={**os.environ, "GH_TOKEN": token},
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def ensure_label(token, repo, name):
    """标签不存在则创建（幂等）——治理标签全集不含 bug 流标签（新增式，随流水线
    自管理）。422=已存在（含 already_exists 错误码的形态），其余失败仅告警：
    加标签一步会再暴露真实错误（验证面后置不掩盖）。"""
    r = gh(["api", f"repos/{repo}/labels", "-f", f"name={name}", "-f", "color=BFD4F2",
            "-f", "description=bugflow W3-C1 (ADR-0064)"], token)
    if r.returncode != 0 and "422" not in (r.stderr or ""):
        print(f"WARN ensure_label({name}) rc={r.returncode} {(r.stderr or '').strip()[:120]}")


def apply_labels(mode, token, repo, issue, add, remove=()):
    """状态真相源 = issue label（宪法 §12）。写序=先移旧态再置新态——不留双
    state 并存窗口（conductor 对多状态标签并存是 abort 红而非 no-op，见其
    路由步注释）。api 写失败不掩盖判定（判定日志/评论已落）——warn 后继续，
    标签可人工补；这区别于哨兵 fail-closed（后者是判定有效性前提，前者是
    结果投递面）。"""
    if mode == "dry-run":
        print(f"LABEL-DRYRUN repo={repo} issue={issue} add={sorted(add)} remove={sorted(remove)}")
        return
    for n in remove:
        gh(["api", "-X", "DELETE",
            f"repos/{repo}/issues/{issue}/labels/" + urllib.parse.quote(n, safe="")], token)
    for n in add:
        ensure_label(token, repo, n)
        r = gh(["api", f"repos/{repo}/issues/{issue}/labels", "-f", f"labels[]={n}"], token)
        if r.returncode != 0:
            print(f"WARN 加标签 {n} 失败：{(r.stderr or '').strip()[:120]}（判定已成立，可人工补标）")


def issue_comment(mode, token, repo, issue, body):
    if mode == "dry-run":
        print("COMMENT-DRYRUN:", body.replace("\n", " | ")[:220])
        return
    gh(["api", f"repos/{repo}/issues/{issue}/comments", "-f", f"body={body}"], token)


# ---------- repro 子命令主流程 ----------
def cmd_repro(a):
    if a.body_file:
        body = open(a.body_file, encoding="utf-8").read()
    else:
        r = gh(["api", f"repos/{a.repo}/issues/{a.issue}", "--jq", '.body // ""'], a.token)
        if r.returncode != 0:
            die(2, f"issue 正文拉取失败（fail-closed）：{(r.stderr or '').strip()[:150]}")
        body = r.stdout
    if not body.strip():
        die(2, "issue 正文为空——无法提取结构化字段（fail-closed）")
    form = parse_form(body)
    repo_field = (form.get(F_REPO) or a.repo).split()[0].strip("`")
    symptom, stack = form.get(F_SYMPTOM, ""), form.get(F_STACK, "")
    fp = fingerprint(repo_field, symptom, stack)
    reported_env = parse_reported_env(form.get(F_ENV, ""))
    os.makedirs(a.log_dir, exist_ok=True)
    print(f"fingerprint=bugfp:{fp}")

    # ---- 同指纹去重（AC-2 后半：同指纹二次上报绕过重复 reproduce）----
    corpus = open(a.corpus_file, encoding="utf-8").read() if a.corpus_file else ""
    if a.gh_dedup:
        r = gh(["api", f"repos/{a.repo}/issues/{a.issue}/comments", "--paginate",
                "--jq", ".[].body"], a.token)
        if r.returncode == 0:
            corpus += r.stdout
        else:
            print("WARN 同 issue 评论拉取失败——去重面失明，按无先例处理（宁可重跑不误绕过）")
        r = gh(["api", "search/issues", "-f", f"q=repo:{a.repo} \"bugfp:{fp}\" is:issue",
                "--jq", ".items[].body"], a.token)
        if r.returncode == 0:
            corpus += r.stdout
    prior = find_prior_verdict(corpus, fp)
    if prior:
        apply_labels(a.label_mode, a.token, a.repo, a.issue, ["duplicate-fingerprint"])
        issue_comment(a.label_mode, a.token, a.repo, a.issue,
                      f"**同指纹绕过**（ADR-0064 决策 5）：`bugfp:{fp} | verdict={prior}` "
                      f"已有终态判定，本次跳过重复 reproduce。如需重判由 owner 人工处理。")
        print(f"BYPASS prior-verdict={prior}")
        return 3

    # ---- 入口状态（免签：bug + state:bug 即 reproduce 阶段，transitions.yaml B1）----
    apply_labels(a.label_mode, a.token, a.repo, a.issue, ["bug", "state:bug"])

    # ---- 场景解析（fixture 目录 or 真实 checkout 目录）----
    base_dir, fix_dir, env_dir = a.base_dir, a.fix_dir, a.env_dir
    repro_argv, pass_workdir = None, False
    if a.scenario:
        s = a.scenario if os.path.isdir(a.scenario) else os.path.join(HERE, "tests", "fixtures", a.scenario)
        man = json.load(open(os.path.join(s, "manifest.json"), encoding="utf-8"))
        base_dir = os.path.join(s, "base")
        fix_dir = os.path.join(s, "fix") if os.path.isdir(os.path.join(s, "fix")) else None
        env_dir = os.path.join(s, "env")
        repro_argv, pass_workdir = ["bash", os.path.join(s, "repro.sh")], True
        reported_env = man.get("reported_env", reported_env)
        if man.get("timeout_s"):
            a.timeout = man["timeout_s"]
    else:
        cmd = a.repro_cmd
        if not cmd:  # 真实模式缺省取 issue form 的"机器复现用例"字段首行
            lines = form.get(F_REPRO, "").strip().splitlines()
            cmd = lines[0].strip() if lines else ""
        if not cmd:
            die(2, "无复现用例（--repro_cmd / --scenario / issue 机器复现用例字段均空）——fail-closed")
        repro_argv = ["bash", "-lc", cmd]
    if not (base_dir and os.path.isdir(base_dir)):
        die(2, f"base 目录不存在：{base_dir}（fail-closed）")

    # ---- 判定主循环（AC-3：inconclusive → 换新环境重试一次 → 仍不可判定 → needs-human；
    # 每次尝试都重新自证（新 attestation 日志）——"换新环境"在 workflow 层=新 runner，
    # 进程内=全新临时目录+完整重自证，语义一致、成本可承受）----
    attempts, verdict, detail = [], "inconclusive", "未运行"
    for i in range(1 + max(0, a.retry)):
        run_sentinel(argv_of(a.baseline_cmd), argv_of(a.sentinel_cmd), a.attest_timeout, a.log_dir, i)
        check_attestation_log(a.log_dir, i)  # 只认落盘证据（AC-1）
        env = env_evidence(base_dir, env_dir)
        ok, mism = env_gate(env, reported_env)
        with tempfile.TemporaryDirectory(prefix="bugflow-") as wr:
            ba = run_repro(base_dir, repro_argv, a.timeout, wr, pass_workdir)
            bb = run_repro(base_dir, repro_argv, a.timeout, wr, pass_workdir)
            fx = run_repro(fix_dir, repro_argv, a.timeout, wr, pass_workdir) if fix_dir else None
        verdict, detail = tri_verdict(ba, bb, fx, ok, mism)
        attempts.append({"attempt": i, "env": env, "env_gate_ok": ok,
                         "base": [ba["status"], bb["status"]], "fix": fx["status"] if fx else None,
                         "verdict": verdict, "detail": detail})
        print(f"attempt-{i} base={ba['status']}/{bb['status']} fix={fx['status'] if fx else '-'} "
              f"env_gate={'ok' if ok else 'MISMATCH'} verdict={verdict}")
        if verdict != "inconclusive":
            break

    # ---- 判定落账（verdict.json + 账本 JSONL——AC-4 抽样数据源）----
    rec = {"schema": "bugflow-verdict/v1", "ts": now_iso(), "run_id": a.run_id, "repo": a.repo,
           "issue": a.issue, "fingerprint": f"bugfp:{fp}", "verdict": verdict, "detail": detail,
           "attempts": attempts, "base_ref": a.base_ref, "fix_ref": a.fix_ref}
    with open(os.path.join(a.log_dir, "verdict.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    os.makedirs(os.path.dirname(os.path.abspath(a.ledger)), exist_ok=True)
    with open(a.ledger, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- 标签 + 评论（transitions.yaml B2/B4/B5；cannot-reproduce 不转移状态=保留人裁）----
    if verdict == "reproduced":
        add, remove = ["reproduced", "state:reproduced"], ["state:bug"]
    elif verdict == "cannot-reproduce":
        add, remove = ["cannot-reproduce"], []
    else:
        add, remove = ["inconclusive", "state:needs-human"], ["state:bug"]
    apply_labels(a.label_mode, a.token, a.repo, a.issue, add, remove)
    issue_comment(a.label_mode, a.token, a.repo, a.issue,
                  f"**reproduce 三值判定：{verdict}**（{len(attempts)} 次尝试，ADR-0064）\n"
                  f"`bugfp:{fp} | verdict={verdict} | run={a.run_id}`\n{detail}\n"
                  + ("换新环境重试仍不可判定 → 转人裁（state:needs-human）。" if verdict == "inconclusive" else ""))
    return 0


# ---------- sample-week 子命令（AC-4：每周抽 3 单，误关率人工复核清单）----------
def in_week(ts, week):
    d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).isocalendar()
    return f"{d.year}-W{d.week:02d}" == week


def cmd_sample_week(a):
    recs = [json.loads(l) for l in open(a.ledger, encoding="utf-8") if l.strip()]
    week_recs = sorted([r for r in recs if r.get("verdict") in VERDICTS + ("needs-human",)
                        and in_week(r.get("ts", ""), a.week)], key=lambda r: r["ts"])
    rnd = random.Random(int(hashlib.sha256(a.week.encode()).hexdigest()[:12], 16))  # 种子=ISO 周：抽样可复现
    sample = rnd.sample(week_recs, min(a.size, len(week_recs)))
    out = {"schema": "bugflow-weekly-sample/v1", "week": a.week, "population": len(week_recs),
           "sampled": sample, "misclosure_rate": None,  # 周审计人工复核后回填（宪法 §3/§7）
           "note": "误关率抽样清单（每周 3 单）——dashboard 联动属 W5-C4，本件为数据源 artifact"}
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False) + "\n")
    print(f"week={a.week} population={len(week_recs)} sampled={len(sample)} -> {a.out}")
    for s in sample:
        print(f"  sample: {s['repo']}#{s['issue']} verdict={s['verdict']} fp={s['fingerprint'][:23]}…")
    return 0


def main():
    p = argparse.ArgumentParser(description="Bug 流 reproduce 阶段（W3-C1 .github#218，ADR-0064）")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("repro")
    r.add_argument("--repo", required=True)
    r.add_argument("--issue", required=True)
    r.add_argument("--body-file", help="issue 正文文件（缺省经 gh api 拉取）")
    r.add_argument("--corpus-file", help="去重语料文件（缺省空；--gh-dedup 在线聚合）")
    r.add_argument("--gh-dedup", action="store_true", help="经 gh 聚合 issue 评论+搜索作去重语料（生产模式）")
    r.add_argument("--scenario", help="fixture 场景名/路径（tests/fixtures/ 下）")
    r.add_argument("--base-dir")
    r.add_argument("--fix-dir")
    r.add_argument("--env-dir")
    r.add_argument("--base-ref")
    r.add_argument("--fix-ref")
    r.add_argument("--repro-cmd")
    r.add_argument("--timeout", type=int, default=1800)  # ADR-0064 决策 4：30min 超时上限（repro 用例）
    r.add_argument("--attest-timeout", type=int, default=300)  # 自证资产（本仓套件）独立超时——fixture 场景把 repro 超时压到秒级时自证不被殃及
    r.add_argument("--retry", type=int, default=1)       # inconclusive → 换新环境重试一次
    r.add_argument("--label-mode", choices=["dry-run", "api"], default="dry-run")
    r.add_argument("--token", default=os.environ.get("GH_TOKEN", ""))
    r.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    r.add_argument("--log-dir", default="bugflow-logs")
    r.add_argument("--ledger", default=os.path.join("bugflow-logs", "verdicts.jsonl"))
    r.add_argument("--baseline-cmd", default=DEFAULT_BASELINE)
    r.add_argument("--sentinel-cmd", default=DEFAULT_SENTINEL)
    s = sub.add_parser("sample-week")
    s.add_argument("--ledger", required=True)
    s.add_argument("--week", required=True, help="ISO 周，如 2026-W34")
    s.add_argument("--size", type=int, default=3)
    s.add_argument("--out", default="weekly-sample.jsonl")
    a = p.parse_args()
    try:
        rc = cmd_repro(a) if a.cmd == "repro" else cmd_sample_week(a)
    except AttestationError as e:
        die(2, f"FAIL-CLOSED（AC-1）：{e}")
    sys.exit(rc)


if __name__ == "__main__":
    main()
