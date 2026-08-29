#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""precision.py —— post-fix precision 基准管线（W2-C4 .github#217 / ADR-0063 决策 4）

方法学署名：post-fix 基准（bot 建议被事后人工修复命中的比例 = precision）源自
withmartian/code-review-benchmark（MIT，方法学 fork 自建管线）；评审执行本体 =
alibaba/open-code-review（Apache-2.0）。晋升判据（ADR-0063 / 卡 AC-3）：
precision ≥ 0.8 且累积 ≥ 30 例——本脚本只产数与判定，不授权晋升（veto 晋升另走 ADR）。

判定语义（与 evalsets/ocr-shadow README 口径一致，archive 仓）：
  分母 evaluated —— 有观察窗内后续 commit 的建议（无后续 commit = pending，不入分母；
                   后续全为 bot 的建议保守计 miss——对晋升阈值取 fail-closed 方向）
  命中 hit      —— 某后续非 bot commit 的 diff 触及建议锚定文件，且变更行区间与
                   建议 [start_line, end_line] 相交（±line-tolerance 吸收行漂移；
                   后续 diff 须以建议基线（PR head/merge）为参照生成——README 数据格式约定）
  污染防御      —— 只认非 bot 修复 commit（ADR-0063：人工照抄 bot 修复 = 假命中）
  时序          —— 按建议 ts 的自然月聚合 + 累积曲线（月度时序即「precision 时序」）

零网络运行（--followups fixture 目录）；--api-repo 模式经 gh api 拉历史 PR 的
后续 commit diff（测试不触碰该路径）。
用法：
  python3 precision.py --records F.jsonl [F2...] --followups DIR --out F
                       [--window-days 14] [--line-tolerance 3]
  python3 precision.py --records F.jsonl --api-repo ORG/REPO --out F ...   # 在线采集
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

PRECISION_MIN = 0.8
EXAMPLES_MIN = 30
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
BOT_SUFFIX = "[bot]"
KNOWN_BOTS = ("github-actions", "dependabot", "coderabbit", "renovate", "zizmor")


def is_bot(author: str) -> bool:
    """bot 判定：登录名后缀 [bot] 或已知 bot 账号前缀（命中判定只认非 bot——ADR-0063）。"""
    a = (author or "").lower()
    return a.endswith(BOT_SUFFIX) or a.startswith(KNOWN_BOTS)


def diff_touched_lines(diff_text: str) -> dict[str, set[int]]:
    """解析 unified diff → {文件: 变更行号集合}（'+' 取新侧、'-' 取旧侧行号——
    删除坏行也是修复，旧侧行号即建议基线坐标）。"""
    files: dict[str, set[int]] = {}
    path = None
    old_no = new_no = 0
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            path = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            if path:
                files.setdefault(path, set())
            in_hunk = False
            continue
        if line.startswith("@@"):
            m = HUNK_RE.match(line)
            if not m:
                raise ValueError(f"不可解析的 hunk 头: {line!r}")
            if path is None:
                # 删除文件（+++ /dev/null）后的 hunk 头：b 侧不存在——跳过
                # （此前误与"不可解析"合并 raise：纯删除 diff 必炸，PR #131
                # 实测；postprocess.py 同款修复）
                in_hunk = False
                continue
            old_no, new_no = int(m.group(1)), int(m.group(3))
            in_hunk = True
            continue
        if in_hunk and path is not None:
            if line.startswith("+"):
                files[path].add(new_no)
                new_no += 1
            elif line.startswith("-"):
                files[path].add(old_no)
                old_no += 1
            elif line.startswith(" "):
                old_no += 1
                new_no += 1
    return files


def suggestion_id(rec: dict) -> str:
    s = rec["suggestion"]
    raw = f"{rec['repo']}#{rec['pr']}@{rec['head_sha']}:{s['path']}:{s['start_line']}:{s['end_line']}:{s['content']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_records(paths: list[str]) -> list[dict]:
    """装载 JSONL：仅取 record=suggestion 且 schema 匹配的行（summary 行忽略）。"""
    out = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{p}:{ln} JSONL 损坏（fail-closed）: {e}") from e
                if rec.get("schema") == "ocr-shadow/v1" and rec.get("record") == "suggestion":
                    out.append(rec)
    return out


def load_followups_dir(d: str) -> dict[int, list[dict]]:
    """fixture/离线模式：DIR/pr-<N>.json → {"pr": N, "commits": [{sha, author,
    committed_at, diff}]}（diff 为以建议基线为参照的 unified diff 文本）。"""
    out: dict[int, list[dict]] = {}
    for name in sorted(os.listdir(d)):
        if not (name.startswith("pr-") and name.endswith(".json")):
            continue
        with open(os.path.join(d, name), encoding="utf-8") as f:
            doc = json.load(f)
        out[int(doc["pr"])] = doc.get("commits") or []
    return out


def harvest_followups_api(repo: str, records: list[dict], window_days: int) -> dict[int, list[dict]]:
    """在线采集（gh api；测试零网络不触及）：对每个 PR 取 merge 后 window 内的
    默认分支 commit（跳过 bot），逐个拉 commit diff。"""
    def gh(*args: str) -> str:
        r = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=True)
        return r.stdout

    followups: dict[int, list[dict]] = {}
    for pr in sorted({rec["pr"] for rec in records}):
        pulls = json.loads(gh(f"repos/{repo}/pulls/{pr}"))
        merged_at = pulls.get("merged_at")
        if not merged_at:
            continue  # 未合并 → 观察窗未开启，pending
        t0 = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        t1 = t0 + timedelta(days=window_days)
        since, until = t0.strftime("%Y-%m-%dT%H:%M:%SZ"), t1.strftime("%Y-%m-%dT%H:%M:%SZ")
        commits = []
        for c in json.loads(gh(f"repos/{repo}/commits?since={since}&until={until}&per_page=100")):
            author = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
            if is_bot(author):
                continue
            detail = json.loads(gh(f"repos/{repo}/commits/{c['sha']}"))
            diff = "\n".join(f.get("patch") or "" for f in detail.get("files") or [])
            if diff:
                commits.append({"sha": c["sha"], "author": author,
                                "committed_at": c["commit"]["committer"]["date"], "diff": diff})
        followups[int(pr)] = commits
    return followups


def evaluate(records: list[dict], followups: dict[int, list[dict]], line_tol: int) -> dict:
    """核心判定（纯函数，测试直接调用）：返回 precision 报告 dict。"""
    per, hits, evaluated, pending = [], 0, 0, 0
    for rec in records:
        s = rec["suggestion"]
        anchor = set(range(s["start_line"] - line_tol, s["end_line"] + 1 + line_tol))
        hit_sha = None
        commits = followups.get(int(rec["pr"])) or []
        if not commits:
            pending += 1
        else:
            evaluated += 1
            for c in commits:
                if c.get("is_bot") or is_bot(c.get("author", "")):
                    continue  # 污染防御：bot commit 不算修复命中
                touched = diff_touched_lines(c["diff"])
                lines = touched.get(s["path"])
                if lines and lines & anchor:
                    hit_sha = c["sha"]
                    break
            if hit_sha:
                hits += 1
        per.append({"id": suggestion_id(rec), "pr": rec["pr"], "path": s["path"],
                    "start_line": s["start_line"], "end_line": s["end_line"],
                    "ts": rec["ts"], "evaluated": bool(commits),
                    "matched": hit_sha is not None, "hit_commit": hit_sha})
    precision = round(hits / evaluated, 4) if evaluated else None
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "methodology": "post-fix precision（withmartian/code-review-benchmark 方法学 MIT fork；"
                       "评审执行=alibaba/open-code-review Apache-2.0；ADR-0063 决策 4）",
        "thresholds": {"precision_min": PRECISION_MIN, "examples_min": EXAMPLES_MIN},
        "evaluated": evaluated, "pending_observation": pending, "hits": hits,
        "precision": precision,
        "promotion_ready": precision is not None and precision >= PRECISION_MIN and evaluated >= EXAMPLES_MIN,
        "series": _series(per),
        "per_suggestion": per,
    }


def _series(per: list[dict]) -> list[dict]:
    """按月时序 + 累积曲线（AC-3 precision 时序）。分母口径与总 precision 一致：
    仅计 evaluated（pending 不入月度/累积分母）。"""
    months = sorted({p["ts"][:7] for p in per}) or []
    out, cum_e, cum_h = [], 0, 0
    for m in months:
        rows = [p for p in per if p["ts"].startswith(m) and p["evaluated"]]
        e, h = len(rows), sum(1 for p in rows if p["matched"])
        cum_e, cum_h = cum_e + e, cum_h + h
        out.append({"period": m, "evaluated": e, "hits": h,
                    "precision": round(h / e, 4) if e else None,
                    "cumulative_evaluated": cum_e, "cumulative_hits": cum_h,
                    "cumulative_precision": round(cum_h / cum_e, 4) if cum_e else None})
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="post-fix precision 基准（ADR-0063 决策 4）")
    ap.add_argument("--records", nargs="+", required=True, help="shadow JSONL（可多个）")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--followups", help="目录：pr-<N>.json 后续 commit fixtures")
    grp.add_argument("--api-repo", help="ORG/REPO——经 gh api 在线采集后续 commit")
    ap.add_argument("--out", required=True)
    ap.add_argument("--window-days", type=int, default=14, help="merge 后观察窗（天）")
    ap.add_argument("--line-tolerance", type=int, default=3, help="行漂移容差")
    args = ap.parse_args(argv)

    try:
        records = load_records(args.records)
        if args.followups:
            followups = load_followups_dir(args.followups)
        else:
            followups = harvest_followups_api(args.api_repo, records, args.window_days)
        report = evaluate(records, followups, args.line_tolerance)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
        print(f"::error::precision 管线输入/采集失败（fail-closed）: {e}", file=sys.stderr)
        return 2

    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"precision={report['precision']} hits={report['hits']}/{report['evaluated']}"
          f"（pending={report['pending_observation']}） promotion_ready={report['promotion_ready']}"
          f" 阈值: precision≥{PRECISION_MIN} 且 ≥{EXAMPLES_MIN} 例（ADR-0063 决策 4；本脚本不授权晋升）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
