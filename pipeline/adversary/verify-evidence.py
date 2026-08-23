#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify-evidence.py —— 红队/verifier 证据引用机械核对（W3-C5 .github#281，ADR-0067）

输入：一份 redteam/verifier 报告 JSON，内含 citations 数组；每条 citation 必须
带 file（相对 repo-dir 的路径）、line_start、line_end、exact_string、id。

行为（AC-9 / INV-03 / AC-2）：
1. run 开始时动态获取基准版本（git HEAD SHA + UTC 抓取时间）并写入报告；
   取不到 SHA 时直接判整体 infra 失败（exit 2，fail-closed）。
2. 对每条引用从当前已检出仓库读取真实工件，在 [line_start, line_end] 行范围内
   做字符串级精确匹配（exact_string 必须出现）。
3. 引用作废（void）情形：文件缺失、行号非法、精确字符串未命中、基准获取失败。
   任一引用被作废，报告 verdict 强制转 insufficient（作废是判定，不是记录）。
4. 将核对所依据的工件快照持久化到 --snapshot-dir（含 TTL manifest），支持历史
   重放审计，杜绝验过即焚/即丢。

用法:
  python verify-evidence.py --report-in <report.json> \
      [--repo-dir <dir>] [--snapshot-dir <dir>] [--report-out <verified.json>]

退出码: 0=全部引用核对通过 | 1=至少一条引用作废（verdict insufficient）
        | 2=infra/配置错误（fail-closed）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SNAPSHOT_ROOT = os.path.join(HERE, "snapshots")
DEFAULT_TTL_DAYS = 90


def err(msg):
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)


def die(code, msg):
    err(msg)
    sys.exit(code)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_head_sha(repo_dir):
    """动态获取仓库 HEAD SHA；失败/非仓库返回 None（fail-closed 由调用方处理）。"""
    try:
        out = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        return out if len(out) == 40 else None
    except Exception:  # noqa: BLE001
        return None


def load_report(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        die(2, f"报告不可读或 JSON 非法 {path}: {e}")


def safe_relpath(path, repo_dir):
    """保证 citation 的 file 在 repo-dir 内，防止 .. 路径穿越。"""
    base = os.path.abspath(repo_dir)
    target = os.path.normpath(os.path.join(base, path))
    if not target.startswith(base + os.sep) and target != base:
        return None
    return target


def read_lines(path, line_start, line_end):
    """读取文件指定行范围（1-based，闭区间）。返回 (lines_text, 是否成功, 错误信息)。"""
    if not os.path.isfile(path):
        return "", False, "文件缺失"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:  # noqa: BLE001
        return "", False, f"文件不可读: {e}"
    total = len(lines)
    if line_start < 1 or line_end < line_start:
        return "", False, f"行号非法: {line_start}-{line_end}"
    # 允许 line_end 超出实际行数——按 EOF 截断，但标记为可能不完整
    end = min(line_end, total)
    selected = lines[line_start - 1 : end]
    text = "".join(selected)
    if end < line_end:
        return text, False, f"行范围超出文件尾（实际 {total} 行）"
    return text, True, ""


def verify_citation(cit, repo_dir):
    """核对单条 citation。返回带 _status/_reason/_matched 的字典。"""
    cid = cit.get("id") or "(无 id)"
    rel = cit.get("file")
    line_start = cit.get("line_start")
    line_end = cit.get("line_end")
    exact = cit.get("exact_string")

    result = dict(cit)
    result["_checked_at"] = now_iso()

    if not rel or not isinstance(rel, str):
        result["_status"] = "void"
        result["_reason"] = "citation 缺少 file 字段"
        return result
    if not isinstance(line_start, int) or not isinstance(line_end, int):
        result["_status"] = "void"
        result["_reason"] = "line_start/line_end 非整数"
        return result
    if exact is None:
        result["_status"] = "void"
        result["_reason"] = "citation 缺少 exact_string"
        return result

    abs_path = safe_relpath(rel, repo_dir)
    if abs_path is None:
        result["_status"] = "void"
        result["_reason"] = "file 路径穿越或不合法"
        return result

    text, ok, reason = read_lines(abs_path, line_start, line_end)
    if not ok:
        result["_status"] = "void"
        result["_reason"] = reason
        return result

    matched = exact in text
    result["_status"] = "valid" if matched else "void"
    result["_reason"] = "命中" if matched else "精确字符串未在指定行范围内命中"
    result["_matched"] = matched
    return result


def snapshot_artifacts(snap_dir, citations, repo_dir):
    """把核对用到的工件快照到 snap_dir，返回 manifest。"""
    files_copied = []
    seen = set()
    for cit in citations:
        rel = cit.get("file")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        abs_path = safe_relpath(rel, repo_dir)
        if abs_path is None or not os.path.isfile(abs_path):
            continue
        dst = os.path.join(snap_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(abs_path, dst)
        files_copied.append(rel)
    return files_copied


def build_manifest(run_id, baseline, snap_dir, citations, ttl_days):
    created = now_iso()
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": "evidence-snapshot-manifest/v1",
        "run_id": run_id,
        "created_at": created,
        "ttl_days": ttl_days,
        "expires_at": expires,
        "baseline_sha": baseline.get("sha"),
        "fetched_at": baseline.get("fetched_at"),
        "snapshot_dir": os.path.abspath(snap_dir),
        "citation_files": sorted({c.get("file") for c in citations if c.get("file")}),
    }


def verify(report_in, repo_dir, snapshot_dir, report_out, run_id, ttl_days):
    repo_dir = os.path.abspath(repo_dir or os.getcwd())
    report = load_report(report_in)

    # 动态获取基准（AC-9：基准版本在 run 开始时动态获取并写入报告）
    sha = git_head_sha(repo_dir)
    fetched_at = now_iso()
    if not sha:
        die(2, f"无法获取仓库 HEAD SHA（repo_dir={repo_dir}），基准为空时 fail-closed")
    baseline = {"sha": sha, "fetched_at": fetched_at, "repo_dir": repo_dir}

    # run_id 缺省 = sha 前缀 + 时间戳
    if not run_id:
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{sha[:8]}-{ts}"

    citations = report.get("citations") or []
    if not isinstance(citations, list):
        die(2, "报告 citations 字段不是数组")

    snap_dir = snapshot_dir or os.path.join(DEFAULT_SNAPSHOT_ROOT, run_id)
    os.makedirs(snap_dir, exist_ok=True)

    # 先快照再核对——确保核对依据可被历史重放
    copied = snapshot_artifacts(snap_dir, citations, repo_dir)

    verified = []
    voided = []
    for cit in citations:
        v = verify_citation(cit, repo_dir)
        verified.append(v)
        if v["_status"] == "void":
            voided.append(v.get("id") or "(无 id)")

    original_verdict = report.get("verdict")
    # AC-2 / AC-9：任一引用作废 → verdict 强制 insufficient
    verdict = "insufficient" if voided else (original_verdict or "insufficient")
    blocking = verdict == "insufficient"

    manifest = build_manifest(run_id, baseline, snap_dir, citations, ttl_days)
    manifest_path = os.path.join(snap_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    out = {
        "schema": "verified-report/v1",
        "run_id": run_id,
        "baseline": baseline,
        "original_verdict": original_verdict,
        "verdict": verdict,
        "blocking": blocking,
        "voided": voided,
        "citation_count": len(verified),
        "valid_count": len(verified) - len(voided),
        "void_count": len(voided),
        "citations": verified,
        "snapshot_dir": os.path.abspath(snap_dir),
        "snapshot_manifest": manifest_path,
    }

    if report_out:
        with open(report_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

    return out


def print_summary(out):
    print("== 证据引用机械核对（ADR-0067 / AC-9）==")
    print(f"baseline: {out['baseline']['sha']} @ {out['baseline']['fetched_at']}")
    print(f"citations: {out['citation_count']} 条，valid={out['valid_count']}，void={out['void_count']}")
    print(f"verdict: {out['original_verdict']} → {out['verdict']}（blocking={out['blocking']}）")
    for c in out["citations"]:
        mark = "✓" if c["_status"] == "valid" else "✗"
        print(f"  {mark} [{c.get('id')}] {c.get('file')}:{c.get('line_start')}-{c.get('line_end')} — {c['_reason']}")
    if out["voided"]:
        print(f"作废引用: {', '.join(out['voided'])} —— verdict 强制 insufficient")
    print(f"快照: {out['snapshot_dir']}")


def main():
    ap = argparse.ArgumentParser(prog="verify-evidence.py", description="红队/verifier 证据引用机械核对")
    ap.add_argument("--report-in", required=True, help="含 citations 的报告 JSON")
    ap.add_argument("--repo-dir", default=None, help="已检出仓库根目录（默认当前目录）")
    ap.add_argument("--snapshot-dir", default=None, help="快照输出目录（默认 snapshots/<run_id>/）")
    ap.add_argument("--report-out", default=None, help="核对后报告输出路径")
    ap.add_argument("--run-id", default=None, help="run id（默认由 SHA+时间戳生成）")
    ap.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS, help=f"快照保留天数（默认 {DEFAULT_TTL_DAYS}）")
    a = ap.parse_args()

    out = verify(a.report_in, a.repo_dir, a.snapshot_dir, a.report_out, a.run_id, a.ttl_days)
    print_summary(out)
    sys.exit(1 if out["voided"] else 0)


if __name__ == "__main__":
    main()
