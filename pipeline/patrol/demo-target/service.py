#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""demo 探针目标（W3-C2 .github#219，ADR-0065）——patrol 演习/自测的受控靶场。

stdin 一个 JSON payload → stdout 一个 JSON 包络 {ok, http_status, data, error}。
刻意播种 5+1 类缺陷（对应五类 oracle + metamorphic 等价破坏），是 patrol
oracle 负控制（ADR-0065 风险缓解：已知违约样本必须被抓）的机器真源：

  bug-1 crash        op=div b=0 → 未捕获 ZeroDivisionError（进程崩溃）
  bug-2 http-5xx     op=fetch id=boom → http_status=500
  bug-3 schema       op=report include_debug=1 → data 缺 summary 字段
  bug-4 invariant    op=transfer amount>1000 → 余额守恒被隐性费用破坏
  bug-5 perf-budget  op=search q 前缀 zzz → 延迟 350ms（预算 200ms）
  bug-6 metamorphic  op=add 且 a==7 → 结果 +1（交换律/数值形态等价被破坏）

生产仓接入时替换本目录为真实探针适配器（契约同此：stdin payload/包络 stdout）。
"""
import json
import sys
import time


def handle(p):
    op = p.get("op")
    if op == "add":
        r = p["a"] + p["b"] + (1 if p["a"] == 7 else 0)  # bug-6：metamorphic 等价破坏
        return {"ok": True, "http_status": 200, "data": {"result": r}}
    if op == "sub":
        return {"ok": True, "http_status": 200, "data": {"result": p["a"] - p["b"]}}
    if op == "mul":
        return {"ok": True, "http_status": 200, "data": {"result": p["a"] * p["b"]}}
    if op == "div":
        return {"ok": True, "http_status": 200,
                "data": {"result": p["a"] / p["b"]}}  # bug-1：b=0 未包络直接崩
    if op == "transfer":
        amt = p["amount"]
        fee = 1 if amt > 1000 else 0  # bug-4：大额隐性费用破坏守恒（无声明）
        return {"ok": True, "http_status": 200, "data": {
            "from_after": p["from_before"] - amt - fee,
            "to_after": p["to_before"] + amt}}
    if op == "fetch":
        if p.get("id") == "boom":  # bug-2：资源内部错误未兜底 → 5xx
            return {"ok": False, "http_status": 500, "data": {}, "error": "internal"}
        return {"ok": True, "http_status": 200, "data": {"id": p.get("id"), "found": True}}
    if op == "report":
        data = {"rows": [], "generated": True}
        if p.get("include_debug"):
            data = {"rows": [], "generated": True, "debug": True}  # bug-3：debug 分支丢 summary
            return {"ok": True, "http_status": 200, "data": data}
        data["summary"] = "ok"
        return {"ok": True, "http_status": 200, "data": data}
    if op == "search":
        if str(p.get("q", "")).startswith("zzz"):  # bug-5：病态前缀触发全表扫描
            time.sleep(0.35)
        return {"ok": True, "http_status": 200, "data": {"hits": 0}}
    return {"ok": False, "http_status": 400, "data": {}, "error": f"unknown op {op}"}


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        print(json.dumps({"ok": False, "http_status": 400, "data": {},
                          "error": "payload 非 JSON"}))
        return 0
    t0 = time.monotonic()
    env = handle(payload)  # bug-1 的崩溃刻意不兜底——oracle 负控制真源
    # 包络自带服务内耗时：CLI 适配器的进程墙钟含解释器启动（Windows 可达
    # 250ms），会把预算判定污染成环境噪音——perf oracle 优先消费 service_ms
    env["service_ms"] = int((time.monotonic() - t0) * 1000)
    print(json.dumps(env, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
