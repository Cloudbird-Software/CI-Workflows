#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""demo 演习（W3-C2 .github#219 AC-1/AC-3 证据）——对仓内 demo 靶场全链路跑通：

  1) patrol run #1（draft 模式零线上开单；LLM 走离线回放零真实调用）
  2) 三值判定 reproduced → 毕业：回归测试文件（fail-before：靶场带缺陷必须红）
     + 场景离开 patrol 语料
  3) patrol run #2（+2h 错峰窗口）：已开指纹全部去重（不重复开单）、
     毕业场景不再消费（防刷熟）、observation 桶两次独立升级开单
  4) yield 指标输出（每百次唯一真 bug 数/复现存活率/信噪比）

退出码 0=演习全绿；非 0=断言失败（CI selftest job 红——演习不是摆设）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
PATROL = os.path.join(ROOT, "pipeline", "patrol", "patrol.py")
POLICY = os.path.join(HERE, "policy-demo.yaml")
REPLAY = os.path.join(HERE, "llm-replay.json")
REPO = "Cloudbird-Software/CI-Workflows"
T0 = "2026-08-22T03:43:00Z"
T1 = "2026-08-22T05:43:00Z"  # +2h：频控小时窗滚动（deferred 获得再攻击机会）


def run_patrol(*extra):
    r = subprocess.run([sys.executable, PATROL, "run", "--policy", POLICY, *extra],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        raise SystemExit(f"FATAL: patrol run 失败 rc={r.returncode}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def patrol_cli(*args):
    return subprocess.run([sys.executable, PATROL, *args], capture_output=True,
                          text=True, timeout=120)


def check(cond, label):
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        raise SystemExit(f"EXERCISE FAIL: {label}")


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def export_evidence(tmp):
    """证据打包到 PATROL_EXERCISE_OUT（CI 上传 artifact；PR body 引用即证据）。"""
    dst = os.environ.get("PATROL_EXERCISE_OUT", os.path.join(os.getcwd(), "patrol-exercise"))
    os.makedirs(dst, exist_ok=True)
    for sub in ("out-r1", "out-r2", "out-r3", "out-grad"):
        src = os.path.join(tmp, "out", sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dst, sub), dirs_exist_ok=True)
    for name in ("fingerprints.jsonl", "observations.jsonl", "verdicts.jsonl",
                 "graduations.jsonl", "runs.jsonl", "corpus-state.json"):
        src = os.path.join(tmp, "state", name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dst, name))
    print(f"evidence → {dst}")


def main():
    tmp = tempfile.mkdtemp(prefix="patrol-exercise-")
    state, out = os.path.join(tmp, "state"), os.path.join(tmp, "out")
    print(f"== patrol demo 演习（状态 {state}；issue_mode=draft——artifact 即证据，不开线上单）==")

    print("\n[1] run demo-1（seed=42，LLM 离线回放）")
    m1 = run_patrol("--state", state, "--out", os.path.join(out, "r1"),
                    "--target-base", ROOT, "--repo", REPO,
                    "--run-id", "demo-1", "--seed", "42",
                    "--llm-replay", REPLAY, "--clock", T0)
    print(f"  opened={m1['opened_this_run']} deferred={len(m1['deferred'])} "
          f"deduped={m1['deduped']} scenarios={m1['scenarios']} llm={m1['llm_status']}")
    check(m1["llm_status"] == "ok", "LLM 源经 metering wrapper 离线回放跑通（零真实调用）")
    check(m1["scenarios"] == {"ac-registry": 4, "escape-pattern": 2, "llm-metamorphic": 3},
          "三源场景齐备（AC 派生 4 + 逃逸变体 2 + metamorphic2/LLM1）")
    check(m1["opened_this_run"] == 6, "频控：首小时上限 6，超出 deferred（AC-2 频控面）")
    check(len(m1["deferred"]) == 2, "deferred=2（mt-commute+llm-frontier 等下一窗口再攻击）")
    check(m1["observations_seen"] == 1 and not m1["observations_escalated"],
          "LLM『看着不对』只进 observation 桶（首次不升级——AC-1）")
    check(any(o["class"] == "invariant" for o in m1["opened"]),
          "机器可判定 oracle 违约自动成单（AC-1：invariant 类命中）")
    drafts = os.listdir(os.path.join(out, "r1", "issues"))
    check(len(drafts) == 6, f"bug issue 草稿 artifact×6（附 trace+指纹）：{drafts[:2]}…")

    print("\n[2] 三值判定 → 毕业（AC-3）")
    fps = {r["scenario_id"]: r["fingerprint"] for r in read_jsonl(os.path.join(state, "fingerprints.jsonl"))}
    fp = fps["ac-AC-DEM-2"]
    r = patrol_cli("verdict", "--state", state, "--fingerprint", fp,
                   "--verdict", "reproduced", "--by", "demo-exercise")
    check(r.returncode == 0, f"verdict reproduced 登记（{fp[:19]}…）")
    g = patrol_cli("graduate", "--state", state, "--fingerprint", fp,
                   "--out", os.path.join(out, "grad"))
    check(g.returncode == 0, "graduate 执行成功")
    rec = json.loads(g.stdout.strip().splitlines()[-1])
    check(rec["removed_from_corpus"] and rec["scenario_id"] == "ac-AC-DEM-2",
          "场景离开 patrol 语料（防刷熟）")
    reg = rec["regression_test"]
    check(os.path.isfile(reg), f"回归测试文件产出（供人审入库）：{os.path.basename(reg)}")
    red = subprocess.run([sys.executable, reg], capture_output=True, text=True)
    check(red.returncode != 0 and "AssertionError" in red.stderr + red.stdout,
          "毕业回归 fail-before：靶场带缺陷必须断言红（ADR-0061）")
    bad = patrol_cli("graduate", "--state", state, "--fingerprint", fps["ac-AC-DEM-1"],
                     "--out", os.path.join(out, "grad"))
    check(bad.returncode == 2, "未复现成功的指纹毕业被拒（毕业仅由复现成功触发）")

    print("\n[3] run demo-2（+2h，seed=43——独立性成立）")
    m2 = run_patrol("--state", state, "--out", os.path.join(out, "r2"),
                    "--target-base", ROOT, "--repo", REPO,
                    "--run-id", "demo-2", "--seed", "43",
                    "--llm-replay", REPLAY, "--clock", T1)
    print(f"  opened={m2['opened_this_run']} deduped={m2['deduped']} "
          f"scenarios={m2['scenarios']} graduated={m2['graduated_active']}")
    check(m2["scenarios"]["ac-registry"] == 3, "毕业场景不再生成（4→3，防刷熟）")
    check(m2["deduped"] == 5, "同指纹全部去重（run1 已开的 5 张单不再开——AC-2）")
    check(m2["opened_this_run"] == 3, "新窗口开单 3（deferred 再攻击 2 + observation 升级 1）")
    check(len(m2["observations_escalated"]) == 1,
          "observation 桶两次独立（不同 run+不同 seed）→ 升级开单（AC-1）")

    print("\n[4] run demo-3（+4h，seed=44——全指纹收敛）")
    m3 = run_patrol("--state", state, "--out", os.path.join(out, "r3"),
                    "--target-base", ROOT, "--repo", REPO,
                    "--run-id", "demo-3", "--seed", "44",
                    "--llm-replay", REPLAY, "--clock", "2026-08-22T07:43:00Z")
    check(m3["opened_this_run"] == 0 and m3["deduped"] == 7,
          "指纹台账收敛：第三轮零新开单（去重幂等——AC-2）")

    print("\n[5] yield 指标（AC-4）")
    r = patrol_cli("metrics", "--policy", POLICY, "--state", state)
    met = json.loads(r.stdout.strip().splitlines()[-1])
    print(f"  {json.dumps({k: met[k] for k in ('runs', 'issues_opened', 'issues_reproduced', 'unique_real_bugs', 'yield_per_100_runs', 'issue_reproduction_survival_rate', 'snr')}, ensure_ascii=False)}")
    check(met["yield_per_100_runs"] == round(100 / 3, 4),
          "每百次唯一真 bug 数=33.33（3 run 1 确认）")
    check(met["issue_reproduction_survival_rate"] == round(1 / 9, 4),
          "开单复现存活率=1/9")
    check(met["snr"] is not None and met["snr"] >= met["snr_window"]["threshold"],
          f"信噪比 {met['snr']} ≥ 阈值——不触发降频")

    print("\nEXERCISE OK —— AC-1（oracle 分级+observation 桶）/AC-2（指纹去重+频控）/"
          "AC-3（毕业+离库）证据齐备；产物：" + tmp)
    export_evidence(tmp)


if __name__ == "__main__":
    main()
