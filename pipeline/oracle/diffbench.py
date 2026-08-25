#!/usr/bin/env python3
"""差分对拍消费器（gate 侧 · Cloudbird-Software IR-0004 rev6 · AC-12）。

PM 优先范式：oracle 制作可选、消费必须——champion/oracle 输出缺失、为空或超时，
一律 exit 1 显式失败，绝不静默停摆；硬区分歧 exit 2，强制二选一路由裁决。

用法：
  python oracle/diffbench.py --registry <yaml> --champion-out <file> --oracle-out <file> \
      --zones [--ledger <json>] [--timeout <sec>]

对拍输入：JSONL，每行 {"case": "<id>", "output": <任意 JSON 值>}；
非 JSONL 文本按整文件单用例回退比对。案例 id 依注册表 hard_zone/soft_zone glob
归区（fnmatch，区分大小写）。

退出码：0=等价或仅软区分歧（软区输出警告清单）；
       1=注册表非法 / 对拍未运行 / 输入缺失 / 超时 / zones 模式下无任何案例归区；
       2=硬区分歧（错误信息含裁决路由指令）。
输出：对拍 JSON 台账 {date, entries, verdicts}（stdout；指定 --ledger 时另存文件）。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
from pathlib import Path

try:
    from .registry import load_registry, now_iso, validate_registry
except ImportError:  # 以脚本方式直接运行
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(_here))
    sys.path.insert(0, _here)
    from registry import load_registry, now_iso, validate_registry

HARD_ZONE_MARKER = "硬区分歧：缺陷修复或契约修订二选一路径路由裁决"
HARD_ZONE_DETAIL = ("路由裁决：缺陷修复 或 契约修订（二选一路径裁决 / 二选一路由裁决），"
                    "不得双修、不得跳过、不得静默。")
WHOLE_CASE = "__whole_file__"


def read_text(path):
    try:
        return path.read_text(encoding="utf-8"), None
    except OSError as exc:
        return None, "读取失败 %s: %s" % (path, exc)


def parse_cases(text):
    """JSONL（每行 {case, output}）→ 用例映射；否则回退整文件单用例。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    cases = {}
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            return {WHOLE_CASE: text.rstrip("\n")}
        if not isinstance(obj, dict) or "case" not in obj:
            return {WHOLE_CASE: text.rstrip("\n")}
        cases[obj["case"]] = obj.get("output")
    if cases:
        return cases
    return {WHOLE_CASE: text.rstrip("\n")}


def classify(case_id, entries):
    """按注册表所有条目的 hard_zone/soft_zone glob 归区；硬区优先。"""
    for entry in entries:
        for g in entry.get("hard_zone") or []:
            if fnmatch.fnmatchcase(case_id, g):
                return "hard", g
    for entry in entries:
        for g in entry.get("soft_zone") or []:
            if fnmatch.fnmatchcase(case_id, g):
                return "soft", g
    return None, None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="diffbench.py", description="差分对拍消费器（AC-12）")
    parser.add_argument("--registry", required=True, help="注册表 YAML")
    parser.add_argument("--champion-out", required=True, help="champion 输出文件")
    parser.add_argument("--oracle-out", required=True, help="oracle 输出文件")
    parser.add_argument("--zones", action="store_true", help="启用硬/软区 glob 裁决（按注册表）")
    parser.add_argument("--ledger", help="对拍 JSON 台账输出路径（{date, entries, verdicts}）")
    parser.add_argument("--timeout", type=int, default=300, help="对拍时限秒数（超出 exit 1）")
    args = parser.parse_args(argv)

    t0 = time.monotonic()
    if args.timeout <= 0:
        print("diffbench: --timeout 必须为正整数", file=sys.stderr)
        return 1

    data, err = load_registry(args.registry)
    if err:
        print("diffbench: %s" % err, file=sys.stderr)
        return 1
    errors = validate_registry(data)
    if errors:
        for e in errors:
            print("diffbench: 注册表非法 %s" % e, file=sys.stderr)
        return 1
    entries_meta = data["entries"]

    champ_path, oracle_path = Path(args.champion_out), Path(args.oracle_out)
    champ_text, err = read_text(champ_path)
    if err is None and not champ_text.strip():
        err = "champion 输出为空（对拍未运行）"
    if err:
        print("diffbench: 对拍未运行或输入缺失 %s: %s" % (champ_path, err), file=sys.stderr)
        return 1
    oracle_text, err = read_text(oracle_path)
    if err is None and not oracle_text.strip():
        err = "oracle 输出为空（对拍未运行；oracle 未制作不是静默放行的理由）"
    if err:
        print("diffbench: 对拍未运行或输入缺失 %s: %s" % (oracle_path, err), file=sys.stderr)
        return 1

    champion = parse_cases(champ_text)
    oracle = parse_cases(oracle_text)

    records = []
    hard_details = []
    soft_warnings = []
    hard_div = False
    soft_div = False
    classified = 0
    for case_id in sorted(set(champion) | set(oracle)):
        if args.zones:
            zone, glob = classify(case_id, entries_meta)
            if zone is None:
                soft_warnings.append("案例 %s 未匹配任何已登记区（不参与裁决，仅登记警告）" % case_id)
                records.append({"case": case_id, "zone": "unclassified", "glob": None, "verdict": "skipped"})
                continue
            classified += 1
        else:
            zone, glob = "hard", None  # 未启用分区：整文件按硬区口径比对
        in_c, in_o = case_id in champion, case_id in oracle
        if in_c and in_o:
            verdict = "equal" if champion[case_id] == oracle[case_id] else "divergent"
        elif in_c:
            verdict = "missing-in-oracle"
        else:
            verdict = "missing-in-champion"
        records.append({"case": case_id, "zone": zone, "glob": glob, "verdict": verdict})
        if verdict != "equal":
            if zone == "hard":
                hard_div = True
                hard_details.append("case=%s verdict=%s glob=%s" % (case_id, verdict, glob))
            else:
                soft_div = True
                soft_warnings.append("软区分歧：case=%s verdict=%s glob=%s" % (case_id, verdict, glob))

    if args.zones and classified == 0:
        print("diffbench: zones 模式下没有任何案例匹配硬/软区（对拍未有效运行，不得静默停摆）",
              file=sys.stderr)
        return 1

    elapsed = time.monotonic() - t0
    if elapsed > args.timeout:
        print("diffbench: 对拍超时（%.1fs > timeout=%ds），不得静默停摆" % (elapsed, args.timeout),
              file=sys.stderr)
        return 1

    overall = "hard_divergence" if hard_div else ("soft_divergence" if soft_div else "equivalent")
    exit_code = 2 if hard_div else 0
    ledger = {
        "date": now_iso(),
        "entries": records,
        "verdicts": {
            "overall": overall,
            "hard_zone": "divergent" if hard_div else "clean",
            "soft_zone": "divergent" if soft_div else "clean",
            "exit_code": exit_code,
        },
        "warnings": soft_warnings,
        "duration_ms": int(elapsed * 1000),
    }
    text = json.dumps(ledger, ensure_ascii=False, indent=2)
    print(text)
    if args.ledger:
        try:
            with open(args.ledger, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text + "\n")
        except OSError as exc:
            print("diffbench: 台账写入失败 %s" % exc, file=sys.stderr)
            return 1
    if hard_div:
        print(HARD_ZONE_MARKER, file=sys.stderr)
        print(HARD_ZONE_DETAIL, file=sys.stderr)
        for d in hard_details:
            print("diffbench: %s" % d, file=sys.stderr)
        return 2
    for w in soft_warnings:
        print("diffbench[软区警告]: %s" % w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
