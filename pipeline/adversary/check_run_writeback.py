#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_run_writeback.py —— 红队 verdict 以 check run 写回 spec PR（W4-C2 .github#283，INV-02）。

adversary 产出判定报告（adversary-report/v1）后，本模块以 cloudbrid-agent App 令牌
（checks:write 权限，INV-02：GITHUB_TOKEN 不持有状态写权）在 spec PR 的 head commit
上创建 check run，使红队 verdict 成为合并阻断项（BEH-01）。

verdict → check run 结论映射（fail-closed）：
  survived     → success        （套件通过考验，放行合并）
  insufficient → failure        （套件不充分，blocking——spec PR 须先补强套件）
  no-attempts  → neutral        （白卷/infra，AC-15：不进入失败/锁卡分支，留痕+报人）
  报告缺失/不合 schema → failure（fail-closed：无 verdict 不放行）

用法：
  python3 pipeline/adversary/check_run_writeback.py \
      --report <adversary-report.json> \
      --repo Cloudbird-Software/.github \
      --pr-number 284 \
      --head-sha <spec-pr-head-sha> \
      [--gh-token $APP_TOKEN] \
      [--name adversary]

  python3 pipeline/adversary/check_run_writeback.py \
      --report - --repo Cloudbird-Software/.github --pr-number 284 --head-sha $SHA \
      --gh-token $APP_TOKEN < report.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# adversary-report/v1 必填字段（与 w3-c1/pipeline/adversary/adversary.py 产出一致）
REPORT_REQUIRED = ["schema", "ts", "target", "verdict", "blocking"]
VERDICT_CONCLUSION = {
    "survived": "success",
    "insufficient": "failure",
    "no-attempts": "neutral",
    # W4-C3 EXPECTED_SKIP：开发路径豁免（diff 路径集无 specs/**），确定性派生，放行
    "skip": "success",
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def err(msg: str) -> None:
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)


def die(code: int, msg: str) -> None:
    err(msg)
    sys.exit(code)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="红队 verdict 以 check run 写回 spec PR（W4-C2 .github#283，INV-02）"
    )
    p.add_argument("--report", required=True, help="adversary-report/v1 JSON 路径（- = stdin）")
    p.add_argument("--repo", required=True, help="目标仓库 owner/name（spec PR 所在仓）")
    p.add_argument("--pr-number", type=int, required=True, help="spec PR 编号（仅用于 summary 引用）")
    p.add_argument("--head-sha", required=True, help="spec PR head commit SHA（check run 挂接点）")
    p.add_argument("--gh-token", default=os.environ.get("GH_TOKEN"), help="App 令牌（checks:write）")
    p.add_argument("--name", default="adversary", help="check run 名称（默认 adversary）")
    p.add_argument("--dry-run", action="store_true", help="仅打印请求体，不实际创建")
    return p.parse_args(argv)


def load_report(path: str) -> dict:
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"报告 JSON 解析失败：{e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"报告根类型错误：{type(data).__name__}")
    return data


def validate_report(data: dict) -> list[str]:
    """校验 adversary-report/v1 schema；返回缺失字段列表（空=通过）。"""
    schema_key = data.get("schema")
    if schema_key != "adversary-report/v1":
        return [f"schema={schema_key!r}（期望 adversary-report/v1）"]
    return [k for k in REPORT_REQUIRED if k not in data]


def verdict_to_conclusion(verdict: str) -> str:
    """verdict → check run 结论（fail-closed：未知 verdict → failure）。"""
    c = VERDICT_CONCLUSION.get(verdict)
    if c is None:
        err(f"未知 verdict={verdict!r}，按 fail-closed 判 failure")
        return "failure"
    return c


def build_check_run(data: dict, pr_number: int, repo: str) -> dict:
    verdict = data.get("verdict", "missing")
    conclusion = verdict_to_conclusion(verdict)
    blocking = data.get("blocking", conclusion == "failure")
    exploited = data.get("exploited", [])
    holes = data.get("holes", [])
    attempt_count = data.get("attempt_count", 0)
    ts = data.get("ts", now_iso())
    target = data.get("target", "")

    # 摘要（Markdown）：verdict + 钻洞归因 + 尝试数
    summary_lines = [
        f"**verdict: {verdict}** —— {'blocking' if blocking else '放行'}",
        f"target: `{target}`",
        f"attempts: {attempt_count}",
    ]
    if exploited:
        summary_lines.append(f"exploited strategies: {', '.join(exploited)}")
    if holes:
        hole_strs = [f"{h.get('strategy', '?')}→{h.get('suite_gap', '?')}" for h in holes[:5]]
        summary_lines.append("holes: " + "; ".join(hole_strs))
    if verdict == "no-attempts":
        summary_lines.append("_no-attempts：白卷/infra，不进入失败/锁卡分支（AC-15）_")
    summary_lines.append(f"_spec PR #{pr_number} @ {repo}_")

    return {
        "name": "adversary",
        "head_sha": None,  # 由调用方注入
        "status": "completed",
        "conclusion": conclusion,
        "started_at": ts,
        "completed_at": now_iso(),
        "output": {
            "title": f"adversary 红队审计：{verdict}",
            "summary": "\n".join(summary_lines),
        },
    }


def create_check_run(repo: str, token: str, payload: dict, dry_run: bool) -> dict:
    """POST /repos/{repo}/check-runs（checks:write）。"""
    payload = dict(payload)
    payload["head_sha"] = payload.pop("_head_sha")
    if dry_run:
        return {"dry_run": True, "payload": payload}

    if not token:
        die(2, "GH_TOKEN 未设置（需要 checks:write 权限的 App 令牌）")

    url = f"https://api.github.com/repos/{repo}/check-runs"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "cloudbrid-agent",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"创建 check run 失败 HTTP {e.code}: {body}") from e


def main(argv=None) -> int:
    args = parse_args(argv)

    # 1) 加载 + 校验报告
    try:
        report = load_report(args.report)
    except ValueError as e:
        die(3, f"报告加载失败（fail-closed）：{e}")

    missing = validate_report(report)
    if missing:
        die(3, f"报告 schema 缺必填字段 {missing}（fail-closed：无有效 verdict 不放行）")

    # 2) 构造 check run
    payload = build_check_run(report, args.pr_number, args.repo)
    payload["_head_sha"] = args.head_sha
    if args.name != "adversary":
        payload["name"] = args.name
        payload["output"]["title"] = f"{args.name}：{report.get('verdict', 'missing')}"

    # 3) 写回
    try:
        result = create_check_run(args.repo, args.gh_token, payload, args.dry_run)
    except RuntimeError as e:
        die(2, f"check run 写回失败：{e}")

    if result.get("dry_run"):
        print(json.dumps(result["payload"], ensure_ascii=False, indent=2))
        print("（dry-run：未实际创建 check run）", file=sys.stderr)
    else:
        cr_id = result.get("id", "?")
        conclusion = result.get("conclusion", "?")
        print(f"check run #{cr_id} created: conclusion={conclusion}")
        print(json.dumps({"id": cr_id, "conclusion": conclusion, "verdict": report.get("verdict")}, ensure_ascii=False))

    # 4) 退出码反映 verdict（供 workflow 步参考）
    verdict = report.get("verdict")
    if verdict == "insufficient":
        return 1  # blocking
    if verdict == "no-attempts":
        return 3  # infra（恒绿防御语义由 workflow 层解释）
    return 0


if __name__ == "__main__":
    sys.exit(main())
