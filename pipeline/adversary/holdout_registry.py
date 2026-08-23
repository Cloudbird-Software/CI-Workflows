#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""holdout_registry.py —— holdout 注册/哈希校验/PR 引用一致性检查（W2-C4 .github#276，AC-3 / AC-17 / AC-18 / ADR-0068）

职责：
  - holdout 注册由验证者 APP 执行（复用 ADR-0068 揭封 hash 校验机制）；
  - 机器校验 PR 引用的 holdout hash 与已注册记录一致（防 PR 声明跑 A 实际跑 B）；
  - 非验证者 APP 写入 holdout 内容被拒（覆盖跨仓场景，AC-18 403）；
  - 与 CI-Workflows 既有 pipeline/holdout-unseal/ 揭封 gate 接口兼容（sealed_sha256 公式同源）。

验证者 APP 时序闸（ADR-0080 / DECISION-01 / IFACE-01）：
  - expected-state.json 中 verifier_app.id=null 表示尚未创建；
  - 本模块以环境变量 / 配置开关控制验证者 APP 身份面：
      * HOLDOUT_REGISTRY_MODE = "verifier" | "dry-run"
      * "verifier" 模式：要求 verifier APP 令牌，执行真实注册；
      * "dry-run" 模式（默认，验证者 APP 未就绪）：本地校验哈希与一致性，
        写操作落盘到 registry 文件但不 push（占位，待验证者 APP 就绪后切换）。
  - 验证者 APP 就绪后由 scripts/register-verifier-app.sh 回填 id，本模块自动
    切换到 "verifier" 模式（检测非空 installation_id 即启用）。

用法:
  python3 pipeline/adversary/holdout_registry.py register \
      --card ISSUE-263 --entry HO-0001 --sha256 <hex> \
      --registry holdout/entries/registry.yaml
  python3 pipeline/adversary/holdout_registry.py check-pr \
      --card ISSUE-263 --declared-hash <hex> \
      --registry holdout/entries/registry.yaml
  python3 pipeline/adversary/holdout_registry.py verify-hash \
      --entry holdout/entries/HO-0001.json
  python3 pipeline/adversary/holdout_registry.py identity

退出码：0=通过 | 1=不一致/未找到 | 2=配置/环境/infra 错误 | 3=非授权身份写入被拒
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # noqa: BLE001
    print("FATAL: 需要 PyYAML（pip install pyyaml==6.0.3）", file=sys.stderr)
    sys.exit(2)

EXPECTED_STATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..",
    ".github", "governance", "expected-state.json")
DEFAULT_MODE = "dry-run"
VERIFIER_APP_ID = None  # 运行时从 expected-state.json 或环境变量解析


def err(msg: str) -> None:
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)


def die(code: int, msg: str) -> None:
    err(msg)
    sys.exit(code)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 验证者 APP 身份解析（IFACE-01 时序闸）
# ---------------------------------------------------------------------------

def resolve_verifier_id() -> int | None:
    """解析验证者 APP 的 installation id。

    优先级：环境变量 VERIFIER_INSTALLATION_ID > expected-state.json#verifier_app.id。
    返回 None 表示验证者 APP 尚未创建/安装（dry-run 模式）。
    """
    env_id = os.environ.get("VERIFIER_INSTALLATION_ID")
    if env_id and env_id.isdigit():
        return int(env_id)
    # 尝试读 expected-state.json（CI-Workflows 仓内无此文件时跳过）
    try:
        with open(EXPECTED_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        vid = (data.get("verifier_app") or {}).get("id")
        if vid and str(vid) != "null":
            return int(vid)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def current_mode() -> str:
    """返回当前注册模式：verifier（验证者 APP 已就绪）或 dry-run（占位）。"""
    if os.environ.get("HOLDOUT_REGISTRY_MODE") == "verifier":
        return "verifier"
    if os.environ.get("HOLDOUT_REGISTRY_MODE") == "dry-run":
        return "dry-run"
    # 自动判定
    return "verifier" if resolve_verifier_id() is not None else "dry-run"


def require_verifier_identity() -> int:
    """要求验证者 APP 身份；未就绪则 exit 3（非授权写入被拒，AC-18）。"""
    vid = resolve_verifier_id()
    if vid is None:
        err("holdout 注册需验证者 APP 身份（verifier_app.id 未就绪）——非验证者 APP 写入被拒（AC-18 403 等价，exit 3）")
        sys.exit(3)
    return vid


# ---------------------------------------------------------------------------
# registry 读写
# ---------------------------------------------------------------------------

def load_registry(path: str) -> dict:
    """加载 registry；文件不存在返回空结构（注册时可创建）。"""
    if not os.path.isfile(path):
        return {"cards": {}, "entries": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        die(2, f"registry 不可读 {path}: {e}")
    if not isinstance(data, dict):
        data = {}
    data.setdefault("cards", {})
    data.setdefault("entries", {})
    return data


def save_registry(path: str, data: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def sanitize_sha256(value: str) -> str:
    """校验并规范化 sha256 哈希（7-40 位十六进制）。"""
    if not isinstance(value, str):
        return ""
    v = value.strip().lower()
    if v.startswith("sha256:"):
        v = v[7:]
    if not (7 <= len(v) <= 64):
        return ""
    try:
        int(v, 16)
    except ValueError:
        return ""
    return v


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def cmd_register(args) -> int:
    sha = sanitize_sha256(args.sha256)
    if not sha:
        die(2, f"非法 sha256: {args.sha256}")

    mode = current_mode()
    # 真实注册必须由验证者 APP 执行（AC-18：非验证者 APP 写入被拒）
    if mode == "verifier":
        vid = require_verifier_identity()
        print(f"验证者 APP 身份确认（installation_id={vid}）— 执行真实注册")

    registry = load_registry(args.registry)

    # 幂等：同一 card+entry 已注册且 hash 一致 → no-op
    card_entries = registry["cards"].setdefault(args.card, {})
    existing = card_entries.get(args.entry)
    if isinstance(existing, dict) and sanitize_sha256(existing.get("hash", "")) == sha:
        print(f"注册未变：{args.card}/{args.entry} = sha256:{sha[:11]}")
        return 0

    record = {
        "hash": f"sha256:{sha}",
        "registered_at": now_iso(),
        "registered_by": f"verifier-app-{resolve_verifier_id()}" if mode == "verifier" else "dry-run-placeholder",
        "mode": mode,
    }
    card_entries[args.entry] = record
    registry["entries"][args.entry] = {
        "card": args.card,
        "hash": f"sha256:{sha}",
        "registered_at": record["registered_at"],
    }

    if mode == "dry-run":
        print(f"[dry-run] 注册落盘（验证者 APP 未就绪，不 push）：{args.card}/{args.entry} = sha256:{sha[:11]}")
    save_registry(args.registry, registry)
    print(f"注册完成：{args.card}/{args.entry} = sha256:{sha[:11]} (mode={mode})")
    return 0


# ---------------------------------------------------------------------------
# PR 引用一致性检查（AC-3 / AC-17）
# ---------------------------------------------------------------------------

def cmd_check_pr(args) -> int:
    """校验 PR 声明的 holdout hash 与 registry 已注册记录一致。"""
    declared = sanitize_sha256(args.declared_hash)
    if not declared:
        die(2, f"PR 声明的 holdout hash 非法: {args.declared_hash}")

    registry = load_registry(args.registry)
    card_entries = registry.get("cards", {}).get(args.card, {})

    if not card_entries:
        err(f"卡 {args.card} 无已注册 holdout 条目 — PR 引用无法核对")
        return 1

    mismatches = []
    for entry_id, rec in card_entries.items():
        registered = sanitize_sha256((rec.get("hash") or "") if isinstance(rec, dict) else "")
        if registered and registered != declared:
            mismatches.append((entry_id, registered))

    if mismatches:
        err(f"PR 声明 hash sha256:{declared[:11]} 与已注册记录不一致：")
        for eid, reg in mismatches:
            err(f"  {args.card}/{eid}: 已注册 sha256:{reg[:11]}")
        return 1

    print(f"PR 引用一致：{args.card} 全部已注册条目 hash = sha256:{declared[:11]}")
    return 0


# ---------------------------------------------------------------------------
# 条目 hash 校验（复用 ADR-0068 揭封 sealed_sha256 公式）
# ---------------------------------------------------------------------------

def canon(obj) -> str:
    """与 holdout/scripts/validate_entries.py 同式——封存哈希公式单一事实源。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha_hex(obj) -> str:
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def cmd_verify_hash(args) -> int:
    """校验 holdout 条目的 sealed_sha256 与 payload 一致（ADR-0068 揭封前置）。"""
    try:
        with open(args.entry, encoding="utf-8") as f:
            entry = json.load(f)
    except Exception as e:  # noqa: BLE001
        die(2, f"条目不可读 {args.entry}: {e}")

    sealed = entry.get("sealed_sha256", "")
    actual = sha_hex(entry.get("payload", {}))
    if sealed != actual:
        err(f"sealed_sha256 不符：条目声明 {sealed[:11] if sealed else '空'} vs 实际 {actual[:11]}")
        return 1
    print(f"sealed_sha256 一致：{os.path.basename(args.entry)} = {actual[:11]}")
    return 0


# ---------------------------------------------------------------------------
# 身份查询
# ---------------------------------------------------------------------------

def cmd_identity(args) -> int:
    mode = current_mode()
    vid = resolve_verifier_id()
    print(f"mode={mode}")
    print(f"verifier_app.id={vid if vid else '(未就绪，dry-run 占位)'}")
    print(f"HOLDOUT_REGISTRY_MODE={os.environ.get('HOLDOUT_REGISTRY_MODE', '(未设置，自动判定)')}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="holdout_registry.py", description="holdout 注册/哈希校验/PR 引用一致性")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="注册 holdout 条目（验证者 APP 身份）")
    p_reg.add_argument("--card", required=True, help="卡 ID（如 ISSUE-263）")
    p_reg.add_argument("--entry", required=True, help="条目 ID（如 HO-0001）")
    p_reg.add_argument("--sha256", required=True, help="holdout 内容的 sha256")
    p_reg.add_argument("--registry", required=True, help="registry.yaml 路径")

    p_cp = sub.add_parser("check-pr", help="校验 PR 引用 hash 与注册一致")
    p_cp.add_argument("--card", required=True)
    p_cp.add_argument("--declared-hash", required=True, help="PR 声明的 holdout hash")
    p_cp.add_argument("--registry", required=True)

    p_vh = sub.add_parser("verify-hash", help="校验条目 sealed_sha256（ADR-0068 公式）")
    p_vh.add_argument("--entry", required=True, help="holdout 条目 JSON 路径")

    sub.add_parser("identity", help="查询当前验证者 APP 身份与模式")

    args = ap.parse_args(argv)
    if args.cmd == "register":
        return cmd_register(args)
    if args.cmd == "check-pr":
        return cmd_check_pr(args)
    if args.cmd == "verify-hash":
        return cmd_verify_hash(args)
    return cmd_identity(args)


if __name__ == "__main__":
    sys.exit(main())
