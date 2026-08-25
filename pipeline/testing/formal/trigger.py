#!/usr/bin/env python3
"""trigger —— 形式化条件触发判定（IR-0004 AC-7，机械优先 + fail-closed）。

对 card/spec 元数据 YAML 逐项评估 checklist.yaml：
  - 机械字段（source 存在且 rule 可判）→ matched true/false + 证据值；
  - 源字段缺失 → evaluated=false（机械不可判，不算命中）；
  - semantic_fields（human/LLM 填充位）→ 只留痕（filled/pending + 值）不参与自动判定。

final 判定（机械，fail-closed）：
  1. risk_level 缺失           → needs_risk_level（退出码 3）
  2. 任一 positive 命中        → applicable
  3. 任一 negative 命中        → not_applicable
  4. 否则（无正证据）           → not_applicable（理由：无正适用证据）

CLI：
  python trigger.py --meta card.yaml [--out verdict.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:  # 双模式导入：包内 / 独立脚本
    from pipeline.testing import _yamlmini
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pipeline.testing import _yamlmini

DEFAULT_CHECKLIST = Path(__file__).resolve().parent / "checklist.yaml"


def get_field(meta, dotted):
    node = meta
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _plain_eq(a, b):
    if isinstance(b, bool) or isinstance(a, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def eval_rule(rule, value):
    """规则求值；返回 (matched, reason)。规则算子：equals/not_equals/in/lte/lt/gte/gt/exists。"""
    if not isinstance(rule, dict) or len(rule) != 1:
        raise ValueError("rule 必须是单算子映射: %r" % (rule,))
    op, expected = next(iter(rule.items()))
    if op == "equals":
        return _plain_eq(value, expected), "%r == %r" % (value, expected)
    if op == "not_equals":
        return not _plain_eq(value, expected), "%r != %r" % (value, expected)
    if op == "in":
        return value in expected, "%r in %r" % (value, expected)
    if op in ("lte", "lt", "gte", "gt"):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False, "字段非数值，%s 不可判（%r）" % (op, value)
        table = {
            "lte": lambda: value <= expected,
            "lt": lambda: value < expected,
            "gte": lambda: value >= expected,
            "gt": lambda: value > expected,
        }
        ok = table[op]()
        return ok, "%r %s %r" % (value, op, expected)
    if op == "exists":
        return bool(value) == bool(expected), "exists(%r)==%r" % (value, expected)
    raise ValueError("未知规则算子: %r" % op)


def evaluate_item(meta, item):
    value, present = get_field(meta, item["source"])
    result = {
        "id": item["id"],
        "kind": item["kind"],
        "label": item.get("label", ""),
        "source": item["source"],
        "field_value": value,
        "evaluated": present,
        "matched": False,
        "reason": "",
    }
    if not present:
        result["reason"] = "源字段缺失（机械不可判，不命中）"
        return result
    matched, reason = eval_rule(item["rule"], value)
    result["matched"] = matched
    result["reason"] = reason
    if matched and item.get("extra_conditions"):
        for cond in item["extra_conditions"]:
            cvalue, cpresent = get_field(meta, cond["source"])
            if not cpresent:
                result["matched"] = False
                result["reason"] += "; 附加条件字段缺失: %s" % cond["source"]
                return result
            cmatch, creason = eval_rule(cond["rule"], cvalue)
            result["reason"] += "; 附加条件 %s -> %s" % (cond["source"], creason)
            if not cmatch:
                result["matched"] = False
                return result
    return result


def build_fill_trace(meta, items):
    trace = []
    for item in items:
        for slot in item.get("semantic_fields", []) or []:
            value, present = get_field(meta, slot["field"])
            trace.append(
                {
                    "item": item["id"],
                    "field": slot["field"],
                    "filler": slot.get("filler", "human"),
                    "value": value,
                    "status": "filled" if present else "pending",
                }
            )
    return trace


def judge(meta, checklist_path=DEFAULT_CHECKLIST):
    doc = _yamlmini.load(str(checklist_path))
    items = doc.get("items") or []
    if not items:
        raise ValueError("checklist 无 items: %s" % checklist_path)
    results = [evaluate_item(meta, item) for item in items]
    fill_trace = build_fill_trace(meta, items)
    risk_field = (doc.get("risk_gate") or {}).get("field", "risk_level")
    risk_level, risk_present = get_field(meta, risk_field)

    positives = [r for r in results if r["kind"] == "positive" and r["matched"]]
    negatives = [r for r in results if r["kind"] == "negative" and r["matched"]]

    if not risk_present:
        final, reason = "needs_risk_level", (
            "fail-closed：元数据缺少 %s（机械字段），无法完成风险门（AD 不允许默认放行）" % risk_field
        )
    elif positives:
        final = "applicable"
        reason = "正适用项命中: %s" % ", ".join(r["id"] for r in positives)
    elif negatives:
        final = "not_applicable"
        reason = "反适用项命中且无正适用项: %s" % ", ".join(r["id"] for r in negatives)
    else:
        final = "not_applicable"
        reason = "无正适用证据（机械字段全部未命中且无反适用项命中）"
    return {
        "schema": "cloudbird/formal-trigger/1",
        "checklist": str(checklist_path),
        "meta_id": meta.get("id", meta.get("name", "unknown")),
        "risk_level": risk_level if risk_present else None,
        "items": results,
        "fill_trace": fill_trace,
        "final": final,
        "final_reason": reason,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="形式化条件触发判定（AC-7）")
    parser.add_argument("--meta", required=True, help="card/spec 元数据 YAML")
    parser.add_argument("--checklist", default=str(DEFAULT_CHECKLIST))
    parser.add_argument("--out", help="判定 JSON 输出路径")
    args = parser.parse_args(argv)

    try:
        meta = _yamlmini.load(args.meta)
        verdict = judge(meta, checklist_path=args.checklist)
    except (OSError, ValueError) as exc:
        print("FATAL: %s" % exc, file=sys.stderr)
        return 2

    payload = json.dumps(verdict, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8", newline="\n")
    print(payload, end="")
    exit_code = {"applicable": 0, "not_applicable": 0, "needs_risk_level": 3}[verdict["final"]]
    print("FINAL: %s（%s）" % (verdict["final"], verdict["final_reason"]), file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
