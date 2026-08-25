#!/usr/bin/env python3
"""relations —— 蜕变关系可执行检查（IR-0004 AC-4，机械判定）。

catalog.yaml 中 status: implemented 的关系在此实现为可执行检查：
  MR-001 输入重排 → 结果集等价（多重集等价 + 重排前置校验）
  MR-002 重试 → 幂等（深相等）
  MR-003 单元拆分 → 总量守恒（数值叶子和守恒）

case 文件格式（JSON，键为关系 id，仅含 implemented 的关系会被执行）：
  {
    "MR-001": {"input_before": [3,1,2], "output_before": [1,2,3],
               "input_after":  [2,1,3], "output_after":  [1,2,3]},
    "MR-002": {"output_before": {"n": 1}, "output_after": {"n": 1}},
    "MR-003": {"output_before": {"total": 100},
               "output_after": [{"total": 40}, {"total": 60}]}
  }
反例（关系不成立）必须被判 fail/error 并以退出码 1 结束。

CLI：
  python relations.py --catalog catalog.yaml --case case.json
  python relations.py --catalog catalog.yaml --list
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

DEFAULT_CATALOG = Path(__file__).resolve().parent / "catalog.yaml"


# ---------------------------------------------------------------- 机械原语


def canon(value):
    """规范化为可比较/可排序的字节串（多重集元素表示）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def multiset(values):
    return sorted(canon(v) for v in values)


def is_permutation(before, after):
    """after 是否为 before 的重排（多重集相等）。"""
    if not isinstance(before, list) or not isinstance(after, list):
        return False
    return multiset(before) == multiset(after)


def numeric_total(value):
    """提取数值总量：数字取自身；dict 取数值字段之和；list 取元素之和。无数值返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        total = 0
        found = False
        for item in value.values():
            part = numeric_total(item)
            if part is not None:
                total += part
                found = True
        return total if found else None
    if isinstance(value, list):
        total = 0
        found = False
        for item in value:
            part = numeric_total(item)
            if part is not None:
                total += part
                found = True
        return total if found else None
    return None


def _require(payload, *keys):
    missing = [k for k in keys if k not in payload]
    if missing:
        raise KeyError("缺少字段: %s" % ", ".join(missing))


# ---------------------------------------------------------------- 三条检查


def check_permutation_invariance(payload):
    """MR-001：输入重排 → 结果集等价。"""
    _require(payload, "input_before", "input_after", "output_before", "output_after")
    before_in, after_in = payload["input_before"], payload["input_after"]
    before_out, after_out = payload["output_before"], payload["output_after"]
    if not is_permutation(before_in, after_in):
        return "error", "前置条件不成立：input_after 不是 input_before 的重排"
    if not isinstance(before_out, list) or not isinstance(after_out, list):
        return "error", "前置条件不成立：输出必须是列表（结果集语义）"
    if multiset(before_out) != multiset(after_out):
        return "fail", "结果集不等价：before=%s after=%s" % (canon(before_out), canon(after_out))
    return "pass", "重排后结果集多重集等价（%d 个元素）" % len(after_out)


def check_retry_idempotence(payload):
    """MR-002：相同输入重试 → 输出幂等。"""
    _require(payload, "output_before", "output_after")
    before, after = payload["output_before"], payload["output_after"]
    if canon(before) != canon(after):
        return "fail", "两次输出不相等：%s != %s" % (canon(before), canon(after))
    return "pass", "重试输出深相等（幂等成立）"


def check_split_conservation(payload):
    """MR-003：单元拆分 → 总量守恒。"""
    _require(payload, "output_before", "output_after")
    before_total = numeric_total(payload["output_before"])
    parts = payload["output_after"]
    if not isinstance(parts, list):
        return "error", "前置条件不成立：output_after 必须是拆分后的列表"
    after_total = numeric_total(parts)
    if before_total is None or after_total is None:
        return "error", "无可提取数值总量（before=%s after=%s）" % (before_total, after_total)
    if before_total != after_total:
        return (
            "fail",
            "总量不守恒：整体=%s 拆分和=%s（差 %s）"
            % (before_total, after_total, after_total - before_total),
        )
    return "pass", "总量守恒：整体=%s 拆分和=%s" % (before_total, after_total)


CHECKS = {
    "MR-001": ("输入重排→结果集等价", check_permutation_invariance),
    "MR-002": ("重试→幂等", check_retry_idempotence),
    "MR-003": ("单元拆分→总量守恒", check_split_conservation),
}


# ---------------------------------------------------------------- 运行入口


def load_catalog(path=DEFAULT_CATALOG):
    doc = _yamlmini.load(str(path))
    relations = doc.get("relations") or []
    if not relations:
        raise ValueError("catalog 无 relations 条目: %s" % path)
    for entry in relations:
        if entry.get("status") not in ("candidate", "implemented", "rejected"):
            raise ValueError("非法 status: %r（%s）" % (entry.get("status"), entry.get("id")))
    return relations


def implemented_ids(relations):
    return [r["id"] for r in relations if r.get("status") == "implemented"]


def run_case(relations, case):
    """对 case 中出现的 implemented 关系逐条执行，返回结果 dict。"""
    results = []
    for rel_id in implemented_ids(relations):
        entry_desc = next(r for r in relations if r["id"] == rel_id)
        payload = case.get(rel_id)
        if payload is None:
            results.append({"id": rel_id, "relation": CHECKS[rel_id][0], "status": "skipped", "detail": "case 未提供该关系数据"})
            continue
        if rel_id not in CHECKS:
            results.append({"id": rel_id, "relation": entry_desc.get("relation", ""), "status": "error", "detail": "catalog 标记 implemented 但 relations.py 无实现（契约破裂）"})
            continue
        try:
            status, detail = CHECKS[rel_id][1](payload)
        except KeyError as exc:
            status, detail = "error", str(exc)
        results.append({"id": rel_id, "relation": CHECKS[rel_id][0], "status": status, "detail": detail})
    summary = {key: sum(1 for r in results if r["status"] == key) for key in ("pass", "fail", "error", "skipped")}
    return {"results": results, "summary": summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description="蜕变关系可执行检查（AC-4）")
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--case", help="case JSON 文件路径")
    parser.add_argument("--list", action="store_true", help="列出 catalog 条目")
    args = parser.parse_args(argv)

    try:
        relations = load_catalog(args.catalog)
    except (OSError, ValueError) as exc:
        print("FATAL: catalog 加载失败: %s" % exc, file=sys.stderr)
        return 2

    if args.list:
        for r in relations:
            print("%-8s %-12s %s" % (r["id"], r.get("status", "?"), r.get("relation", "")[:60]))
        print("total: %d  implemented: %d" % (len(relations), len(implemented_ids(relations))))
        return 0

    if not args.case:
        parser.error("需要 --case 或 --list")
    try:
        case = json.loads(Path(args.case).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print("FATAL: case 读取失败: %s" % exc, file=sys.stderr)
        return 2

    report = run_case(relations, case)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = report["summary"]
    if summary["fail"] or summary["error"]:
        print("VERDICT: 蜕变关系被违反（fail=%d error=%d）" % (summary["fail"], summary["error"]), file=sys.stderr)
        return 1
    print("VERDICT: pass=%d skipped=%d" % (summary["pass"], summary["skipped"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
