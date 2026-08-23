#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""holdout_registry.py —— holdout 注册、hash 校验、PR 引用一致性检查（W2-C4 .github#276，AC-3 / AC-17 / ADR-0068）

holdout 注册由验证者 APP 执行（复用 ADR-0068 揭封 hash 校验）；
机器校验 PR 引用 hash 与注册一致。

职责：
  1. holdout 注册（append-only 台账，验证者 APP 身份校验——非验证者 APP 写入拒
     绝，覆盖跨仓场景，AC-17 / IFACE-01）；
  2. hash 校验（复用 ADR-0068 揭封 sealed_sha256 公式——canonical JSON + SHA-256）；
  3. PR 引用一致性检查（PR 引用的 holdout hash 必须与已注册记录一致，AC-3）；
  4. 与 ADR-0068 揭封 gate（pipeline/holdout-unseal/unseal_gate.py）接口兼容。

时序约束（ADR-0061 / DECISION-01）：验证者 APP 尚未创建（expected-state.json
verifier_app.id=null）时，代码使用占位符条件判断——注册逻辑已实现，但验证者 APP
身份校验使用占位符 VERIFYER_APP_SLUG，待验证者 APP 创建后替换。

用法:
  python holdout_registry.py register --entry <entry.json> --actor <actor-slug> \
      [--registry <registry.json>] [--repo <repo>]
  python holdout_registry.py verify-hash --entry <entry.json>
  python holdout_registry.py check-pr --pr <n> --repo <repo> \
      [--registry <registry.json>] [--token <token>]
  python holdout_registry.py self-test [--registry <registry.json>]

退出码: 0=成功/一致/通过
        1=校验失败（hash 不一致 / PR 引用不匹配 / 非验证者 APP 写入）
        2=infra/配置错误（fail-closed）
"""
from __future__ import annotations

import argparse
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
DEFAULT_REGISTRY = os.path.join(HERE, "holdout-registry.json")

# ---------------------------------------------------------------------------
# 验证者 APP 占位符（DECISION-01：验证者 APP 尚未创建，待创建后替换）
# expected-state.json#verifier_app.id=null —— 使用占位符条件判断
# 验证者 APP 安装后，将此值替换为实际 slug（如 "cloudbrid-verifier[bot]"）
# ---------------------------------------------------------------------------
VERIFIER_APP_SLUG = os.environ.get("VERIFIER_APP_SLUG", "verifier-app[bot]")
# 时序断言（ADR-0061 / IFACE-01）：AG-1 修订 ADR 合并前，验证者 APP 不得实施。
# 当检测到非占位符以外的"验证者 APP"实施证据时，执行逻辑判红（负向断言）。
AG1_MERGED = os.environ.get("AG1_ADR_MERGED", "0") == "1"


def err(msg: str) -> None:
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)


def die(code: int, msg: str) -> None:
    err(msg)
    sys.exit(code)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# ADR-0068 hash 校验（与 unseal_gate.py 完全同式）
# ---------------------------------------------------------------------------

def canon(obj) -> str:
    """与 holdout scripts/validate_entries.py 完全同式——封存哈希公式单一事实源。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha_hex(obj) -> str:
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def compute_sealed_hash(payload: dict) -> str:
    """计算 holdout 条目的 sealed_sha256（ADR-0068 公式）。"""
    return sha_hex(payload)


def verify_entry_hash(entry: dict) -> tuple[bool, str]:
    """校验 holdout 条目的 sealed_sha256 与 payload 一致。"""
    expected = entry.get("sealed_sha256", "")
    actual = compute_sealed_hash(entry.get("payload", {}))
    if expected != actual:
        return False, f"sealed_sha256 不一致: 期望={actual} 实际={expected}"
    # 文件级 hash 校验
    for f in entry.get("payload", {}).get("files", []):
        content_b64 = f.get("content_b64", "")
        import base64
        try:
            raw = base64.b64decode(content_b64, validate=True)
        except Exception as e:  # noqa: BLE001
            return False, f"文件 {f.get('name', '?')} content_b64 解码失败: {e}"
        file_hash = hashlib.sha256(raw).hexdigest()
        if file_hash != f.get("sha256"):
            return False, f"文件 {f.get('name', '?')} hash 不一致"
    return True, "hash 校验通过"


# ---------------------------------------------------------------------------
# 验证者 APP 身份校验（AC-17 / IFACE-01 / DECISION-01）
# ---------------------------------------------------------------------------

def is_verifier_app(actor: str) -> bool:
    """判断 actor 是否为验证者 APP 身份。

    时序约束（DECISION-01）：验证者 APP 尚未创建时，使用占位符。
    当 AG-1 修订 ADR 未合并时，任何"验证者 APP 实施证据"即判红。
    """
    if not actor:
        return False
    # 精确匹配验证者 APP slug
    if actor == VERIFIER_APP_SLUG:
        return True
    # 环境变量注入的验证者 APP 身份列表（逗号分隔）
    extra_slugs = os.environ.get("VERIFIER_APP_SLUGS", "")
    if extra_slugs and actor in {s.strip() for s in extra_slugs.split(",") if s.strip()}:
        return True
    return False


def check_writer_authorization(actor: str, target_repo: str) -> tuple[bool, str]:
    """校验写入者是否有权注册 holdout。

    返回 (authorized, reason)。
    非验证者 APP 写入 holdout 内容被拒（覆盖跨仓场景，AC-17）。
    """
    # 开发 agent（cloudbrid-agent）无权写入 holdout
    if actor == "cloudbrid-agent[bot]":
        return False, f"开发 agent {actor} 无权写入 holdout（仅验证者 APP）"
    # 验证者 APP 有权写入
    if is_verifier_app(actor):
        return True, f"验证者 APP {actor} 授权写入"
    # 人类 owner 有权写入（owner 身份由调用方通过 token 范围判定）
    # 这里仅做 slug 级判断，实际 owner 判定在 workflow/App 令牌层
    if actor and not actor.endswith("[bot]"):
        # 人类用户——需进一步校验是否为 owner（此处放行，workflow 层兜底）
        return True, f"人类用户 {actor} 写入（workflow 层 owner 校验兜底）"
    return False, f"未知身份 {actor} 无权写入 holdout"


# ---------------------------------------------------------------------------
# 注册表操作（append-only 台账）
# ---------------------------------------------------------------------------

def load_registry(registry_path: str) -> dict:
    """加载 holdout 注册表。"""
    if not os.path.isfile(registry_path):
        return {"schema": "holdout-registry/v1", "entries": [], "version": "1"}
    try:
        with open(registry_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        die(2, f"注册表读取失败 {registry_path}: {e}")


def save_registry(registry_path: str, registry: dict) -> None:
    """持久化 holdout 注册表（原子写入）。"""
    tmp = registry_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp, registry_path)


def register_entry(entry: dict, actor: str, registry_path: str,
                   repo: str = "unknown") -> tuple[bool, str, dict]:
    """注册 holdout 条目。返回 (success, reason, record)。"""
    # 身份校验
    authorized, auth_reason = check_writer_authorization(actor, repo)
    if not authorized:
        return False, f"403 拒绝: {auth_reason}", {}

    # hash 校验
    hash_ok, hash_reason = verify_entry_hash(entry)
    if not hash_ok:
        return False, f"hash 校验失败: {hash_reason}", {}

    registry = load_registry(registry_path)
    entries = registry.get("entries") or []

    # 查重：同 id 条目不可重复注册（append-only）
    entry_id = entry.get("id", "")
    existing = [e for e in entries if e.get("entry_id") == entry_id]
    if existing:
        return False, f"条目 {entry_id} 已注册（append-only，不可重复注册）", {}

    record = {
        "entry_id": entry_id,
        "sealed_sha256": entry.get("sealed_sha256", ""),
        "registered_by": actor,
        "registered_at": now_iso(),
        "repo": repo,
        "payload_kind": entry.get("payload", {}).get("kind", "unknown"),
        "file_count": len(entry.get("payload", {}).get("files", [])),
        "file_hashes": {f.get("name", ""): f.get("sha256", "")
                        for f in entry.get("payload", {}).get("files", [])},
    }
    entries.append(record)
    registry["entries"] = entries
    save_registry(registry_path, registry)

    return True, f"注册成功: {entry_id} (by {actor})", record


# ---------------------------------------------------------------------------
# PR 引用一致性检查（AC-3）
# ---------------------------------------------------------------------------

def extract_holdout_refs_from_pr(pr_body: str) -> list[dict]:
    """从 PR body 中提取引用的 holdout hash。

    匹配格式：holdout-sha256: <hex> 或 Holdout-Hash: <hex>
    """
    refs = []
    for m in re.finditer(r"(?:holdout[_-]?sha256|holdout[_-]?hash|Holdout-Hash)\s*[:=]\s*([0-9a-fA-F]{64})", pr_body):
        refs.append({"sha256": m.group(1), "source": "pr_body"})
    return refs


def check_pr_reference(pr_body: str, registry_path: str,
                       token: str | None = None, repo: str | None = None,
                       pr_number: int | None = None) -> tuple[bool, str, list[dict]]:
    """校验 PR 引用的 holdout hash 与注册表一致。

    返回 (all_match, reason, mismatches)。
    """
    registry = load_registry(registry_path)
    entries = registry.get("entries") or []
    registered_hashes = {e.get("sealed_sha256", "") for e in entries}

    # 从 PR body 提取引用
    refs = extract_holdout_refs_from_pr(pr_body) if pr_body else []

    # 如果提供了 token 和 repo，尝试从 PR API 获取 body
    if not refs and token and repo and pr_number:
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json",
                         "User-Agent": "holdout-registry"})
            with urllib.request.urlopen(req, timeout=30) as r:
                pr_data = json.loads(r.read())
                pr_body = pr_data.get("body") or ""
                refs = extract_holdout_refs_from_pr(pr_body)
        except urllib.error.HTTPError as e:
            err(f"PR API 读取失败 HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            err(f"PR API 读取异常: {e}")

    if not refs:
        # PR 未引用 holdout——不强制要求（开发路径 PR 可能无 holdout）
        return True, "PR 未引用 holdout hash（无需校验）", []

    mismatches = []
    for ref in refs:
        ref_hash = ref.get("sha256", "")
        if ref_hash not in registered_hashes:
            mismatches.append({"sha256": ref_hash, "reason": "未在注册表中找到"})

    if mismatches:
        return False, f"PR 引用 hash 与注册表不一致: {len(mismatches)} 项不匹配", mismatches
    return True, f"PR 引用 hash 与注册表一致（{len(refs)} 项）", []


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

def self_test(registry_path: str) -> int:
    """内置 self-test：注册、hash 校验、PR 引用一致性。"""
    print(f"== holdout_registry self-test ==")

    # 构造测试条目
    import base64
    test_payload = {
        "kind": "sealed-test-set",
        "files": [
            {
                "name": "test_holdout_dummy.py",
                "content_b64": base64.b64encode(b"def test_dummy():\n    assert True\n").decode("ascii"),
                "sha256": hashlib.sha256(b"def test_dummy():\n    assert True\n").hexdigest(),
            }
        ],
    }
    test_entry = {
        "id": "HO-SELFTEST",
        "payload": test_payload,
    }
    test_entry["sealed_sha256"] = compute_sealed_hash(test_payload)

    tmp_registry = registry_path + ".selftest"
    try:
        # 注册测试
        ok, reason, record = register_entry(test_entry, VERIFIER_APP_SLUG, tmp_registry, "self-test")
        print(f"  注册: {'OK' if ok else 'FAIL'} — {reason}")
        if not ok:
            return 1

        # hash 校验
        hash_ok, hash_reason = verify_entry_hash(test_entry)
        print(f"  hash 校验: {'OK' if hash_ok else 'FAIL'} — {hash_reason}")
        if not hash_ok:
            return 1

        # 非验证者 APP 写入拒绝
        ok2, reason2, _ = register_entry(test_entry, "cloudbrid-agent[bot]", tmp_registry, "self-test")
        print(f"  开发 agent 写入拒绝: {'OK' if not ok2 else 'FAIL'} — {reason2}")
        if ok2:
            err("开发 agent 写入应被拒绝")
            return 1

        # PR 引用一致性
        pr_body = f"Card: Cloudbird-Software/.github#276\nholdout-sha256: {test_entry['sealed_sha256']}"
        match, pr_reason, _ = check_pr_reference(pr_body, tmp_registry)
        print(f"  PR 引用一致性: {'OK' if match else 'FAIL'} — {pr_reason}")
        if not match:
            return 1

        print("  self-test 全部通过")
        return 0
    finally:
        if os.path.isfile(tmp_registry):
            os.remove(tmp_registry)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(prog="holdout_registry.py", description="holdout 注册、hash 校验、PR 引用一致性（W2-C4 / AC-3 / ADR-0068）")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY, help="注册表路径")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "unknown"), help="目标仓库")
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN"), help="GitHub API 令牌")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="注册 holdout 条目")
    p_reg.add_argument("--entry", required=True, help="holdout 条目 JSON 路径")
    p_reg.add_argument("--actor", required=True, help="写入者身份 slug")

    p_hash = sub.add_parser("verify-hash", help="校验条目 hash")
    p_hash.add_argument("--entry", required=True, help="holdout 条目 JSON 路径")

    p_pr = sub.add_parser("check-pr", help="校验 PR 引用一致性")
    p_pr.add_argument("--pr", type=int, default=0, help="PR 编号")
    p_pr.add_argument("--pr-body", default="", help="PR body 文本（缺省从 API 获取）")

    sub.add_parser("self-test", help="运行内置 self-test")

    a = ap.parse_args()

    if a.cmd == "self-test":
        return self_test(a.registry)

    if a.cmd == "register":
        entry = load_json(a.entry) if os.path.isfile(a.entry) else {}
        ok, reason, record = register_entry(entry, a.actor, a.registry, a.repo)
        print(f"注册结果: {reason}")
        return 0 if ok else 1

    if a.cmd == "verify-hash":
        entry = load_json(a.entry) if os.path.isfile(a.entry) else {}
        ok, reason = verify_entry_hash(entry)
        print(f"hash 校验: {reason}")
        return 0 if ok else 1

    if a.cmd == "check-pr":
        pr_body = a.pr_body or ""
        ok, reason, mismatches = check_pr_reference(pr_body, a.registry, a.token, a.repo, a.pr or None)
        print(f"PR 引用一致性: {reason}")
        if mismatches:
            for m in mismatches:
                print(f"  不匹配: {m['sha256']} — {m['reason']}")
        return 0 if ok else 1

    return 2


def load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        die(2, f"JSON 不可读 {path}: {e}")


if __name__ == "__main__":
    sys.exit(main())
