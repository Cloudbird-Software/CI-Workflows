#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""expected_skip.py —— 开发路径豁免谓词（W4-C3 .github#284，AC-14 / DECISION-02）。

判定一 PR 是否可豁免红队审计（EXPECTED_SKIP）：
  - 豁免谓词由 diff 路径集确定性派生——禁人工打标（无 label / 评论 / 人工标注介入）；
  - 含 specs/** 实质内容变更却试图走豁免的 PR 必须红（反向断言，fail-closed）；
  - 与 ADR-0032 登记豁免制一致：specs/** 为唯一受保护路径集合，其余视为开发实现路径。

用途：被 adversary-gate.yml（workflow 层，当前 App 无 workflows 权限，本地保留）调用，
在 gate 步判定——specs/** 变更须跑 adversary，纯开发路径 PR 直接放行（EXPECTED_SKIP）。

用法（PR 文件列表由 GitHub API 取，仓内直接判定）：
  python3 pipeline/adversary/expected_skip.py \
      --repo Cloudbird-Software/.github --pr-number 284 \
      --protected 'specs/**' \
      [--gh-token $APP_TOKEN]

用法（本地路径列表，便于离线/CI 复用）：
  python3 pipeline/adversary/expected_skip.py \
      --paths-from-stdin --protected 'specs/**' \
      < changed_files.txt

退出码：
  0 = EXPECTED_SKIP（纯开发路径，无 specs/** 变更，放行）
  1 = 须跑 adversary（含 specs/** 变更，不豁免）
  2 = 配置/输入错误
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Iterable, List

# 默认受保护路径模式（AC-14 / DECISION-02）：specs/** 为 spec/测试设计路径，
# 必须经红队审计；其余为开发实现路径，可按 EXPECTED_SKIP 豁免。
DEFAULT_PROTECTED = ["specs/**"]


def err(msg: str) -> None:
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)


def die(code: int, msg: str) -> None:
    err(msg)
    sys.exit(code)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="开发路径豁免谓词：specs/** 变更须 adversary，纯开发路径 EXPECTED_SKIP（W4-C3 AC-14）"
    )
    p.add_argument("--repo", help="仓库 owner/name（与 --pr-number 配合，经 API 取文件列表）")
    p.add_argument("--pr-number", type=int, help="PR 编号（经 GitHub API 取 diff 路径集）")
    p.add_argument("--gh-token", default=os.environ.get("GH_TOKEN"), help="GitHub token（读 PR 文件列表）")
    p.add_argument("--protected", action="append", default=[],
                   help="受保护路径 glob 模式（可多次；默认 specs/**）")
    p.add_argument("--paths-from-stdin", action="store_true",
                   help="从 stdin 读路径列表（一行一路径），不经 API")
    p.add_argument("--paths", nargs="*", default=[], help="直接传路径列表（命令行）")
    p.add_argument("--decision-as-adversary", action="store_true",
                   help="存在受保护路径变更时，将决策输出为 adversary verdict 而非仅退出码")
    return p.parse_args(argv)


def fetch_pr_files(repo: str, pr_number: int, token: str) -> List[str]:
    """经 GitHub API 取 PR 变更文件路径列表（Pulls API /files，含重命名/删除）。"""
    if not token:
        die(2, "GH_TOKEN 未设置（需要读 PR 文件列表的令牌）")
    files: List[str] = []
    page = 1
    while True:
        path = f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}"
        req = urllib.request.Request(
            f"https://api.github.com{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "cloudbrid-agent",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            die(2, f"读取 PR #{pr_number} 文件列表失败 HTTP {e.code}: {body[:400]}")
        if not data:
            break
        for f in data:
            # filename 为变更后路径（新增/修改/重命名），previous_filename 为重命名前
            name = f.get("filename")
            if name:
                files.append(name)
            prev = f.get("previous_filename")
            if prev and prev != name:
                files.append(prev)
        if len(data) < 100:
            break
        page += 1
    return files


def load_paths_from_stdin() -> List[str]:
    paths: List[str] = []
    for line in sys.stdin:
        line = line.strip()
        if line:
            paths.append(line)
    return paths


def is_protected(path: str, protected_patterns: Iterable[str]) -> bool:
    """路径是否匹配任一受保护 glob 模式。"""
    for pat in protected_patterns:
        if fnmatch.fnmatch(path, pat):
            return True
    return False


def classify(paths: List[str], protected_patterns: Iterable[str]) -> dict:
    """将路径集分类为受保护变更与开发路径变更。"""
    protected_hits: List[str] = []
    dev_paths: List[str] = []
    for p in paths:
        if is_protected(p, protected_patterns):
            protected_hits.append(p)
        else:
            dev_paths.append(p)
    skip = len(protected_hits) == 0
    return {
        "skip": skip,
        "protected_hits": protected_hits,
        "dev_paths": dev_paths,
        "total": len(paths),
        "verdict": "EXPECTED_SKIP" if skip else "must-run-adversary",
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    protected = args.protected or DEFAULT_PROTECTED

    # 取路径集
    if args.paths_from_stdin:
        paths = load_paths_from_stdin()
    elif args.paths:
        paths = list(args.paths)
    elif args.repo and args.pr_number:
        paths = fetch_pr_files(args.repo, args.pr_number, args.gh_token)
    else:
        die(2, "须指定 --paths-from-stdin / --paths / (--repo + --pr-number)")

    if not paths:
        # 空 diff（无文件变更）→ 无受保护路径命中，放行
        result = {"skip": True, "protected_hits": [], "dev_paths": [], "total": 0,
                  "verdict": "EXPECTED_SKIP", "note": "空 diff（无文件变更）"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("EXPECTED_SKIP：空 diff", file=sys.stderr)
        return 0

    result = classify(paths, protected)
    result["protected_patterns"] = list(protected)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result["skip"]:
        print(f"EXPECTED_SKIP：{result['total']} 个路径均为开发路径，无 {protected} 变更",
              file=sys.stderr)
        return 0

    # 含 specs/** 变更却走豁免 → 必须红（反向断言）
    hits = ", ".join(result["protected_hits"][:10])
    err(f"受保护路径变更命中 {len(result['protected_hits'])} 个（{hits}）"
        + (" ..." if len(result["protected_hits"]) > 10 else "")
        + f"—— 须跑 adversary，禁止豁免（AC-14 反向断言）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
