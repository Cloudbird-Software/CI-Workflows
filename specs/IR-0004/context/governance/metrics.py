#!/usr/bin/env python3
"""metrics.py —— 度量计算库（W5-C4 .github#227，ADR-0073；宪法 §8/§12）

纯计算库：输入=采集层（dashboard-update.py / board-sync.py）拿到的原始数据结构，
输出=dashboard JSON 状态块 + human-brief 摘要。零网络、零时钟旁路（now 一律
注入）——全部口径可离线 fixture 复算（owner 周审计独立复算权，宪法 §7）。

北极星对互锁（AC-1 / ADR-0073 决策 1，宪法级）：
  零接触合并数 × 质量护栏是**一个指标对**。合并数单独上屏，Goodhart 定律保证
  系统会牺牲质量刷合并数。互锁规则：
  - 任一护栏 red → 合并数 display=0（呈现层显式归零 + 原因标注；raw 保留在 JSON
    ——非数据删除，护栏回绿自动恢复显示）
  - 护栏 pending（数据源未落/零分母）≠ 劣化，不触发归零，但独立显示盲区
    （决策 7：缺数据不得渲染成好数据，也不得渲染成坏数据）
  - 演习红率目标 ≈100%（<阈值=关卡漏检=质量劣化，ADR-0069）

阈值唯一来源 governance/policy/metrics.yaml（本库不内嵌任何阈值）。
"""
import argparse
import datetime as _dt
import json
import math
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("FATAL 缺少 PyYAML（CI 预装；本地 pip install pyyaml）", file=sys.stderr)
    raise SystemExit(2)

DIR = os.path.dirname(os.path.abspath(__file__))

# 护栏渲染序（同屏呈现顺序=后果严重度：逃逸>回滚>演习>误放行>泄漏>通过率差）
GUARD_ORDER = ("escape_rate_sustained", "revert_rate", "drill_red_rate",
               "false_allow", "state_change_leak", "holdout_gap")


def load_policy(path=None):
    p = path or os.path.join(DIR, "policy", "metrics.yaml")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def percentile(values, q):
    """最近邻秩百分位（values 空→None：零分母诚实口径，不除零不出假值）。"""
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))
    return s[idx]


def _guard_status(name, inp, policy):
    """单护栏三值判定：green/red/pending（缺输入或零分母=pending，不造数）。"""
    g = (policy.get("north_star") or {}).get("guardrails") or {}
    if name == "escape_rate_sustained":
        # 逃逸>0 持续=当前窗与上一窗均>0（事件时戳直算双窗，无跨轮状态残留）
        if not isinstance(inp, dict) or "current" not in inp or "previous" not in inp:
            return "pending", "数据源未接入"
        cur, prev = inp["current"], inp["previous"]
        val = {"current": cur, "previous": prev}
        if cur > 0 and prev > 0:
            return "red", f"逃逸持续：上一窗 {prev} + 本窗 {cur}（[auto-revert]+post-merge P0）"
        return "green", f"双窗逃逸：上一窗 {prev} · 本窗 {cur}"
    if name == "revert_rate":
        if not isinstance(inp, dict) or not inp.get("denom"):
            return "pending", "零分母（窗口内无 merged PR——不除零，#98 T2）"
        rate = inp["num"] / inp["denom"]
        thr = g["revert_rate"]["red_when_gt"]
        return ("red" if rate > thr else "green"), f"{inp['num']}/{inp['denom']}={rate:.3f}（阈 {thr}）"
    if name == "drill_red_rate":
        if not isinstance(inp, dict) or not inp.get("denom"):
            return "pending", "零可判定演习（红率不造 100%）"
        rate = inp["red"] / inp["denom"]
        thr = g["drill_red_rate"]["red_when_lt"]
        return ("red" if rate < thr else "green"), f"红 {inp['red']}/{inp['denom']}={rate:.2f}（目标 ≈100%，阈 {thr}）"
    if name == "false_allow":
        if inp is None:
            return "pending", "arbiter 台账不可读（盲区独立显示，不冒充 0）"
        return ("red" if inp > g["false_allow"]["red_when_gt"] else "green"), f"窗口内误放行 {inp} 例"
    if name in ("state_change_leak", "holdout_gap"):
        return "pending", f"数据源 pending（{g[name].get('data_source')}）——盲区独立显示"
    return "pending", f"未知护栏 {name}"


def north_star(data, policy):
    """北极星对同屏互锁（AC-1）。data 键：zero_touch_merges_7d:int|None + 各护栏输入。"""
    raw = data.get("zero_touch_merges_7d")
    guards, reasons = {}, []
    for name in GUARD_ORDER:
        status, detail = _guard_status(name, data.get(name), policy)
        guards[name] = {"status": status, "detail": detail}
        if status == "red":
            reasons.append(name)
    zeroed = bool(reasons)  # 呈现层归零=仅护栏 red；pending/零合并周不标注归零
    display = 0 if zeroed else (raw if raw is not None else 0)
    return {
        "zero_touch_merges_7d": {
            "raw": raw,  # 原始计数永不删除（归零只在呈现层）
            "display": display, "zeroed": zeroed,
            "zeroed_reasons": reasons,
            "note": ("护栏破线期间的产出计数无意义——显示归零+原因标注；"
                     "raw 保留（ADR-0073 决策 1：呈现层归零，非数据删除）"
                     if zeroed else ("零接触合并周（分母为 0 的如实 0）" if raw == 0 else "护栏全绿——如实显示")),
        },
        "guardrails": guards,
        "interlocked_zeroed": zeroed,
        "pending_blind_zones": [n for n in GUARD_ORDER if guards[n]["status"] == "pending"],
    }


# ---------- 四类指标（AC-2/AC-4，宪法 §8；缺数据=pending 不造数） ----------

def _fmt_s(sec):
    """秒→人读时长（签署/停留呈现用）。"""
    if sec is None:
        return "pending"
    if sec < 90:
        return f"{sec:.0f}s"
    if sec < 5400:
        return f"{sec / 60:.1f}min"
    return f"{sec / 3600:.1f}h"


def attention_stats(data, policy):
    """注意力会计：签署耗时/needs-human p90/超时默认触发数/可疑快速签署数（宪法 §7）。"""
    at = policy["attention"]
    durations = data.get("sign_durations_seconds") or []
    dwell = data.get("needs_human_dwell_hours") or []
    p90 = percentile(dwell, 0.90)
    stop_h = at["needs_human_p90_stop_hours"]
    return {
        "sign_count": len(durations),
        "sign_p50_seconds": percentile(durations, 0.50),
        "sign_p90_seconds": percentile(durations, 0.90),
        "sign_in_flight": data.get("sign_in_flight", 0),  # 仍 ir-draft 未签（不计耗时统计）
        "suspicious_fast_signs": sum(1 for s in durations if s < at["suspicious_fast_sign_seconds"]),
        "suspicious_fast_sign_seconds": at["suspicious_fast_sign_seconds"],
        "needs_human_count": len(dwell),
        "needs_human_p90_hours": p90,
        "needs_human_p90_stop": (None if p90 is None else bool(p90 > stop_h)),
        "needs_human_stop_hours": stop_h,
        # 数据源未落（决策卡/审计包基建在后续波次）——pending 诚实显示，不冒充 0
        "timeout_defaults": "pending:决策卡超时默认触发数（数据源未落）",
        "owner_minutes_per_merge": "pending:每合并 owner 分钟（评审事件流未落）",
        "audit_overtime_rate": "pending:周审计超时率（审计包组装未落）",
    }


def security_stats(data, policy):
    """安全正确性：误放行/误拒（arbiter 台账窗内）、泄漏、演习红率、陷阱拦截率。"""
    win_days = policy["security"]["false_decision_window_days"]
    now = _parse_iso(data.get("now"))
    allow = deny = 0
    for rec in data.get("false_decision_lines") or []:
        ts = _parse_iso(rec.get("date"))
        if ts and now and (now - ts).days <= win_days:
            if rec.get("kind") == "false-allow":
                allow += 1
            elif rec.get("kind") == "false-deny":
                deny += 1
    drills = data.get("drill_records") or []
    reds = sum(1 for r in drills if r.get("verdict") == "red")
    denom = sum(1 for r in drills if r.get("verdict") in ("red", "green"))  # no-surface 不入分母
    return {
        "false_allow_window": allow,
        "false_deny_window": deny,
        "false_decision_window_days": win_days,
        "drill_red": reds, "drill_denom": denom,
        "state_change_leaks": "pending:未经仲裁的状态变更泄漏检测面未建",
        "trap_intercept_rate": "pending:陷阱拦截率（ADR-0071 W5-C2 信任门未落）",
    }


def cost_stats(data, policy):
    """成本：单 IR 美元（声明价折算——公开仓计费净额 $0，防失控速率的虚拟口径）。"""
    co = policy["cost"]
    minutes = data.get("actions_minutes_month")
    tokens = data.get("llm_tokens_month")
    irs = data.get("ir_count_month")
    per_ir = None
    if minutes is not None and tokens is not None and irs:
        per_ir = round((minutes * co["actions_price_per_minute_usd"]
                        + tokens / 1000 * co["llm_price_per_1k_tokens_usd"]) / irs, 4)
    return {
        "actions_minutes_month": minutes,
        "llm_tokens_month": tokens,
        "ir_count_month": irs,
        "per_ir_usd": per_ir,
        "per_ir_usd_note": ("pending:当月零 IR——不除零（#98 T2）" if irs == 0
                            else "声明价折算（actions $/min × 分钟 + LLM $/1k × token）/ 当月 IR 数"),
        "actions_price_per_minute_usd": co["actions_price_per_minute_usd"],
        "llm_price_per_1k_tokens_usd": co["llm_price_per_1k_tokens_usd"],
        "snapshot_age_minutes": data.get("cost_snapshot_age_minutes"),
        "butler_usd_week": "pending:管家美元/周（按 workflow 分钟拆账未落）",
        "patrol_yield": "pending:patrol yield（W3-C2 demo-probe 期，真实 bug 计数为 0 分母）",
    }


def user_results_stats(data, policy):
    """用户结果指标：各产品仓声明读取位（文件缺失=pending）+ 季度难测配额记录位。"""
    ur = policy["user_results"]
    files = data.get("user_metric_files") or {}
    products = {}
    for p in ur.get("products") or []:
        repo = p["repo"]
        m = files.get(repo)
        if isinstance(m, dict) and m.get("metric_key") and "value" in m:
            products[repo] = {"status": "ok", **m}
        else:
            products[repo] = {"status": "pending",
                              "detail": f"产品仓未声明用户结果指标（{ur['read_path']} 缺失——埋点滞后，ADR-0073 后果节）"}
    quota = (ur.get("quarterly_hard_quota") or {}).get("entries") or []
    return {"products": products,
            "quarterly_hard_quota": {"entries": quota,
                                     "note": "配额制：每季度刻意做一个难测的——entries 空=本季未立（记录位，owner 回填）"}}


def build_payload(data, policy):
    """schema v2 组装：north_star + metrics 四组（generated_at 由采集层注入）。"""
    return {
        "generated_at": data.get("generated_at"),
        "north_star": north_star(data, policy),
        "metrics": {
            "attention": attention_stats(data, policy),
            "security": security_stats(data, policy),
            "cost": cost_stats(data, policy),
            "user_results": user_results_stats(data, policy),
        },
    }


# ---------- human-brief 渲染（宪法 §8"人 30 秒读懂"；正文顶部=北极星对） ----------

def render_brief(payload):
    ns, m = payload["north_star"], payload["metrics"]
    z = ns["zero_touch_merges_7d"]
    if z["zeroed"]:
        head = (f"零接触合并数（近 7 天）：**0（显示归零——护栏破线："
                f"{'、'.join(z['zeroed_reasons'])}）**；原始计数 {z['raw']} 保留在 JSON raw"
                "（呈现层归零，非数据删除）")
    elif z["raw"] == 0:
        head = "零接触合并数（近 7 天）：**0**（零接触合并周——如实 0，非归零）"
    else:
        head = f"零接触合并数（近 7 天）：**{z['raw']}**（护栏全绿——如实显示）"
    gtxt = " · ".join(f"{n} {ns['guardrails'][n]['status']}" for n in GUARD_ORDER)
    glines = "\n".join(f"  - {n}: **{g['status']}**（{g['detail']}）"
                       for n, g in ns["guardrails"].items())
    at, se, co, ur = m["attention"], m["security"], m["cost"], m["user_results"]
    p90h = "pending" if at["needs_human_p90_hours"] is None else f"{at['needs_human_p90_hours']:.0f}h"
    per_ir = "pending" if co["per_ir_usd"] is None else f"${co['per_ir_usd']}"
    prod_txt = " · ".join(f"{r}: {v['status']}" + (f"（{v['metric_key']}={v['value']}{v.get('unit', '')}）" if v["status"] == "ok" else "")
                          for r, v in sorted(ur["products"].items()))
    quota = "、".join(f"{e['quarter']} {e['product']}（{e.get('status', 'planned')}）"
                      for e in ur["quarterly_hard_quota"]["entries"]) or "本季未立（记录位空——诚实显示）"
    return f"""## 北极星对（同屏互锁 · 宪法 §8 / ADR-0073 决策 1）

{head}
质量护栏：{gtxt}
{glines}
盲区（pending，不参与归零判定——缺数据≠劣化）：{'、'.join(ns['pending_blind_zones']) or '无'}

## 四类指标（宪法 §8 全景）

- 注意力会计：签署 {at['sign_count']} 例（p50 {_fmt_s(at['sign_p50_seconds'])} / p90 {_fmt_s(at['sign_p90_seconds'])}，在途 {at['sign_in_flight']}）· 可疑快速签署（<{at['suspicious_fast_sign_seconds']}s）{at['suspicious_fast_signs']} 例 · needs-human {at['needs_human_count']} 张 p90 停留 {p90h}（>{at['needs_human_stop_hours']}h=整机停摆线）· 超时默认触发 pending
- 安全正确性（{se['false_decision_window_days']} 天窗）：误放行 {se['false_allow_window']} · 误拒 {se['false_deny_window']} · 演习红率 {se['drill_red']}/{se['drill_denom']} · 泄漏 pending · 陷阱拦截率 pending
- 成本：单 IR {per_ir}（Actions {co['actions_minutes_month'] if co['actions_minutes_month'] is not None else 'pending'} 分钟 + LLM {co['llm_tokens_month'] if co['llm_tokens_month'] is not None else 'pending'} token，声明价）· 管家美元/周 pending · patrol yield pending
- 用户结果：{prod_txt}
- 季度难测配额：{quota}
"""


def _parse_iso(s):
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("northstar", help="北极星互锁判定（fixture 输入→JSON 输出）")
    a.add_argument("--input", required=True, help="JSON 文件：{zero_touch_merges_7d, escape_rate_sustained, ...}")
    a.add_argument("--policy", default=None)
    a = sub.add_parser("eval", help="全量指标计算（fixture/采集层输入→payload JSON + human-brief）")
    a.add_argument("--input", required=True, help="JSON 文件（dashboard 数据结构，见各 *_stats docstring）")
    a.add_argument("--policy", default=None)
    a.add_argument("--render", action="store_true", help="附加 human-brief markdown（分节符 ==== 后输出）")
    args = ap.parse_args(argv)
    policy = load_policy(args.policy)
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    if args.cmd == "northstar":
        print(json.dumps(north_star(data, policy), ensure_ascii=False, indent=2))
        return 0
    payload = build_payload(data, policy)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.render:
        print("\n==== human-brief ====\n")
        print(render_brief(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
