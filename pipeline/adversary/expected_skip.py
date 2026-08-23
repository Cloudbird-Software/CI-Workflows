#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""expected_skip.py —— 开发路径豁免谓词由 diff 路径集确定性派生（W4-C3 .github#284，AC-14/AC-19）

specs/** 路径 PR 必须含 adversary check；开发实现路径 PR 按 EXPECTED_SKIP 模式
条件化豁免。豁免谓词由 diff 路径集确定性派生（禁止人工打标），与 ADR-0032
登记豁免制一致（豁免须登记、其余非 success 一律红）。

判定逻辑（纯函数，零副作用，便于单测）：
  changed_paths = PR 的 diff 路径集（list[str]）
  1. specs_paths = 命中 specs/** 的路径（前缀 specs/ 且为实质内容文件）
  2. 若 specs_paths 为空 → dev 路径，EXPECTED_SKIP=True（豁免 adversary check）
  3. 若 specs_paths 非空：
     a. 先过 owner 登记豁免清单（reference-level tweaks，每路径 glob/精确匹配）
        → 命中即 EXPECTED_SKIP=True（留痕：exempted_paths + exemption_sha）
     b. 未命中豁免清单的 specs/** 实质变更 → EXPECTED_SKIP=False
        （必须含 adversary check，否则 red，AC-4 负向断言）

  禁人工打标（fail-closed 铁律）：本脚本只读 diff 路径集 + 登记豁免清单，
  不读 PR 标签 / issue 评论 / 人工标记文件 / PR 主体声明。任何试图经标签或
  声明绕过 specs/**  adversary check 的行为一律无效——本脚本输出是唯一真源。
  豁免清单仓内治理（C1 路径，ADR-0032 登记制），版本化、机器可审计。

用法：
  # 纯函数模式（由 adversary-gate.yml 经 API 取 PR files 后传入路径 JSON）：
  python3 pipeline/adversary/expected_skip.py judge \
      --paths '["src/foo.py", "specs/ISSUE-263/spec.md"]' \
      [--exempt-list pipeline/adversary/fixtures/expected_skip/exemptions.json]

  # PR 模式（脚本自行取 diff，需 GH_TOKEN with pull-requests:read）：
  python3 pipeline/adversary/expected_skip.py judge-pr \
      --pr <number> --repo Cloudbird-Software/.github \
      [--exempt-list ...]

退出码：0=EXPECTED_SKIP=True（dev 路径豁免）| 1=EXPECTED_SKIP=False（spec 须 check）
        2=参数/环境错 | 3=API 失败（judge-pr 模式）
stdout：JSON {expected_skip, reason, specs_paths, exempted_paths, exemption_sha, mode}
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

GITHUB_API = os.environ.get("CB_GITHUB_API", "https://api.github.com")
# specs/** 中"实质内容"文件 glob；命中才算 spec 变更（目录占位/README 等可在此精化）
SPECS_GLOBS = ["specs/**/*.md", "specs/**/*.yaml", "specs/**/*.yml", "specs/**/*.json",
               "specs/**/*.py", "specs/**/*.sh", "specs/**/*.txt"]
# 命中 specs/ 前缀但不在实质内容 glob 内的路径（如 specs/<card>/suite/ 由 g060 锁定、
# 或由 card_bound_test 覆盖）——这里视为"spec 相关"但单独归类，仍须 check
SPECS_PREFIX = "specs/"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# 纯函数核心（零网络、零副作用，单测直接调用）
# ----------------------------------------------------------------------------
def is_spec_path(path: str) -> bool:
    """路径是否属于 specs/** 实质内容变更。"""
    norm = path.replace("\\", "/").lstrip("./")
    if not norm.startswith(SPECS_PREFIX):
        return False
    return any(fnmatch.fnmatch(norm, g) for g in SPECS_GLOBS)


def load_exempt_list(exempt_file: str | None) -> tuple[list[str], str]:
    """读 owner 登记豁免清单（glob 列表）。返回 (patterns, sha)。"""
    if not exempt_file or not os.path.isfile(exempt_file):
        return [], sha256_text("")
    with open(exempt_file, encoding="utf-8") as f:
        content = f.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # 纯文本：每行一个 glob/路径（# 注释、空行忽略）
        patterns = [ln.strip() for ln in content.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
        return patterns, sha256_text(content)
    patterns = data.get("exemptions", []) if isinstance(data, dict) else list(data)
    return [str(p) for p in patterns], sha256_text(content)


def is_exempt(path: str, patterns: list[str]) -> bool:
    """路径是否命中 owner 登记豁免清单（glob 或精确匹配）。"""
    norm = path.replace("\\", "/").lstrip("./")
    for pat in patterns:
        if norm == pat or fnmatch.fnmatch(norm, pat):
            return True
    return False


def judge(changed_paths: list[str], exempt_file: str | None = None) -> dict:
    """纯函数：由 diff 路径集确定性派生 EXPECTED_SKIP。"""
    patterns, exemption_sha = load_exempt_list(exempt_file)
    specs_paths = [p for p in changed_paths if is_spec_path(p)]
    dev_paths = [p for p in changed_paths if not is_spec_path(p)]

    if not specs_paths:
        return {
            "expected_skip": True,
            "reason": "dev 路径 PR：diff 无 specs/** 实质内容变更，豁免 adversary check（AC-14 开发路径豁免）",
            "specs_paths": [], "dev_paths": dev_paths,
            "exempted_paths": [], "exemption_sha": exemption_sha,
            "mode": "judge", "ts": now_iso(),
        }

    # specs/** 有实质变更 → 过 owner 登记豁免清单
    exempted = [p for p in specs_paths if is_exempt(p, patterns)]
    remaining = [p for p in specs_paths if p not in exempted]

    if not remaining:
        return {
            "expected_skip": True,
            "reason": f"specs/** 变更全部命中 owner 登记豁免清单（{len(exempted)} 条，reference-level tweaks）",
            "specs_paths": specs_paths, "dev_paths": dev_paths,
            "exempted_paths": exempted, "exemption_sha": exemption_sha,
            "mode": "judge", "ts": now_iso(),
        }

    # 含 specs/** 实质变更却未全命中豁免 → 必须含 adversary check，否则 red
    return {
        "expected_skip": False,
        "reason": f"specs/** 含未豁免实质变更（{len(remaining)} 条）：必须含 adversary check，否则 red（AC-4 负向断言）",
        "specs_paths": specs_paths, "dev_paths": dev_paths,
        "exempted_paths": exempted, "remaining_specs": remaining,
        "exemption_sha": exemption_sha,
        "mode": "judge", "ts": now_iso(),
    }


# ----------------------------------------------------------------------------
# judge-pr 模式：经 GitHub API 取 PR diff 路径集
# ----------------------------------------------------------------------------
class Gh:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo

    def call(self, path: str, method: str = "GET", expected: tuple[int, ...] = (200,)) -> Any:
        url = GITHUB_API + path
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                   "User-Agent": "expected_skip"}
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"API {method} {path} HTTP {e.code}: {err[:500]}") from e

    def pr_changed_files(self, pr_number: int) -> list[str]:
        out: list[str] = []
        for page in range(1, 10):
            chunk = self.call(f"/repos/{self.repo}/pulls/{pr_number}/files?per_page=100&page={page}")
            if not isinstance(chunk, list) or not chunk:
                break
            out.extend(f.get("filename", "") for f in chunk)
            if len(chunk) < 100:
                break
        return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="expected_skip.py",
                                 description="开发路径豁免谓词由 diff 路径集确定性派生（W4-C3 .github#284，AC-14）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pj = sub.add_parser("judge", help="纯函数模式：传 --paths JSON 数组")
    pj.add_argument("--paths", required=True, help="diff 路径 JSON 数组（如 '[\"src/foo.py\"]'）")
    pj.add_argument("--exempt-list", default=None, help="owner 登记豁免清单 JSON/glob 文件")
    jp = sub.add_parser("judge-pr", help="PR 模式：脚本取 diff（需 GH_TOKEN）")
    jp.add_argument("--pr", type=int, required=True)
    jp.add_argument("--repo", default="Cloudbird-Software/.github")
    jp.add_argument("--exempt-list", default=None)
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN", os.environ.get("APP_TOKEN", ""))

    if args.cmd == "judge":
        try:
            paths = json.loads(args.paths)
            if not isinstance(paths, list):
                raise ValueError("非数组")
        except (json.JSONDecodeError, ValueError) as e:
            print(f"::error::expected_skip: --paths 解析失败：{e}", file=sys.stderr)
            sys.exit(2)
        result = judge(paths, args.exempt_list)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["expected_skip"] else 1)

    # judge-pr
    if not token:
        print("::error::expected_skip: judge-pr 模式需 GH_TOKEN/APP_TOKEN", file=sys.stderr)
        sys.exit(2)
    try:
        gh = Gh(token, args.repo)
        paths = gh.pr_changed_files(args.pr)
    except RuntimeError as e:
        print(f"::error::expected_skip: 取 PR diff 失败：{e}", file=sys.stderr)
        sys.exit(3)
    result = judge(paths, args.exempt_list)
    result["pr"] = args.pr
    result["repo"] = args.repo
    result["mode"] = "judge-pr"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["expected_skip"] else 1)


if __name__ == "__main__":
    main()
