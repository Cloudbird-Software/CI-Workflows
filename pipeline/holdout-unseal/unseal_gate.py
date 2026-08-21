#!/usr/bin/env python3
# unseal_gate.py —— holdout 揭封 gate（W4-C3 .github#222 / ADR-0068；宪法 §4B/§4E）
#
# 由 verdict 阶段调用（.github/workflows/holdout-unseal.yml）。流程（ADR-0068 决策 2/3/6）：
#   1) sealed_sha256 校验（公式=ADR-0056 canonical JSON；条目级 files[].sha256 同验）
#      ——不匹配=fail-closed exit 3（试卷被篡改，拒揭封）
#   2) 解封只落系统临时目录（tempfile.mkdtemp）——绝不落盘进调用仓工作区：
#      诱饵/试卷内容不进主仓树（宪法 §6；canary 条目 kind 不符永不解封）
#   3) 执行（pytest）→ stdout 只出计数与百分比（"holdout: N/M 通过"）——PR check
#      零明细零测试名（决策 3；运行日志由 audit_outputs.py scan 模式复核）
#   4) 通过率差 = 主套件 − holdout > 阈值（config.json，缺省 5%）→ exit 1 =
#      needs-human 升级 + 该 PR verdict 不过（决策 6；打 state:needs-human 标签属
#      verdict 接线，本 gate 出 exit 1 + escalation 记录）
#   5) 失败明细只写 --detail-out（→ holdout 仓 issue，由 workflow 持专用凭据写回）；
#      strict 模式需写明细而无凭据（GH_TOKEN 空）= fail-closed exit 2 并注明
#   6) 揭封记录 --record-out（含 sealed_sha256 校验结果）→ holdout scripts/unseal-log.py
#      append-only 台账（AC-3）；--banned-out 供审计器扫描日志
# 退出码：0=过 | 1=通过率差超阈 | 2=环境/凭据 fail-closed | 3=sealed_sha256 不符
import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

RECORD_SCHEMA = "holdout-unseal-record/1"
DETAIL_SCHEMA = "holdout-unseal-detail/1"
ENTRY_KIND = "sealed-test-set"
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def canon(obj) -> str:
    """与 holdout scripts/validate_entries.py 完全同式——封存哈希公式单一事实源。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha_hex(obj) -> str:
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def now():
    return datetime.now(timezone.utc).strftime(TS_FMT)


def fail(code: int, msg: str) -> int:
    print(f"::error::{msg}")
    return code


def parse_rate(s: str) -> float:
    """主套件通过率：0..1 小数或带 % 百分数。"""
    v = s.strip().rstrip("%")
    rate = float(v) / (100.0 if s.strip().endswith("%") else 1.0)
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"通过率须在 [0,1]/[0%..100%]: {s!r}")
    return rate


def select_entries(root: Path, only=None):
    """只挑 payload.kind=sealed-test-set 条目（canary 等其他条目永不解封执行）。"""
    out = {}
    for f in sorted(root.glob("entries/HO-????.json")):
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"::error::条目读取失败（fail-closed）: {f.name}: {exc}")
            sys.exit(2)
        if e.get("payload", {}).get("kind") == ENTRY_KIND and (not only or e.get("id") in only):
            out[e["id"]] = e
    return out


def decode_entry(eid: str, entry: dict, outdir: Path) -> bool:
    """sealed_sha256+files[].sha256 双验后解封；任一不符 False（exit 3 路径）。"""
    if entry.get("sealed_sha256") != sha_hex(entry.get("payload")):
        return False
    for f in entry["payload"].get("files", []):
        name = f.get("name", "")
        if not re.match(r"^[A-Za-z0-9_.\-]+\.py$", name):  # 防路径逃逸（seal.py 同款白名单）
            print(f"::error::{eid} 文件名非法（拒落盘）")
            return False
        try:
            raw = base64.b64decode(f.get("content_b64", ""), validate=True)
        except (binascii.Error, ValueError):
            return False
        if hashlib.sha256(raw).hexdigest() != f.get("sha256"):
            return False
        (outdir / name).write_bytes(raw)
    return True


def run_pytest(testdir: Path):
    """执行并只回计数/节点结果（原始输出不外泄——内存内解析，绝不 print）。
    隔离三件套（pytest9/Windows 实测：宿主 %TEMP% 高频建删文件会让 pytest 的
    tmp 工厂扫描竞态出 collection error）：cwd=测试目录+相对参数、-p no:tmpdir
    （sealed 测试不用 tmp_path，禁之无失）、TMP 指向独立目录。"""
    cmd = [sys.executable, "-m", "pytest", ".", "-q", "--tb=no", "-rA",
           "--no-header", "-p", "no:cacheprovider", "-p", "no:tmpdir", "--color=no"]
    workroot = Path(tempfile.mkdtemp(prefix="holdout-unseal-tmp-"))
    env = {**os.environ, "TMP": str(workroot), "TEMP": str(workroot), "TMPDIR": str(workroot)}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           timeout=600, env=env, cwd=str(testdir))
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"::error::pytest 执行失败（fail-closed）: {exc}")
        sys.exit(2)
    if p.returncode not in (0, 1):
        print(f"::error::pytest 异常退出 {p.returncode}（0/1 之外=环境问题，fail-closed）")
        sys.exit(2)
    text = p.stdout
    def n(pat):
        m = re.search(r"(\d+) " + pat, text)
        return int(m.group(1)) if m else 0
    passed, failed, errors = n("passed"), n("failed"), n("error") + n("errors")
    nodes = {"passed": [], "failed": []}
    for m in re.finditer(r"^(PASSED|FAILED) (\S+::\S+)", text, re.M):
        # Windows 绝对路径形态：node 渲染成 "::::Users::…::test_x.py::test_y"（分隔符
        # 全为 ::）——取末两段（文件名+测试名）统一双平台形态，剥 runner 临时路径前缀
        segs = [s for s in m.group(2).split("::") if s]
        nodes[m.group(1).lower()].append("::".join(segs[-2:]) if len(segs) >= 2 else m.group(2))
    return passed, failed, errors, nodes


def main() -> int:
    ap = argparse.ArgumentParser(description="holdout 揭封 gate（W4-C3 / ADR-0068）")
    ap.add_argument("--holdout-root", required=True, help="holdout 仓 checkout（公开只读 clone 即可）")
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    ap.add_argument("--main-pass-rate", required=True, help="主套件通过率（0..1 或 98% 形）")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "unknown"))
    ap.add_argument("--pr", type=int, default=0)  # AC-3 台账字段
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    ap.add_argument("--entries", default="", help="逗号分隔条目 id 子集（缺省=全部 sealed-test-set）")
    ap.add_argument("--record-out", required=True, help="揭封记录 JSON（→ unseal-log.py 台账）")
    ap.add_argument("--detail-out", required=True, help="失败明细文件（→ holdout 仓 issue；含测试名）")
    ap.add_argument("--banned-out", default="", help="禁出词表（测试名/文件名→审计器扫描日志用）")
    ap.add_argument("--always-detail", action="store_true", help="无失败也写明细（演示）")
    ap.add_argument("--detail-mode", choices=["strict", "artifact"], default="strict",
                    help="strict=需写明细而无凭据则 fail-closed；artifact=明细仅落文件（演示）")
    args = ap.parse_args()

    def write_record(verdict, entries, passed, total, gap=0.0, escalated=False, rate_h=None, rate_m=None):
        rec = {"schema": RECORD_SCHEMA, "ts": now(), "repo": args.repo, "pr": args.pr,
               "run_id": str(args.run_id), "verdict": verdict, "entries": entries,
               "passed": passed, "total": total, "gap_pct": round(gap, 2),
               "threshold_pct": cfg["pass_rate_gap_threshold_pct"], "escalated": escalated}
        if rate_h is not None:
            rec.update(main_pass_rate=round(rate_m, 4), holdout_pass_rate=round(rate_h, 4))
        Path(args.record_out).write_text(json.dumps(rec, ensure_ascii=False, indent=1),
                                         encoding="utf-8", newline="\n")

    try:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail(2, f"config 读取失败（fail-closed）: {args.config}: {exc}")
    try:
        main_rate = parse_rate(args.main_pass_rate)
    except ValueError as exc:
        return fail(2, f"--main-pass-rate 非法: {exc}")

    only = {s.strip() for s in args.entries.split(",") if s.strip()} or None
    entries = select_entries(Path(args.holdout_root), only)
    if not entries:
        return fail(2, "无可解封条目（payload.kind=sealed-test-set 为空）——揭封不可空跑")
    ents_meta = [{"id": i, "sha8": e["sealed_sha256"][:8], "verify": True} for i, e in entries.items()]

    # ---- 1. sealed_sha256 校验（决策 2：不匹配=fail-closed 拒揭）----
    testdir = Path(tempfile.mkdtemp(prefix="holdout-unseal-"))  # 系统临时目录，不在调用仓
    for i, e in entries.items():
        if not decode_entry(i, e, testdir):
            bad = {"id": i, "sha8": e.get("sealed_sha256", "?")[:8], "verify": False}
            write_record("tamper", [bad], 0, 0)
            return fail(3, f"sealed_sha256 校验失败：{i}@{bad['sha8']}（试卷被篡改，拒揭封）")

    # ---- 2. 执行 + 计数（stdout 只出计数——决策 3）----
    passed, failed, errors, nodes = run_pytest(testdir)
    total = passed + failed + errors
    if total == 0:
        write_record("env-fail", ents_meta, 0, 0)
        return fail(2, "holdout 测试零执行（全 skip/未收集）——试卷不可判定，fail-closed")
    hold_rate = passed / total
    gap = (main_rate - hold_rate) * 100.0
    thr = float(cfg["pass_rate_gap_threshold_pct"])
    escalated = gap > thr

    print(f"holdout: {passed}/{total} 通过")  # PR check 唯一结果行——无明细（AC-1）
    print(f"通过率: 主套件 {main_rate*100:.1f}% vs holdout {hold_rate*100:.1f}% → 差 {gap:.1f}%（阈值 {thr:g}%）")

    # ---- 3. 明细/禁出词表（只落文件，绝不进 stdout）----
    detail_needed = failed + errors > 0 or args.always_detail
    detail_entries = []
    for i, e in entries.items():
        sha8 = next(m["sha8"] for m in ents_meta if m["id"] == i)
        fnames = {f["name"] for f in e["payload"].get("files", [])}
        detail_entries.append({"id": i, "sha8": sha8,
                               "failures": [n for n in nodes["failed"] if n.split("::")[0] in fnames]})
    detail = {"schema": DETAIL_SCHEMA, "ts": now(), "repo": args.repo, "pr": args.pr,
              "run_id": str(args.run_id), "threshold_pct": thr, "main_pass_rate": round(main_rate, 4),
              "holdout_pass_rate": round(hold_rate, 4), "gap_pct": round(gap, 2),
              "escalated": escalated, "passed": passed, "total": total, "entries": detail_entries}
    Path(args.detail_out).write_text(
        "## unseal-detail（机器写回 holdout 仓——W4-C3 / ADR-0068 决策 3；内容仅 owner/verdict 可达）\n\n"
        "```json\n" + json.dumps(detail, ensure_ascii=False, indent=1) + "\n```\n",
        encoding="utf-8", newline="\n")
    if args.banned_out:
        names = {f["name"] for e in entries.values() for f in e["payload"]["files"]}
        Path(args.banned_out).write_text("\n".join(sorted(names | set(nodes["passed"]) | set(nodes["failed"]))),
                                         encoding="utf-8", newline="\n")

    # ---- 4. 无凭据 fail-closed（strict：需要写回 holdout 却没有专用凭据）----
    if detail_needed and args.detail_mode == "strict" and not os.environ.get("GH_TOKEN"):
        write_record("env-fail", ents_meta, passed, total, gap, escalated, hold_rate, main_rate)
        return fail(2, "无 holdout 写回凭据（HOLDOUT_UNSEAL_TOKEN 缺席）——失败明细无处可达，"
                       "fail-closed：verdict 不过，绝不降级为'跑过但不可审计'（ADR-0068 决策 3）")

    write_record("gap-escalated" if escalated else "pass", ents_meta, passed, total, gap, escalated,
                 hold_rate, main_rate)
    if escalated:
        return fail(1, f"通过率差 {gap:.1f}% > 阈值 {thr:g}%——升级 needs-human（state:needs-human），"
                       f"该 PR verdict 不通过（主套件绿而 holdout 显著差=对主套件调参信号，ADR-0068 决策 6）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
