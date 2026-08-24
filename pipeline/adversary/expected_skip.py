#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""expected_skip.py —— 开发路径豁免谓词（W4-C3 .github#284，AC-14 / DECISION-02）。

判定一 PR 是否可豁免红队审计（EXPECTED_SKIP）：
  - 豁免谓词由 diff 路径集确定性派生——禁人工打标（无 label / 评论 / 人工标注介入）；
  - 含 specs/** 实质内容变更却试图走豁免的 PR 必须红（反向断言，fail-closed）；
  - 与 ADR-0032 登记豁免制一致：specs/** 为唯一受保护路径集合，其余视为开发实现路径。

两种调用面：
  1) judge 子命令（W4-C2 #283 落地后的统一判定面——adversary-gate 与测试共用）：
       python3 expected_skip.py judge --paths '<JSON数组>' [--exempt-list 清单.json]
     输出 JSON：expected_skip / specs_paths / exempted_paths / remaining_specs /
     exemption_sha；退出码 0=可豁免 / 1=须跑 adversary / 2=输入错误。
     exemption_sha = 豁免清单文件内容的 sha256（无清单时为哨兵值）——判定留痕
     可追溯到具体清单版本（E6：有清单≠无清单）。
  2) 兼容顶层接口（fetch PR 文件列表后判定，供 workflow/人工诊断用）：
       --repo/--pr-number | --paths-from-stdin | --paths a b c

用途：adversary-gate（.github 仓）与 run.py 在 gate 步调用——specs/** 变更须跑
adversary，纯开发路径 PR 直接放行（EXPECTED_SKIP）。

退出码（两种模式一致）：
  0 = EXPECTED_SKIP（纯开发路径或全部命中 owner 登记豁免，放行）
  1 = 须跑 adversary（含 specs/** 实质变更，不豁免）
  2 = 配置/输入错误
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Iterable, List, Optional

# 默认受保护路径模式（AC-14 / DECISION-02）：specs/** 为 spec/测试设计路径，
# 必须经红队审计；其余为开发实现路径，可按 EXPECTED_SKIP 豁免。
DEFAULT_PROTECTED = ["specs/**"]

# 无豁免清单时的哨兵（sha256("no-exempt-list")）——保证 exemption_sha 恒有值
# 且与任何真实清单内容不同（E6 断言依据）。
NO_EXEMPT_SHA = hashlib.sha256(b"no-exempt-list").hexdigest()


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


# ---------------- judge 模式（W4-C2 #283 统一判定面） ----------------

def load_exempt_patterns(path: Optional[str]) -> tuple:
    """加载 owner 登记豁免清单（ADR-0032）：返回 (patterns, sha256)。"""
    if not path:
        return [], NO_EXEMPT_SHA
    try:
        raw = open(path, "rb").read()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        die(2, f"豁免清单读取/解析失败 {path}: {e}")
    # 兼容两种 schema：{"exemptions": [...]} 或裸数组
    patterns = data.get("exemptions", []) if isinstance(data, dict) else data
    if not isinstance(patterns, list) or not all(isinstance(x, str) for x in patterns):
        die(2, f"豁免清单 {path} 格式错误：exemptions 须为字符串数组")
    return patterns, hashlib.sha256(raw).hexdigest()


def judge(paths: List[str], protected_patterns: List[str],
          exempt_patterns: List[str], exemption_sha: str) -> dict:
    """纯函数判定（judge 子命令与复用方共用）。

    语义：
      specs_paths     = 命中受保护模式的路径（豁免前全集）
      exempted_paths  = specs_paths 中命中豁免清单的子集（owner 登记的引用级修改）
      remaining_specs = specs_paths - exempted_paths（须跑 adversary 的实质变更）
      expected_skip   = remaining_specs 为空（fail-closed：部分豁免不豁免整体）
    """
    specs_paths: List[str] = []
    dev_paths: List[str] = []
    for p in paths:
        if is_protected(p, protected_patterns):
            specs_paths.append(p)
        else:
            dev_paths.append(p)
    exempted = [p for p in specs_paths if is_protected(p, exempt_patterns)]
    remaining = [p for p in specs_paths if p not in exempted]
    return {
        "expected_skip": len(remaining) == 0,
        "specs_paths": specs_paths,
        "exempted_paths": exempted,
        "remaining_specs": remaining,
        "dev_paths": dev_paths,
        "exemption_sha": exemption_sha,
        "exempt_pattern_count": len(exempt_patterns),
        "protected_patterns": list(protected_patterns),
        "total": len(paths),
        "verdict": "EXPECTED_SKIP" if not remaining else "must-run-adversary",
    }


def parse_json_paths(raw: str) -> List[str]:
    """解析 judge --paths 的 JSON 数组字符串（E7：非法输入 → exit 2）。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        die(2, f"--paths 不是合法 JSON 数组：{e}")
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        die(2, "--paths 须为字符串 JSON 数组，例：'[\"src/a.py\", \"specs/x/spec.md\"]'")
    return data


def run_judge(argv: List[str]) -> int:
    p = argparse.ArgumentParser(
        prog="expected_skip.py judge",
        description="纯函数判定：路径集 + 豁免清单 → EXPECTED_SKIP 决策（W4-C2/W4-C3）",
    )
    p.add_argument("--paths", required=True,
                   help="路径 JSON 数组字符串（例：'[\"specs/x/spec.md\"]'）")
    p.add_argument("--exempt-list", default=None,
                   help="owner 登记豁免清单 JSON（ADR-0032；缺省=无豁免）")
    p.add_argument("--protected", action="append", default=[],
                   help="受保护 glob（可多次；默认 specs/**）")
    args = p.parse_args(argv)

    paths = parse_json_paths(args.paths)
    protected = args.protected or DEFAULT_PROTECTED
    exempt_patterns, exemption_sha = load_exempt_patterns(args.exempt_list)
    result = judge(paths, protected, exempt_patterns, exemption_sha)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["expected_skip"]:
        print(f"EXPECTED_SKIP：remaining_specs 为空（豁免 {len(result['exempted_paths'])} 条）",
              file=sys.stderr)
        return 0
    err(f"specs/** 实质变更 {len(result['remaining_specs'])} 条未豁免"
        f"（{', '.join(result['remaining_specs'][:10])}）——须跑 adversary（AC-14 反向断言）")
    return 1


def main(argv=None) -> int:
    # judge 子命令分发（保持顶层兼容接口不变）
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "judge":
        return run_judge(argv[1:])
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
