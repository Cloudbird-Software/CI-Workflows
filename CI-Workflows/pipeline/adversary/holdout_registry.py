#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""holdout_registry.py —— holdout 注册 / hash 校验 / PR 引用一致性检查（W2-C4 .github#276 / AC-3 / AC-17 / ADR-0068）

holdout 注册主体 = 验证者 APP（verifier_app.id 由 expected-state.json 注入）。
本模块实现三类机器校验：
  1. sealed_sha256 校验：复用 ADR-0068 揭封 hash 校验（canonical JSON 公式
     = holdout/scripts/seal.py 同款），条目哈希不符即判红（fail-closed）；
  2. PR 引用一致性：PR body 引用的 holdout hash 必须与已注册记录一致，
     防 PR 声明与实际测试脱钩；
  3. 注册身份校验：非验证者 APP（或 owner）写入 holdout 内容被拒 403
     （验证者 APP 未创建时以配置占位符 / 条件判断降级，不阻断非注册路径）。

用法:
  python3 holdout_registry.py <command> [options]
    verify-hash   校验 holdout 仓所有条目 sealed_sha256（ADR-0068 揭封公式）
    check-pr      PR body 引用的 holdout hash 与注册记录一致性
    register      注册一条 holdout 引用（身份校验 + hash 记录）

退出码：0=通过 | 1=校验失败 | 2=配置/环境错误（fail-closed）
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))

# ADR-0056 canonical JSON 公式——与 holdout/scripts/seal.py 完全同式（单一事实源）
CANON_SEPARATORS = (",", ":")


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=CANON_SEPARATORS)


def sha_hex(obj) -> str:
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(code: int, msg: str) -> None:
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# ADR-0068 sealed_sha256 校验（复用揭封 hash 校验，AC-3 / AC-17）
# ---------------------------------------------------------------------------

def verify_entry_hash(entry: dict) -> tuple[bool, str]:
    """校验单条 holdout 条目 sealed_sha256。返回 (ok, reason)。"""
    eid = entry.get("id") or "(无 id)"
    payload = entry.get("payload")
    sealed = entry.get("sealed_sha256")
    if not isinstance(payload, dict):
        return False, f"{eid}: payload 不是对象"
    if not isinstance(sealed, str) or len(sealed) != 64:
        return False, f"{eid}: sealed_sha256 非法（需 64 位 hex）"
    expected = sha_hex(payload)
    if expected != sealed:
        return False, (f"{eid}: sealed_sha256 不符（条目可能被篡改）"
                       f" sealed={sealed[:16]}… computed={expected[:16]}…")
    # 条目级 files[].sha256 同验（unseal_gate.decode_entry 同款）
    for f in (payload.get("files") or []):
        name = f.get("name", "")
        content_b64 = f.get("content_b64", "")
        file_sha = f.get("sha256", "")
        try:
            raw = base64.b64decode(content_b64, validate=True)
        except Exception:
            return False, f"{eid}/{name}: content_b64 非法"
        if hashlib.sha256(raw).hexdigest() != file_sha:
            return False, f"{eid}/{name}: 文件 sha256 不符"
    return True, "ok"


def load_entries(holdout_root: str) -> dict[str, dict]:
    """加载 holdout 仓 entries/HO-NNNN.json（按 id 索引）。"""
    root = Path(holdout_root)
    entries_dir = root / "entries"
    if not entries_dir.is_dir():
        die(2, f"holdout entries 目录不存在: {entries_dir}")
    entries: dict[str, dict] = {}
    for p in sorted(entries_dir.glob("HO-????.json")):
        try:
            e = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            die(2, f"条目读取失败 {p.name}: {exc}")
        if not isinstance(e, dict) or "id" not in e:
            die(2, f"条目格式非法 {p.name}")
        entries[e["id"]] = e
    return entries


def build_hash_index(entries: dict[str, dict]) -> dict[str, str]:
    """id → sealed_sha256 索引，供 PR 引用一致性校验。"""
    return {eid: e.get("sealed_sha256", "") for eid, e in entries.items()}


# ---------------------------------------------------------------------------
# PR 引用一致性校验（AC-3：PR 引用的 holdout hash 须与注册一致）
# ---------------------------------------------------------------------------

HOLDOUT_REF_RE = re.compile(
    r'(?:holdout|HO-[0-9]{4}).*?(?:sha256[:=]\s*([0-9a-f]{64})|([0-9a-f]{64}))',
    re.IGNORECASE,
)
HOLDOUT_ID_RE = re.compile(r'HO-[0-9]{4}', re.IGNORECASE)


def check_pr_references(pr_body: str, hash_index: dict[str, str], pr_ref: str = "") -> dict:
    """校验 PR body 引用的 holdout id/hash 与注册记录一致。"""
    referenced_ids = sorted(set(HOLDOUT_ID_RE.findall(pr_body or "")))
    referenced_shas = [m.group(1) or m.group(2) for m in HOLDOUT_REF_RE.finditer(pr_body or "")
                      if (m.group(1) or m.group(2))]

    violations: list[str] = []
    unknown_ids: list[str] = []
    for rid in referenced_ids:
        if rid not in hash_index:
            unknown_ids.append(rid)
            violations.append(f"PR 引用未注册 holdout id: {rid}（AC-17：注册是引用前置）")

    # 若 PR 同时给出 hash，须与对应 id 的 sealed_sha256 一致
    if referenced_shas and referenced_ids:
        for rid in referenced_ids:
            expected_sha = hash_index.get(rid, "")
            for s in referenced_shas:
                if expected_sha and s != expected_sha:
                    violations.append(
                        f"PR 中 hash {s[:16]}… 与 {rid} 注册 sealed_sha256 {expected_sha[:16]}… 不一致")

    return {
        "schema": "holdout-pr-check/v1",
        "pr_ref": pr_ref,
        "referenced_ids": referenced_ids,
        "referenced_shas_count": len(referenced_shas),
        "violations": violations,
        "ok": len(violations) == 0,
    }


# ---------------------------------------------------------------------------
# 注册身份校验（AC-17 / IFACE-01：非验证者 APP 写入被拒）
# ---------------------------------------------------------------------------

def resolve_verifier_id() -> tuple[str | None, str]:
    """解析验证者 APP id。

    优先读 expected-state.json#verifier_app.id；未配置返回 (None, source)。
    """
    for candidate in (
        os.environ.get("VERIFIER_APP_ID"),
        os.environ.get("CB_VERIFIER_APP_ID"),
    ):
        if candidate and candidate not in ("null", "None", ""):
            return candidate, "env"
    # 尝试从 CI-Workflows 同级 expected-state.json 读取
    expected_state_path = os.path.join(HERE, "..", "..", "..", ".github",
                                       "governance", "expected-state.json")
    if os.path.isfile(expected_state_path):
        try:
            st = json.loads(Path(expected_state_path).read_text(encoding="utf-8"))
            vid = ((st.get("verifier_app") or {}).get("id")
                    or os.environ.get("VERIFIER_APP_ID"))
            if vid and str(vid) not in ("null", "None", ""):
                return str(vid), "expected-state.json"
        except Exception:  # noqa: BLE001
            pass
    return None, "unconfigured"


def check_register_identity(actor: str) -> dict:
    """校验注册操作身份。

    验证者 APP 未创建（id=null）时：
      - 若环境显式 VERIFIER_APP_ID 已配置则按之校验；
      - 否则降级为「仅记录待验证身份」不阻断（代码先实现，身份面补位）。
    任何非验证者 APP / 非 owner 写入 holdout 内容一律被拒 403。
    """
    verifier_id, src = resolve_verifier_id()
    is_owner = actor.startswith("owner:") or actor == os.environ.get("ORG_OWNER", "")
    result = {
        "schema": "holdout-identity-check/v1",
        "actor": actor,
        "verifier_id": verifier_id,
        "verifier_id_source": src,
        "is_owner": is_owner,
        "allowed": False,
        "reason": "",
    }
    if verifier_id is None:
        # 验证者 APP 未创建：代码已实现身份面，当前降级记录（IFACE-01 时序约束）
        result["allowed"] = True
        result["reason"] = (f"验证者 APP 未创建（expected-state verifier_app.id=null，"
                            f"source={src}）——身份面已占位，写入留痕待补验（AC-18 时序约束）")
        result["degraded"] = True
        return result
    is_verifier = actor == f"verifier-app:{verifier_id}" or actor.endswith("[bot]") and verifier_id in actor
    if is_verifier or is_owner:
        result["allowed"] = True
        result["reason"] = f"允许：{'验证者 APP' if is_verifier else 'owner'}（verifier_id={verifier_id}）"
    else:
        result["reason"] = (f"拒绝：actor={actor} 非验证者 APP（id={verifier_id}）且非 owner —— "
                            f"非验证者 APP 写入 holdout 内容被拒 403（AC-17）")
    return result


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def cmd_verify_hash(args) -> int:
    entries = load_entries(args.holdout_root)
    if not entries:
        die(2, "holdout 仓无有效条目 —— 不可空验")
    all_ok = True
    results = []
    for eid in sorted(entries):
        ok, reason = verify_entry_hash(entries[eid])
        mark = "✓" if ok else "✗"
        print(f"  {mark} {eid}: {reason}")
        results.append({"id": eid, "ok": ok, "reason": reason})
        all_ok = all_ok and ok
    summary = {"schema": "holdout-hash-verify/v1", "ts": now_iso(),
               "total": len(entries), "ok_count": sum(1 for r in results if r["ok"]),
               "results": results, "ok": all_ok}
    if args.report_out:
        Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                         encoding="utf-8", newline="\n")
    if not all_ok:
        die(1, f"holdout hash 校验失败 —— 存在被篡改条目（ADR-0068 fail-closed）")
    print(f"全部 {len(entries)} 条 holdout 条目 sealed_sha256 校验通过")
    return 0


def cmd_check_pr(args) -> int:
    entries = load_entries(args.holdout_root)
    hash_index = build_hash_index(entries)
    body = ""
    if args.pr_body_file:
        body = Path(args.pr_body_file).read_text(encoding="utf-8")
    elif args.pr_body:
        body = args.pr_body
    report = check_pr_references(body, hash_index, pr_ref=args.pr_ref)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report_out:
        Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8", newline="\n")
    return 0 if report["ok"] else 1


def cmd_register(args) -> int:
    identity = check_register_identity(args.actor)
    print(json.dumps(identity, ensure_ascii=False, indent=2))
    if not identity["allowed"]:
        die(1, identity["reason"])
    rec = {
        "schema": "holdout-register/v1",
        "ts": now_iso(),
        "actor": args.actor,
        "entry_id": args.entry_id,
        "sealed_sha256": args.sealed_sha256,
        "identity": identity,
    }
    if args.record_out:
        Path(args.record_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.record_out).write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                                         encoding="utf-8", newline="\n")
    print(f"注册留痕: entry={args.entry_id} by {args.actor}"
          f"{' [degraded: 验证者APP未创建]' if identity.get('degraded') else ''}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="holdout_registry.py",
                                 description="holdout 注册 / hash 校验 / PR 引用一致性")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_hash = sub.add_parser("verify-hash", help="校验 holdout 条目 sealed_sha256（ADR-0068）")
    p_hash.add_argument("--holdout-root", required=True, help="holdout 仓 checkout 根")
    p_hash.add_argument("--report-out", default=None)

    p_pr = sub.add_parser("check-pr", help="PR body 引用一致性校验（AC-3）")
    p_pr.add_argument("--holdout-root", required=True)
    p_pr.add_argument("--pr-body", default="", help="PR body 文本")
    p_pr.add_argument("--pr-body-file", default="", help="PR body 文件路径")
    p_pr.add_argument("--pr-ref", default="", help="PR 引用标识（日志用）")
    p_pr.add_argument("--report-out", default=None)

    p_reg = sub.add_parser("register", help="holdout 注册留痕 + 身份校验")
    p_reg.add_argument("--actor", required=True, help="操作身份（app:xxx 或 owner:xxx）")
    p_reg.add_argument("--entry-id", required=True, help="holdout 条目 id")
    p_reg.add_argument("--sealed-sha256", required=True, help="注册时 sealed_sha256")
    p_reg.add_argument("--record-out", default=None, help="注册记录输出路径")

    args = ap.parse_args(argv)
    if args.cmd == "verify-hash":
        return cmd_verify_hash(args)
    if args.cmd == "check-pr":
        return cmd_check_pr(args)
    if args.cmd == "register":
        return cmd_register(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
