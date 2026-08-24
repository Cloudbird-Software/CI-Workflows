#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run.py —— adversary workflow 辅助模式（W3-C1 .github#277，ADR-0067）。

adversary.yml 引用而此前从未落地的两个辅助模式（2026-08-24 e2e 修复补齐——
W3-C1 合入后所有 run 均止步于前置故障，死引用未被发现）：

  credential-scan  AC-6 凭据形状扫描：环境变量中凭据形值只允许 LLM_API_KEY 一枚
                   （出向窗口内多一枚凭据=多一条外泄通道）。--env-dump 落盘脱敏
                   快照（键名+形状标记，绝不落值）。
  validate         adversary-report/v1 schema 校验（白卷即失败，AC-15）——
                   与 check_run_writeback.validate_report 同一契约。

退出码：0=通过 | 2=违规/报告不可用（fail-closed）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# 凭据形状（前缀/模式 → 标签）。命中即视为一枚凭据。
CRED_PATTERNS = [
    (re.compile(r"^ghp_[A-Za-z0-9]{20,}"), "classic-pat"),
    (re.compile(r"^gho_[A-Za-z0-9]{20,}"), "oauth-token"),
    (re.compile(r"^ghs_[A-Za-z0-9]{20,}"), "app-installation-token"),
    (re.compile(r"^ghu_[A-Za-z0-9]{20,}"), "user-token"),
    (re.compile(r"^github_pat_[A-Za-z0-9_]{20,}"), "fine-grained-pat"),
    (re.compile(r"^sk-[A-Za-z0-9_-]{16,}"), "sk-provider-key"),
    (re.compile(r"^ak-[A-Za-z0-9_-]{16,}"), "ak-provider-key"),
    (re.compile(r"^AIza[0-9A-Za-z_-]{30,}"), "google-key"),
]
# 允许存在的唯一凭据变量名（judge-deep 攻击步只持有 provider key）
ALLOWED_CRED_KEYS = {"LLM_API_KEY"}


def cred_shape(value: str):
    for rx, label in CRED_PATTERNS:
        if rx.match(value or ""):
            return label
    return None


def cmd_credential_scan(args) -> int:
    findings = []
    dump = {}
    for key, value in sorted(os.environ.items()):
        shape = cred_shape(value)
        dump[key] = {"shape": shape or "plain", "len": len(value or "")}
        if shape and key not in ALLOWED_CRED_KEYS:
            findings.append(f"{key}（{shape}）")
    if args.env_dump:
        with open(args.env_dump, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema": "credential-scan/v1", "findings": findings, "env": dump},
                      f, ensure_ascii=False, indent=2)
    if findings:
        print(f"::error::AC-6 违规：检出白名单外凭据形环境变量 {findings}（唯一允许：LLM_API_KEY）")
        return 2
    print("OK credential-scan：环境仅含 LLM_API_KEY 一枚凭据（AC-6）")
    return 0


REPORT_REQUIRED = ["schema", "ts", "target", "verdict", "blocking"]


def cmd_validate(args) -> int:
    try:
        with open(args.report, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"::error::报告不可读/不可解析：{e}")
        return 2
    if not isinstance(data, dict):
        print(f"::error::报告根类型错误：{type(data).__name__}")
        return 2
    if data.get("schema") != "adversary-report/v1":
        print(f"::error::schema={data.get('schema')!r}（期望 adversary-report/v1）")
        return 2
    missing = [k for k in REPORT_REQUIRED if k not in data]
    if missing:
        print(f"::error::报告缺必填字段 {missing}")
        return 2
    verdict = data.get("verdict")
    if verdict not in ("survived", "insufficient", "no-attempts", "skip"):
        print(f"::error::verdict 非法：{verdict!r}")
        return 2
    print(f"OK validate：adversary-report/v1（verdict={verdict} blocking={data.get('blocking')}）")
    return 0


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # 兼容 adversary.yml 的 `--mode <name>` 调用风格（等价于子命令形式）
    if argv and argv[0] == "--mode":
        argv = argv[1:]
    p = argparse.ArgumentParser(description="adversary workflow 辅助模式（credential-scan / validate）")
    sub = p.add_subparsers(dest="mode", required=True)
    cs = sub.add_parser("credential-scan", help="AC-6 凭据形状扫描（唯一允许 LLM_API_KEY）")
    cs.add_argument("--env-dump", default=None, help="脱敏环境快照输出路径（键+形状，不含值）")
    va = sub.add_parser("validate", help="adversary-report/v1 schema 校验（AC-15）")
    va.add_argument("--report", required=True, help="adversary-report.json 路径")
    args = p.parse_args(argv)
    if args.mode == "credential-scan":
        return cmd_credential_scan(args)
    return cmd_validate(args)


if __name__ == "__main__":
    sys.exit(main())
