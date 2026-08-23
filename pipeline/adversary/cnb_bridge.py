#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cnb_bridge.py —— CNB dispatch 通道 + canary + fallback 链（W3-C4 .github#280，ADR-0067/0079）

经 CNB（api.cnb.cool）分发红队/verifier 任务到云端沙箱执行，与自有 API 构成
fallback 链。凭据纪律（INV-02 / AC-6）：CNB 沙箱只接触公开内容，零 GitHub 凭据
注入——本模块在派发前做凭据形状扫描，发现 GitHub 凭据形状即拒绝派发（fail-closed）。

通道链（fallback）：
  1. CNB        —— 主通道：CNB_TOKEN org secret 注入，canary 先行（30–60s echo 任务）
                   验证云端存活后再派真实任务；窗口抢占/投递/轮询/收集。
  2. 自有 API   —— CNB 失败时回退到 vars.LLM_ENDPOINT 直连（ADR-0048 现状）。
  3. no-attempts—— 自有 API 也失败：锁 needs-human + 自动开 issue，不产出判定。

熔断：连续 3 次 fallback 或 CNB 额度尽 → 自动开 type:infra issue 报人。

与 W3-C1 adversary workflow（run.py / run-adversary.sh）调用接口兼容：
  - 输入：--issue / --spec-path / --card-id / --audit-run-id（同 trigger.py）
  - 输出：JSON 元数据（cnb-bridge-report/v1）供下游状态机消费
  - 退出码：0=CNB 成功 | 1=自有 API 成功（降级） | 2=配置/凭据/环境异常
             3=no-attempts（已锁 needs-human + 开 issue）

用法：
  python3 pipeline/adversary/cnb_bridge.py dispatch \
      --issue 279 --spec-path specs/ISSUE-263/spec.md --card-id ISSUE-263 \
      [--cnb-token $CNB_TOKEN] [--canary-seconds 45] [--fallback-chain cnb,own-api,no-attempts]

  python3 pipeline/adversary/cnb_bridge.py canary \
      [--cnb-token $CNB_TOKEN] [--canary-seconds 45]

  python3 pipeline/adversary/cnb_bridge.py credential-audit \
      --payload-file task_payload.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CNB_API_BASE = os.environ.get("CNB_API_BASE", "https://api.cnb.cool")
DEFAULT_CANARY_SECONDS = 45  # 规范：30–60s echo 任务
MAX_FALLBACK_CONSECUTIVE = 3
GITHUB_CREDS_PATTERNS = [
    re.compile(r"(?i)github[_\-]?(?:token|key|secret|password)"),
    re.compile(r"(?i)gh[pousr]_[A-Za-z0-9]{36,}"),     # ghp_ gho_ ghu_ ghs_ ghr_
    re.compile(r"(?i)github_pat_[A-Za-z0-9_]{22,}"),   # fine-grained PAT
    re.compile(r"(?i)x-access-token:ghs?_[A-Za-z0-9]"), # git 嵌入凭据
    re.compile(r"(?i)GH_TOKEN|GITHUB_TOKEN"),
]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def err(msg: str) -> None:
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)


def die(code: int, msg: str) -> None:
    err(msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# 凭据纪律：CNB 沙箱零 GitHub 凭据（INV-02 / AC-6）
# ---------------------------------------------------------------------------

def credential_audit(payload: dict) -> dict:
    """扫描 payload 中是否出现 GitHub 凭据形状。发现即拒绝派发（fail-closed）。"""
    violations: list[dict] = []

    def _scan(obj, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if any(p.search(k) for p in GITHUB_CREDS_PATTERNS):
                    violations.append({"path": f"{path}.{k}", "shape": "credential-key", "action": "reject"})
                _scan(v, f"{path}.{k}")
        elif isinstance(obj, str):
            for p in GITHUB_CREDS_PATTERNS:
                if p.search(obj):
                    violations.append({"path": path, "shape": "credential-value", "action": "reject"})
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _scan(item, f"{path}[{i}]")

    _scan(payload, "$")
    return {
        "scanned_at": now_iso(),
        "violations": violations,
        "compliant": len(violations) == 0,
    }


# ---------------------------------------------------------------------------
# CNB 通道
# ---------------------------------------------------------------------------

class CNBBridge:
    def __init__(self, token: str, api_base: str = CNB_API_BASE, timeout: int = 30):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.quota_remaining: int | None = None

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        url = f"{self.api_base}{path}"
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if body else None
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")[:500]
            return e.code, {"_http_error": text}
        except Exception as e:
            return 0, {"_transport_error": str(e)}

    def canary(self, seconds: int = DEFAULT_CANARY_SECONDS) -> dict:
        """canary 先行：派发 echo 任务，验证云端存活。30–60s 后返回 echo 结果。"""
        body = {
            "task_type": "echo",
            "payload": {"message": f"canary-{now_iso()}", "sleep_seconds": seconds},
            "public_only": True,
        }
        status, resp = self._request("POST", "/v1/tasks", body)
        if status not in (200, 201, 202):
            return {"ok": False, "stage": "canary-submit", "status": status, "detail": resp}
        task_id = resp.get("task_id") or resp.get("id")
        if not task_id:
            return {"ok": False, "stage": "canary-submit", "status": status, "detail": resp}

        # 轮询收集（窗口抢占后等待）
        deadline = time.time() + seconds + 30
        while time.time() < deadline:
            st, poll = self._request("GET", f"/v1/tasks/{task_id}")
            if st == 200 and poll.get("status") in ("done", "completed", "success"):
                return {"ok": True, "stage": "canary-complete", "task_id": task_id, "result": poll}
            if poll.get("status") in ("failed", "error"):
                return {"ok": False, "stage": "canary-failed", "task_id": task_id, "detail": poll}
            time.sleep(5)
        return {"ok": False, "stage": "canary-timeout", "task_id": task_id}

    def dispatch(self, task_payload: dict) -> dict:
        """派发真实任务到 CNB 沙箱。返回投递元数据。"""
        body = {
            "task_type": "adversary",
            "payload": task_payload,
            "public_only": True,
        }
        status, resp = self._request("POST", "/v1/tasks", body)
        if status in (200, 201, 202):
            return {"ok": True, "task_id": resp.get("task_id") or resp.get("id"), "status": status}
        if status == 429 or status == 402:
            self.quota_remaining = 0
            return {"ok": False, "stage": "quota-exhausted", "status": status, "detail": resp}
        return {"ok": False, "stage": "dispatch-failed", "status": status, "detail": resp}

    def poll(self, task_id: str, timeout: int = 600) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, poll = self._request("GET", f"/v1/tasks/{task_id}")
            if st == 200 and poll.get("status") in ("done", "completed", "success"):
                return {"ok": True, "result": poll}
            if poll.get("status") in ("failed", "error"):
                return {"ok": False, "detail": poll}
            time.sleep(10)
        return {"ok": False, "stage": "poll-timeout"}


# ---------------------------------------------------------------------------
# 自有 API 回退（ADR-0048 直连 provider）
# ---------------------------------------------------------------------------

class OwnAPIFallback:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def execute(self, task_payload: dict) -> dict:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are an adversary sandbox executing a public-only task."},
                {"role": "user", "content": json.dumps(task_payload, ensure_ascii=False)},
            ],
            "max_tokens": 4096,
        }
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=data,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = json.loads(resp.read().decode("utf-8", errors="replace"))
                content = ((raw.get("choices") or [{}])[0].get("message") or {}).get("content", "")
                return {"ok": True, "content": content, "usage": raw.get("usage", {})}
        except Exception as e:
            return {"ok": False, "detail": str(e)}


# ---------------------------------------------------------------------------
# fallback 链编排 + 熔断
# ---------------------------------------------------------------------------

def open_infra_issue(repo: str, token: str, title: str, body: str) -> dict:
    """自动开 type:infra issue（熔断/额度尽时报警）。"""
    if not token:
        return {"ok": False, "reason": "no-token"}
    url = f"https://api.github.com/repos/{repo}/issues"
    data = json.dumps({"title": title, "body": body, "labels": ["type:infra"]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="replace"))
            return {"ok": True, "issue_number": d.get("number"), "html_url": d.get("html_url")}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def run_fallback_chain(args, task_payload: dict, audit: dict) -> tuple[int, dict]:
    """执行 fallback 链。返回 (exit_code, report)。"""
    chain = [s.strip() for s in args.fallback_chain.split(",")]
    consecutive_fallback = 0
    channel_log: list[dict] = []
    cnb_token = args.cnb_token or os.environ.get("CNB_TOKEN", "")
    github_token = args.github_token or os.environ.get("GITHUB_TOKEN", "")

    for channel in chain:
        if channel == "cnb":
            if not cnb_token:
                channel_log.append({"channel": "cnb", "ok": False, "reason": "CNB_TOKEN 未注入"})
                continue
            bridge = CNBBridge(cnb_token)
            # canary 先行
            canary = bridge.canary(args.canary_seconds)
            channel_log.append({"channel": "cnb", "phase": "canary", **canary})
            if not canary.get("ok"):
                consecutive_fallback += 1
                channel_log.append({"channel": "cnb", "phase": "skip-dispatch", "reason": "canary 失败"})
                if consecutive_fallback >= MAX_FALLBACK_CONSECUTIVE:
                    break
                continue
            # 真实派发
            disp = bridge.dispatch(task_payload)
            channel_log.append({"channel": "cnb", "phase": "dispatch", **disp})
            if disp.get("ok"):
                return 0, {"channel": "cnb", "task_id": disp.get("task_id"), "channel_log": channel_log}
            if disp.get("stage") == "quota-exhausted":
                # 额度尽 → 熔断开 issue
                issue = open_infra_issue(
                    args.target_repo, github_token,
                    f"[W3-C4] CNB 额度耗尽（card {args.card_id or args.issue}）",
                    f"CNB dispatch 通道额度耗尽，连续 fallback={consecutive_fallback}。\n\n"
                    f"- card: {args.card_id or args.issue}\n- ts: {now_iso()}\n"
                    f"- 需人工补充 CNB 配额或切换自有 API。",
                )
                channel_log.append({"channel": "cnb", "phase": "quota-exhausted", "issue": issue})
                return 2, {"channel": "cnb-quota-exhausted", "channel_log": channel_log}
            consecutive_fallback += 1

        elif channel == "own-api":
            own = OwnAPIFallback(
                args.own_api_url or os.environ.get("LLM_ENDPOINT", ""),
                args.own_api_key or os.environ.get("LLM_API_KEY", ""),
                args.own_api_model or os.environ.get("LLM_MODEL", "glm-4.6"),
            )
            if not own.api_key:
                channel_log.append({"channel": "own-api", "ok": False, "reason": "LLM_API_KEY 未注入"})
                continue
            res = own.execute(task_payload)
            channel_log.append({"channel": "own-api", **res})
            if res.get("ok"):
                return 1, {"channel": "own-api", "content": res.get("content"), "channel_log": channel_log}
            consecutive_fallback += 1

        elif channel == "no-attempts":
            # 锁 needs-human + 自动开 issue
            issue = open_infra_issue(
                args.target_repo, github_token,
                f"[W3-C4] adversary 无可用通道（card {args.card_id or args.issue}）",
                f"CNB + 自有 API 均失败，连续 fallback={consecutive_fallback} ≥ {MAX_FALLBACK_CONSECUTIVE}。\n\n"
                f"- card: {args.card_id or args.issue}\n- ts: {now_iso()}\n"
                f"- 状态已锁 needs-human，需人工介入恢复通道。\n\n"
                f"```json\n{json.dumps(channel_log, ensure_ascii=False, indent=2)}\n```",
            )
            channel_log.append({"channel": "no-attempts", "issue": issue})
            return 3, {"channel": "no-attempts", "channel_log": channel_log}

        else:
            channel_log.append({"channel": channel, "ok": False, "reason": f"未知通道 {channel}"})

    return 2, {"channel": "exhausted", "channel_log": channel_log}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_dispatch(args) -> int:
    task_payload = {
        "issue": args.issue,
        "spec_path": args.spec_path,
        "card_id": args.card_id or "",
        "audit_run_id": args.audit_run_id or "",
        "triggered_by": "pipeline/adversary/cnb_bridge.py",
    }
    # 凭据审计（CNB 沙箱零 GitHub 凭据）
    audit = credential_audit(task_payload)
    if not audit["compliant"]:
        err(f"凭据审计失败：payload 含 GitHub 凭据形状，拒绝派发（INV-02）")
        return 2

    code, report = run_fallback_chain(args, task_payload, audit)
    report["credential_audit"] = audit
    report["ts"] = now_iso()
    report["schema"] = "cnb-bridge-report/v1"
    Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report_out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


def cmd_canary(args) -> int:
    token = args.cnb_token or os.environ.get("CNB_TOKEN", "")
    if not token:
        die(2, "CNB_TOKEN 未注入（org secret 或 --cnb-token）")
    bridge = CNBBridge(token)
    result = bridge.canary(args.canary_seconds)
    result["ts"] = now_iso()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


def cmd_credential_audit(args) -> int:
    if not args.payload_file or not Path(args.payload_file).is_file():
        die(2, f"payload 文件不存在：{args.payload_file}")
    payload = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
    result = credential_audit(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["compliant"] else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cnb_bridge.py", description="CNB dispatch 通道 + canary + fallback 链")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("dispatch", help="经 fallback 链派发任务")
    p.add_argument("--issue", required=True)
    p.add_argument("--spec-path", required=True)
    p.add_argument("--card-id", default="")
    p.add_argument("--audit-run-id", default="")
    p.add_argument("--cnb-token", default="")
    p.add_argument("--github-token", default="")
    p.add_argument("--target-repo", default="Cloudbird-Software/.github")
    p.add_argument("--fallback-chain", default="cnb,own-api,no-attempts")
    p.add_argument("--canary-seconds", type=int, default=DEFAULT_CANARY_SECONDS)
    p.add_argument("--own-api-url", default="")
    p.add_argument("--own-api-key", default="")
    p.add_argument("--own-api-model", default="")
    p.add_argument("--report-out", default="cnb-bridge-report.json")
    p.set_defaults(fn=cmd_dispatch)

    p = sub.add_parser("canary", help="仅执行 canary echo 任务")
    p.add_argument("--cnb-token", default="")
    p.add_argument("--canary-seconds", type=int, default=DEFAULT_CANARY_SECONDS)
    p.set_defaults(fn=cmd_canary)

    p = sub.add_parser("credential-audit", help="审计 payload 凭据形状")
    p.add_argument("--payload-file", default="")
    p.set_defaults(fn=cmd_credential_audit)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except Exception as e:
        err(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())

