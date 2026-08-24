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
  skip         → success        （W4-C3 EXPECTED_SKIP：开发路径豁免，确定性派生，放行）
  未知/缺失     → failure        （fail-closed：无有效 verdict 不放行）

定位策略（按优先级）：
  1) --head-sha + --repo（+ --pr-number 仅作摘要引用）——显式定位，零 API；
  2) --spec-pr N + --repo（或 GITHUB_REPOSITORY）——经 Pulls API 解析 head SHA；
  3) 报告内 target 字段仅作摘要引用，不足以定位 → exit 2。

退出码（与 run-adversary.sh 语义对齐，供 workflow 步消费）：
  0 = 写回成功且 verdict 放行（survived/skip）
  1 = 写回成功且 verdict=insufficient（blocking——本 run 红）
  2 = 配置/定位/令牌/写回失败（fail-closed）
  3 = 写回成功但 verdict=no-attempts（白卷 infra，留痕报人）
  4 = 报告不可用（JSON 解析失败 / schema 校验不过——无 verdict 不放行）

用法：
  # workflow 内（adversary.yml 攻击步之后，App 令牌经 gov/scripts/gh-app-token.sh 铸造）
  python3 pipeline/adversary/check_run_writeback.py \
      --report "$RUNNER_TEMP/adversary-report.json" \
      --repo Cloudbird-Software/.github --spec-pr 316 --head-sha "$SHA" \
      --gh-token "$APP_TOKEN"

  # 纯逻辑自测入口见 tests/test_check_run_writeback_logic.py（build_check_body）。
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
from typing import Optional

# adversary-report/v1 必填字段（与 pipeline/adversary/adversary.py 产出一致）
REPORT_REQUIRED = ["schema", "ts", "target", "verdict", "blocking"]
VERDICT_CONCLUSION = {
    "survived": "success",
    "insufficient": "failure",
    "no-attempts": "neutral",
    # W4-C3 EXPECTED_SKIP：开发路径豁免（diff 路径集无 specs/**），确定性派生，放行
    "skip": "success",
}
# GitHub check run output 字段长度上限（API 文档：title 255 / summary 65535 / text 65535）
MAX_OUTPUT_TEXT = 65535
MAX_OUTPUT_SUMMARY = 65535
MAX_TITLE = 255


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
    p.add_argument("--spec-pr", type=int, default=None,
                   help="spec PR 编号（与 --repo 配合经 Pulls API 解析 head SHA；亦可仅作摘要引用）")
    p.add_argument("--pr-number", type=int, default=None,
                   help="--spec-pr 别名（兼容早期接口）")
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"),
                   help="目标仓库 owner/name（spec PR 所在仓；缺省 GITHUB_REPOSITORY）")
    p.add_argument("--head-sha", default=None,
                   help="spec PR head commit SHA（check run 挂接点；给定则免 API 解析）")
    p.add_argument("--gh-token", default=None,
                   help="App 令牌（checks:write）；缺省取 APP_TOKEN → GH_TOKEN")
    p.add_argument("--name", default="adversary", help="check run 名称（默认 adversary）")
    p.add_argument("--dry-run", action="store_true", help="仅打印请求体，不实际创建")
    return p.parse_args(argv)


def resolve_token(args: argparse.Namespace) -> Optional[str]:
    """令牌解析：--gh-token > APP_TOKEN > GH_TOKEN（W4：全缺 → exit 2）。"""
    token = args.gh_token or os.environ.get("APP_TOKEN") or os.environ.get("GH_TOKEN")
    if not token and not os.environ.get("CB_APP_ID"):
        die(2, "无令牌可用（--gh-token / APP_TOKEN / GH_TOKEN 均缺失，且无 CB_APP_ID 可走铸造路径）")
    return token


def api_json(path_or_url: str, token: str, method: str = "GET", payload: Optional[dict] = None) -> dict:
    url = path_or_url if path_or_url.startswith("http") else f"https://api.github.com{path_or_url}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "cloudbrid-agent",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API {method} {url} 失败 HTTP {e.code}: {body[:400]}") from e


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


def validate_report(data: dict) -> list:
    """校验 adversary-report/v1 schema；返回缺失/不符字段列表（空=通过）。"""
    schema_key = data.get("schema")
    if schema_key != "adversary-report/v1":
        return [f"schema={schema_key!r}（期望 adversary-report/v1）"]
    return [k for k in REPORT_REQUIRED if k not in data]


def verdict_to_conclusion(verdict) -> str:
    """verdict → check run 结论（fail-closed：未知/缺失 verdict → failure）。"""
    if not isinstance(verdict, str):
        err(f"verdict 缺失/类型错误（{verdict!r}），按 fail-closed 判 failure")
        return "failure"
    c = VERDICT_CONCLUSION.get(verdict)
    if c is None:
        err(f"未知 verdict={verdict!r}，按 fail-closed 判 failure")
        return "failure"
    return c


def _clip(s: str, limit: int) -> str:
    """按字符截断（超限时尾部加省略标记，总长恒 ≤ limit）。"""
    if len(s) <= limit:
        return s
    mark = "…（truncated）"
    return s[: limit - len(mark)] + mark


def build_check_body(data: dict, name: str = "adversary") -> dict:
    """纯函数：adversary-report/v1 → check run 请求体（不含 head_sha，由调用方注入）。

    - conclusion 映射 fail-closed（未知/缺失 verdict → failure）；
    - output.text 承载 attempts/parse_errors 全量留痕并截断 ≤65535；
    - output.summary 承载 verdict 摘要并截断 ≤65535。
    """
    verdict = data.get("verdict", "missing")
    conclusion = verdict_to_conclusion(verdict)
    blocking = data.get("blocking", conclusion == "failure")
    exploited = data.get("exploited", []) or []
    holes = data.get("holes", []) or []
    attempts = data.get("attempts", []) or []
    parse_errors = data.get("parse_errors", []) or []
    attempt_count = data.get("attempt_count", len(attempts))
    ts = data.get("ts", now_iso())
    target = data.get("target", "")

    summary_lines = [
        f"**verdict: {verdict}** —— {'blocking' if blocking else '放行'}",
        f"target: `{target}`",
        f"attempts: {attempt_count}（exploited {len(exploited)} / holes {len(holes)}）",
    ]
    if exploited:
        summary_lines.append(f"exploited strategies: {', '.join(map(str, exploited))}")
    if holes:
        hole_strs = [f"{h.get('strategy', '?')}→{h.get('suite_gap', '?')}" for h in holes[:5]]
        summary_lines.append("holes: " + "; ".join(hole_strs))
    if verdict == "no-attempts":
        summary_lines.append("_no-attempts：白卷/infra，不进入失败/锁卡分支（AC-15）_")

    # 全量留痕（逐攻击尝试 + 解析错误），超长按 API 上限截断
    text_lines = ["# adversary attempts", ""]
    for a in attempts:
        text_lines.append(json.dumps(a, ensure_ascii=False))
    if parse_errors:
        text_lines += ["", "# parse_errors", *map(str, parse_errors)]
    text_lines += ["", "# raw report", json.dumps(data, ensure_ascii=False)]

    title = f"adversary 红队审计：{verdict}"
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": ts,
        "completed_at": now_iso(),
        "output": {
            "title": _clip(title, MAX_TITLE),
            "summary": _clip("\n".join(summary_lines), MAX_OUTPUT_SUMMARY),
            "text": _clip("\n".join(text_lines), MAX_OUTPUT_TEXT),
        },
    }


def create_check_run(repo: str, token: str, head_sha: str, body: dict, dry_run: bool) -> dict:
    """POST /repos/{repo}/check-runs（checks:write）。dry-run 只打印请求体。"""
    if dry_run:
        return {"dry_run": True, "payload": {**body, "head_sha": head_sha}}
    if not token:
        die(2, "无令牌（需要 checks:write 权限的 App 令牌）")
    payload = {**body, "head_sha": head_sha}
    try:
        result = api_json(f"/repos/{repo}/check-runs", token, method="POST", payload=payload)
    except RuntimeError as e:
        die(2, f"check run 写回失败：{e}")
    return result


def main(argv=None) -> int:
    args = parse_args(argv)

    # 1) 加载报告（不可解析 → exit 4：无有效 verdict 不放行）
    try:
        report = load_report(args.report)
    except ValueError as e:
        die(4, f"报告加载失败（fail-closed）：{e}")

    # 2) 定位 PR head（无 --head-sha 且无 --spec-pr 且报告无 target → exit 2，
    #    先于 schema 校验：定位缺失属调用面配置错误，与报告内容有效性互斥可判）
    pr_number = args.spec_pr or args.pr_number
    if not args.head_sha and not pr_number and not report.get("target"):
        die(2, "无法定位 spec PR（--head-sha / --spec-pr / 报告 target 三者皆无）")

    # 3) schema 校验（不符 → exit 4）
    missing = validate_report(report)
    if missing:
        die(4, f"报告 schema 不符 {missing}（fail-closed：无有效 verdict 不放行）")
    token = resolve_token(args)
    repo = args.repo
    head_sha = args.head_sha
    if not head_sha:
        if not pr_number or not repo:
            die(2, f"--spec-pr/--repo 不足以解析 head SHA（spec_pr={pr_number} repo={repo}）")
        try:
            pr = api_json(f"/repos/{repo}/pulls/{pr_number}", token)
        except RuntimeError as e:
            die(2, f"解析 PR #{pr_number} head 失败：{e}")
        head_sha = pr.get("head", {}).get("sha")
        if not head_sha:
            die(2, f"PR #{pr_number} 响应缺 head.sha")

    # 3) 构造 + 写回
    body = build_check_body(report, name=args.name)
    result = create_check_run(repo, token, head_sha, body, args.dry_run)
    if result.get("dry_run"):
        print(json.dumps(result["payload"], ensure_ascii=False, indent=2))
        print("（dry-run：未实际创建 check run）", file=sys.stderr)
    else:
        cr_id = result.get("id", "?")
        conclusion = result.get("conclusion", "?")
        print(f"check run #{cr_id} created on {repo}@{head_sha[:8]}: conclusion={conclusion}")
        print(json.dumps({"id": cr_id, "conclusion": conclusion,
                          "verdict": report.get("verdict"), "repo": repo}, ensure_ascii=False))

    # 4) 退出码反映 verdict（供 workflow 步参考）
    verdict = report.get("verdict")
    if verdict == "insufficient":
        return 1  # blocking
    if verdict == "no-attempts":
        return 3  # infra（恒绿防御语义由 workflow 层解释）
    return 0


if __name__ == "__main__":
    sys.exit(main())
