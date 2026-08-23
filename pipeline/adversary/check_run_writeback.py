#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_run_writeback.py —— 红队 verdict 写回 spec PR check run（W4-C2 .github#283，INV-02/AC-4/AC-14）

adversary 产出 verdict 后，以 App 令牌（单仓作用域、checks:write，1h 过期）
向 spec PR 写回 check run。check 名 = "adversary"，纳入 specs/**
required checks（W4-C3 由 .github/governance/rulesets/main-protection.json 登记）。
写回经 App 令牌（INV-02：GITHUB_TOKEN 不持有状态写权；check 写回=状态写）。

写回内容：
  - name: "adversary"
  - status: completed
  - conclusion:
        verdict=survived    → success
        verdict=insufficient → failure（语义审计阻断，AC-14）
        verdict=no-attempts → neutral（infra，不阻断）
        verdict=其它/缺失    → failure（fail-closed）
  - output.title/summary：verdict + 攻击摘要（中英双语，summary ≤65535 字符）
  - output.text：完整报告 JSON（截断到 GitHub check run 65535 字符上限）

PR 定位策略（确定性，禁人工打标）：
  - 优先取 --spec-pr（显式 PR 号，由调用方从 issue/PR 上下文解析）
  - 否则取 adversary 报告 target 路径，查 .github 仓 open PR 中 head 含该路径
    变更者（list-pull-requests + 逐 PR 文件列表；多匹配取最近 updated）
  - 失败即 fail-closed exit 2（不静默丢弃写回义务——spec PR 无 adversary
    check 必须红，见 AC-4 负向断言；写回失败≠免除义务）

App 令牌铸造（self-contained）：
  - 优先取 env APP_TOKEN（调用方已铸）
  - 否则用 CB_APP_ID + AGENT_APP_SECRET（/ _FILE）自行铸造：
    JWT(iat, exp, iss=app_id) → GET app/installations → POST installation/access_tokens
  - 令牌作用域 = 单仓（--spec-repo 指定），与 gh-app-token.sh 同语义

用法：
  python3 pipeline/adversary/check_run_writeback.py \
      --report <adversary-report.json> \
      --spec-pr <number> \
      [--spec-repo Cloudbird-Software/.github]

  # 或直接传令牌，跳过铸造：
  APP_TOKEN=<token> python3 ... --report ... --spec-pr ...

env（自行铸造时）：
  CB_APP_ID                GitHub App ID（缺省读 org 变量 CLOUDBIRD_APP_ID → 4632704）
  AGENT_APP_SECRET         私钥 PEM 内容（首选 AGENT_APP_SECRET_FILE 路径）
  APP_TOKEN                直接传入已铸好的令牌（优先）
退出码：0=写回成功 | 2=参数/环境/PR 定位失败 | 3=API 失败 | 4=报告无效
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

GITHUB_API = os.environ.get("CB_GITHUB_API", "https://api.github.com")
CHECK_RUN_NAME = "adversary"
MAX_OUTPUT_TEXT = 64000  # GitHub check run output.text 上限 65535；留余量


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(msg: str, rc: int = 2) -> None:
    print(f"::error::check_run_writeback: {msg}", file=sys.stderr)
    sys.exit(rc)


# ----------------------------------------------------------------------------
# App 令牌铸造（JWT → installation → token），与 gh-app-token.sh 同语义
# ----------------------------------------------------------------------------
def mint_installation_token(app_id: str, pem_private_key: str, spec_repo: str) -> str:
    """为 spec_repo 铸造单仓 App 安装令牌。返回令牌字符串。"""
    try:
        import jwt  # PyJWT（CI 环境可选依赖；缺失时退化到 PyJWT 纯 python 实现）
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 600, "iss": app_id}
        assertion = jwt.encode(payload, pem_private_key, algorithm="RS256")
    except Exception as e:
        die(f"JWT 签名失败（需 PyJWT + 有效 PEM 私钥）：{e}")

    def api(path: str, method: str = "GET", body: Any = None, token: str | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "check_run_writeback"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif path.endswith(("/installations",)):
            headers["Authorization"] = f"Bearer {assertion}"
        req = urllib.request.Request(GITHUB_API + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            return e.code, {"_http_error": err}

    # 1) 取 installation id（org 作用域）
    org = spec_repo.split("/")[0]
    st, installs = api("/app/installations")
    if st != 200:
        die(f"读取 App installations 失败 HTTP {st}: {installs}")
    inst_id = None
    for inst in installs:
        target = (inst.get("target_type") or "")
        acct = (inst.get("account") or {}).get("login", "")
        if acct.lower() == org.lower():
            inst_id = inst["id"]
            break
    if inst_id is None:
        die(f"App 未安装在组织 {org}（无 installation）")

    # 2) 取 installation access token，限定仓库
    repos_body = {"repositories": [spec_repo.split("/")[1]]} if "/" in spec_repo else None
    st, tok = api(f"/app/installations/{inst_id}/access_tokens", "POST", repos_body)
    if st not in (200, 201) or "token" not in tok:
        die(f"铸造 installation token 失败 HTTP {st}: {tok}")
    return tok["token"]


def get_app_token(spec_repo: str) -> str:
    """获取写回用 App 令牌（env 优先，否则自行铸造）。"""
    token = os.environ.get("APP_TOKEN", "")
    if token:
        return token
    app_id = os.environ.get("CB_APP_ID", "") or os.environ.get("CLOUDBIRD_APP_ID", "4632704")
    if not app_id:
        die("缺少 APP_TOKEN 或 CB_APP_ID，无法铸造 App 令牌")
    pem = os.environ.get("AGENT_APP_SECRET", "")
    pem_file = os.environ.get("AGENT_APP_SECRET_FILE", "")
    if not pem and pem_file:
        pem = _read_pem_file(pem_file)
    if not pem:
        die("缺少 AGENT_APP_SECRET / AGENT_APP_SECRET_FILE，无法铸造 App 令牌")
    return mint_installation_token(app_id, pem, spec_repo)


def _read_pem_file(path: str) -> str:
    # ~ 展开（兼容 MSYS/Git Bash）
    if path.startswith("~"):
        path = os.path.expanduser(path)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        die(f"读取私钥文件失败 {path}: {e}")


# ----------------------------------------------------------------------------
# GitHub API 薄封装
# ----------------------------------------------------------------------------
class Gh:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo
        self.org, self.name = repo.split("/", 1) if "/" in repo else ("", repo)

    def call(self, path: str, method: str = "GET", body: Any = None, expected: tuple[int, ...] = (200, 201, 204)) -> Any:
        url = GITHUB_API + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "check_run_writeback",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            msg = f"API {method} {path} HTTP {e.code}: {err[:500]}"
            if e.code in (403, 422):
                raise RuntimeError(f"{msg}（权限/参数问题）") from e
            raise RuntimeError(msg) from e

    def get_pr_head_sha(self, pr_number: int) -> str:
        d = self.call(f"/repos/{self.repo}/pulls/{pr_number}")
        sha = (d.get("head") or {}).get("sha", "")
        if not sha:
            die(f"PR #{pr_number} 无 head sha")
        return sha

    def list_open_prs(self) -> list[dict]:
        out: list[dict] = []
        for page in range(1, 6):  # 上限 5 页
            chunk = self.call(f"/repos/{self.repo}/pulls?state=open&per_page=100&page={page}&sort=updated&direction=desc")
            if not isinstance(chunk, list) or not chunk:
                break
            out.extend(chunk)
            if len(chunk) < 100:
                break
        return out

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

    def upsert_check_run(self, head_sha: str, body: dict, name: str = CHECK_RUN_NAME) -> dict:
        # 同名 check run 已存在则更新，否则创建
        existing = self.call(f"/repos/{self.repo}/commits/{head_sha}/check-runs?per_page=100")
        match = next((c for c in existing.get("check_runs", []) if c.get("name") == name), None)
        if match:
            return self.call(f"/repos/{self.repo}/check-runs/{match['id']}", "PATCH", body, expected=(200,))
        return self.call(f"/repos/{self.repo}/check-runs", "POST", body, expected=(201,))


# ----------------------------------------------------------------------------
# PR 定位（确定性）
# ----------------------------------------------------------------------------
def locate_spec_pr(gh: Gh, spec_pr: int | None, report_target: str) -> tuple[int, str]:
    """返回 (pr_number, head_sha)。优先 --spec-pr；否则按 target 路径匹配。"""
    if spec_pr is not None:
        return spec_pr, gh.get_pr_head_sha(spec_pr)

    if not report_target:
        die("无 --spec-pr 且报告无 target 路径，无法定位 spec PR")
    target_norm = report_target.replace("\\", "/").lstrip("./")
    best: tuple[int, str] | None = None
    best_updated = ""
    for pr in gh.list_open_prs():
        changed = gh.pr_changed_files(pr["number"])
        norm = [f.replace("\\", "/") for f in changed]
        if any(target_norm in n or n in target_norm for n in norm):
            updated = pr.get("updated_at", "")
            if updated >= best_updated:  # 多匹配取最近 updated
                best = (pr["number"], (pr.get("head") or {}).get("sha", ""))
                best_updated = updated
    if best is None:
        die(f"未找到变更含 target={target_norm} 的 open PR（spec PR 必须存在以供 adversary check 写回）")
    return best


# ----------------------------------------------------------------------------
# verdict → check run body
# ----------------------------------------------------------------------------
def build_check_body(report: dict, name: str = CHECK_RUN_NAME) -> dict:
    verdict = report.get("verdict", "")
    ts = report.get("ts") or now_iso()
    target = report.get("target", "")
    attempts = report.get("attempt_count", 0)
    holes = report.get("holes", [])
    exploited = report.get("exploited", [])

    conclusion_map = {"survived": "success", "insufficient": "failure", "no-attempts": "neutral"}
    conclusion = conclusion_map.get(verdict, "failure")  # fail-closed：未知 verdict = failure

    verdict_cn = {"survived": "套件通过考验", "insufficient": "套件不充分（blocking）",
                  "no-attempts": "零有效尝试（infra）"}.get(verdict, f"未知 verdict={verdict}")
    title = f"adversary: {verdict} — {verdict_cn}"

    hole_text = "; ".join(f"{h.get('strategy','')}→{h.get('suite_gap','')}" for h in holes) or "无"
    summary_lines = [
        f"verdict: {verdict}（{conclusion}）",
        f"target: {target}",
        f"攻击尝试: {attempts} 条；得手策略: {', '.join(exploited) or '无'}",
        f"钻洞归因: {hole_text}",
        f"schema: {report.get('schema','')}  ts: {ts}",
        "check 写回经 App 令牌（INV-02：checks:write，单仓作用域，1h 过期）。",
    ]
    summary = "\n".join(summary_lines)
    if len(summary) > MAX_OUTPUT_TEXT:
        summary = summary[: MAX_OUTPUT_TEXT - 3] + "..."

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if len(text) > MAX_OUTPUT_TEXT:
        text = text[: MAX_OUTPUT_TEXT - 3] + "..."

    return {
        "name": CHECK_RUN_NAME,
        "head_sha": None,  # 由调用方填入
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": now_iso(),
        "output": {"title": title[:255], "summary": summary, "text": text},
    }


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(prog="check_run_writeback.py",
                                 description="红队 verdict 写回 spec PR check run（W4-C2 .github#283，INV-02/AC-4）")
    ap.add_argument("--report", required=True, help="adversary 报告 JSON 路径（adversary-report/v1 schema）")
    ap.add_argument("--spec-pr", type=int, default=None, help="spec PR 号（优先；缺省时按报告 target 路径匹配）")
    ap.add_argument("--spec-repo", default="Cloudbird-Software/.github", help="spec PR 所在仓（令牌作用域）")
    ap.add_argument("--name", default=CHECK_RUN_NAME, help=f"check run 名（缺省 {CHECK_RUN_NAME}）")
    args = ap.parse_args()

    # 1) 读报告
    try:
        with open(args.report, encoding="utf-8") as f:
            report = json.load(f)
    except OSError as e:
        die(f"读报告失败 {args.report}: {e}")
    if report.get("schema") != "adversary-report/v1":
        die(f"报告 schema 不匹配（期望 adversary-report/v1，实际 {report.get('schema')!r}）", rc=4)

    check_name = args.name  # 局部别名，避免 global 与赋值顺序冲突

    # 2) 令牌
    token = get_app_token(args.spec_repo)

    # 3) 定位 spec PR
    gh = Gh(token, args.spec_repo)
    target = report.get("target", "")
    pr_number, head_sha = locate_spec_pr(gh, args.spec_pr, target)
    print(f"写回目标: {args.spec_repo}#{pr_number} @ {head_sha[:8]}", file=sys.stderr)

    # 4) 构建并写回 check run
    body = build_check_body(report, name=check_name)
    body["head_sha"] = head_sha
    try:
        res = gh.upsert_check_run(head_sha, body, name=check_name)
    except RuntimeError as e:
        die(f"写回 check run 失败: {e}", rc=3)
    print(f"check run 写回成功: {res.get('html_url', '')}", file=sys.stderr)
    print(json.dumps({"name": body["name"], "conclusion": body["conclusion"],
                      "pr": pr_number, "repo": args.spec_repo,
                      "html_url": res.get("html_url", "")}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
