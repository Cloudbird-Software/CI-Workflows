#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""postprocess.py —— OCR shadow 建议确定性后处理（W2-C4 .github#217 / ADR-0063 决策 3）

宪法 §4C「评审输出统一经确定性后处理」+ §4E「过滤率本身是指标」的可执行件。
LLM/OCR 只产建议；是否计入 shadow 记录由本脚本确定性判定，三重过滤
（顺序即 ADR 决策 3 的表述顺序，统计口径与之绑定）：
  1. in-diff    —— file:line 锚点必须落在 PR diff 的新增行内（文件在 diff 中且
                   [start_line, end_line] 与该文件新增行集合相交），否则丢 outside-diff
  2. rule-hit   —— 建议文本必须命中 rules.yaml 声明的可疑模式类别，否则丢 no-rule-hit
  3. dedup      —— (path, start_line, 首命中 rule_id) 重复者丢弃计 duplicate
丢一计数一：total / kept / dropped_by_reason{outside-diff, no-rule-hit, duplicate}
+ drop_rate。不过滤率（kept/total）本身是指标——评审器质量退化在后处理层可见。

fail-closed 边界：输入文件缺失/JSON 损坏/diff hunk 头不可解析 = 工具完整性问题，
exit 2（shadow 不阻断合并的前提是 check 非 required——infra 错误保持可见）；
纯"建议被过滤"一律 exit 0（过滤是指标不是门禁）。

用法（stdlib + PyYAML（runner 预装，spec-check.py 同款），零网络零推理）：
  python3 postprocess.py --ocr-json F --diff F --rules F \
                         --out kept.json --stats-file stats.json [--ocr-rc N]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys

DROP_OUTSIDE_DIFF = "outside-diff"
DROP_NO_RULE = "no-rule-hit"
DROP_DUP = "duplicate"
HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(text: str) -> dict[str, set[int]]:
    """解析 unified diff → {文件路径: 新增行号集合}（新侧行号，1 起）。

    只统计 '+' 行；'--- a/x' 与 '+++ b/x' 取 b 侧路径，'/dev/null'（删除文件）
    跳过——删除文件无新增行，任何锚点都应落 outside-diff。hunk 头不可解析
    抛 ValueError（fail-closed：静默跳过 hunk = 把越界建议放进分母）。
    """
    files: dict[str, set[int]] = {}
    path = None
    new_no = 0
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p == "/dev/null":
                path, in_hunk = None, False
                continue
            path = p[2:] if p.startswith("b/") else p
            files.setdefault(path, set())
            in_hunk = False
            continue
        if line.startswith("@@"):
            m = HUNK_RE.match(line)
            if not m or path is None:
                raise ValueError(f"不可解析的 hunk 头: {line!r}")
            new_no = int(m.group(2))
            in_hunk = True
            continue
        if in_hunk and path is not None:
            if line.startswith("+"):
                files[path].add(new_no)
                new_no += 1
            elif line.startswith(" "):
                new_no += 1
            elif line.startswith("-"):
                pass
            # '\'（无换行标记）不推进行号
    return files


def load_rules(path: str) -> list[dict]:
    """装载规则表：编译 content_regex，展开 paths 缺省值。"""
    import yaml  # runner 预装 PyYAML（spec-check.py 同款约定）

    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict) or doc.get("schema") != "ocr-shadow-rules/v1":
        raise ValueError("rules.yaml schema 头须为 ocr-shadow-rules/v1")
    rules = []
    for r in doc.get("rules") or []:
        pats = r.get("content_regex") or []
        if not pats:
            raise ValueError(f"规则 {r.get('id')!r} 缺 content_regex")
        rules.append({
            "id": str(r["id"]),
            "regex": [re.compile(p, re.IGNORECASE) for p in pats],
            "paths": r.get("paths") or ["*"],
        })
    if not rules:
        raise ValueError("rules.yaml 无规则（空规则集=全量丢弃，须显式声明而非缺省）")
    return rules


def match_rule(rules: list[dict], path: str, content: str) -> str | None:
    """按规则表顺序返回首个命中规则 id（确定性：先声明先匹配）。"""
    for r in rules:
        if not any(fnmatch.fnmatch(path, g) for g in r["paths"]):
            continue
        if any(rx.search(content) for rx in r["regex"]):
            return r["id"]
    return None


def postprocess(ocr_doc: dict, added: dict[str, set[int]], rules: list[dict]) -> tuple[list[dict], dict]:
    """三重过滤主逻辑。返回 (保留建议列表, 统计)。"""
    comments = ocr_doc.get("comments") or []
    kept, seen = [], set()
    dropped = {DROP_OUTSIDE_DIFF: 0, DROP_NO_RULE: 0, DROP_DUP: 0}
    for c in comments:
        path = str(c.get("path") or "")
        start = c.get("start_line")
        end = c.get("end_line", start)
        content = str(c.get("content") or "")
        if not path or not isinstance(start, int):
            dropped[DROP_OUTSIDE_DIFF] += 1  # 无锚点建议与越界同桶（无法定位=不可验证）
            continue
        if not isinstance(end, int) or end < start:
            end = start
        anchor = set(range(start, end + 1))
        if path not in added or not (anchor & added[path]):
            dropped[DROP_OUTSIDE_DIFF] += 1
            continue
        rule_id = match_rule(rules, path, content)
        if rule_id is None:
            dropped[DROP_NO_RULE] += 1
            continue
        key = (path, start, rule_id)
        if key in seen:
            dropped[DROP_DUP] += 1
            continue
        seen.add(key)
        kept.append({"path": path, "start_line": start, "end_line": end,
                     "content": content, "rule_id": rule_id,
                     "existing_code": str(c.get("existing_code") or ""),
                     "suggestion_code": str(c.get("suggestion_code") or "")})
    total = len(comments)
    stats = {
        "ocr_status": str(ocr_doc.get("status") or "unknown"),
        "ocr_message": str(ocr_doc.get("message") or ""),
        "total": total,
        "kept": len(kept),
        "dropped_by_reason": dropped,
        "drop_rate": round(sum(dropped.values()) / total, 4) if total else 0.0,
    }
    return kept, stats


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="OCR shadow 确定性后处理（ADR-0063 决策 3）")
    ap.add_argument("--ocr-json", required=True, help="ocr review --format json 输出文件")
    ap.add_argument("--diff", required=True, help="PR unified diff 文件")
    ap.add_argument("--rules", required=True, help="rules.yaml 路径")
    ap.add_argument("--out", required=True, help="保留建议 JSON 输出")
    ap.add_argument("--stats-file", required=True, help="过滤统计 JSON 输出（指标本体）")
    ap.add_argument("--ocr-rc", type=int, default=0, help="ocr 进程退出码（透传入 stats 供诚实审计）")
    args = ap.parse_args(argv)

    try:
        with open(args.ocr_json, encoding="utf-8") as f:
            ocr_doc = json.load(f)
        if not isinstance(ocr_doc, dict):
            raise ValueError("ocr-json 顶层须为对象")
        with open(args.diff, encoding="utf-8") as f:
            added = parse_unified_diff(f.read())
        rules = load_rules(args.rules)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"::error::输入不可用（fail-closed，工具完整性问题）: {e}", file=sys.stderr)
        return 2

    kept, stats = postprocess(ocr_doc, added, rules)
    stats["ocr_exit_code"] = args.ocr_rc
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(kept, f, ensure_ascii=False, indent=1)
    with open(args.stats_file, "w", encoding="utf-8", newline="\n") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)

    print(f"ocr_status={stats['ocr_status']} total={stats['total']} kept={stats['kept']}"
          f" dropped(outside-diff/no-rule-hit/duplicate)={stats['dropped_by_reason'][DROP_OUTSIDE_DIFF]}"
          f"/{stats['dropped_by_reason'][DROP_NO_RULE]}/{stats['dropped_by_reason'][DROP_DUP]}"
          f" drop_rate={stats['drop_rate']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
