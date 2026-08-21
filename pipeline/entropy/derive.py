#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""derive.py —— k=5 跨族冷上下文独立重派生 + 底噪重采样（W4-C1 .github#220，
ADR-0066 决策 1/4）。

LLM 环节唯一入口 = 仓内计量 wrapper（pipeline/metering/metering-wrapper.sh，
W2-C3 ADR-0062——INV-06：无计量不算成功）；本文件只组 prompt 与解析输出，
不做任何直连（scan-direct-sdk.sh 可扫）。两种形态：
  live    5 族路由齐备 + LLM_API_KEY → 逐路经 wrapper 调 provider；
          缺任一族路由 fail-closed 拒跑（伪跨族=红队语义退化，见 policy.py）。
  replay  --replay-dir 提供回放响应（provider chat/completions 形态）→ 零凭据
          零网络全链路可跑（CI/本地自测）。回放文件同样走 wrapper 落计量账本
          （--replay-file 路径），保持 invoke 记录形态一致。

冷上下文（Cognition 实证：污染上下文掩盖分歧）：每路独立 prompt 文件、互不
可见、不带任何先前实现残留；族标记写入 prompt 输入（AC-4 断言）。

用法:
  python3 derive.py --spec spec.md --out-dir out/ [--replay-dir r/]
出: out/derivations.json + out/noise.json
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, "..", "metering", "metering-wrapper.sh")

# 本次度量运行的随机数锚：invoke_id 跨运行可重复落账（同一 spec 重跑是合法
# 操作），同运行内幂等去重仍由账本执法（重复 invoke_id 拒绝）。
RUN_NONCE = time.strftime("%Y%m%dT%H%M%S", time.gmtime())

import policy  # noqa: E402

# ---------------- spec 条款解析（条款 ID 坐标） ----------------

_CLAUSE_RE = re.compile(r"^- (?:\*\*)?([A-Z]+-\d+)(?:\*\*)?:\s*(.+?)\s*$")
_SECTION_RE = re.compile(r"^## (.+?)\s*$")


def parse_spec_clauses(spec_text):
    """解析 spec 正文条款：`- ID: 文本` 列表项 → [{id, section, line, text}]。

    frontmatter acceptanceCriteria（AC-n）不入条款坐标（红队对象是正文不变量/
    行为/契约条款，与 spec-template.md 生成形态对齐）。
    """
    clauses, section = [], ""
    for lineno, line in enumerate(spec_text.splitlines(), 1):
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1)
            continue
        m = _CLAUSE_RE.match(line)
        if m:
            clauses.append({"id": m.group(1), "section": section,
                            "line": lineno, "text": m.group(2)})
    return clauses


def spec_clause_map(spec_text):
    return {c["id"]: c for c in parse_spec_clauses(spec_text)}


# ---------------- prompt 组装（冷上下文 + 族标记） ----------------

PROMPT_TMPL = """[派生任务 · 冷上下文] 你是独立实现者。你的派生者族标记：{family}（lane {lane}/{k}）。
除本消息与下方 spec 外，你不携带任何先前实现或他人派生残留（冷上下文红队，ADR-0066）。

仅依据下方 spec，逐条输出你对每个条款的实现读法（实现要点/接口契约/边界行为）。
每条读法必须：忠实陈述你将如何实现该条款；不复述条款字面；不引用其他派生者。

[spec 开始]
{spec}
[spec 结束]

输出（严格 JSON，无围栏无解释）：{{"readings": [{{"clause": "<条款ID>", "text": "<你的实现读法，1-3 句>"}}]}}
条款清单（逐条必有）：{clause_ids}
"""


def build_prompt(spec_text, clause_ids, family, lane):
    return PROMPT_TMPL.format(family=family, lane=lane, k=policy.K,
                              spec=spec_text, clause_ids=json.dumps(clause_ids))


# ---------------- 计量 wrapper 调用 ----------------

def call_wrapper(prompt, model, role, invoke_id, replay_file=None, base_url=None):
    """经 metering-wrapper.sh 调 LLM（唯一入口，INV-06）；回放传 replay_file。"""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt",
                                     delete=False, newline="\n") as pf:
        pf.write(prompt)
        prompt_file = pf.name
    cmd = ["bash", WRAPPER, "--model", model, "--role", role, "--tag", "w4c1-entropy",
           "--prompt-file", prompt_file, "--max-tokens", "2048",
           "--temperature", "0.2", "--thinking", "disabled",
           "--invoke-id", invoke_id]
    if replay_file:
        cmd += ["--replay-file", replay_file]
    if base_url:
        cmd = ["env", f"LLM_BASE_URL={base_url}"] + cmd
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    finally:
        os.unlink(prompt_file)
    if out.returncode != 0:
        raise SystemExit(f"wrapper 调用失败（lane invoke={invoke_id} rc={out.returncode}）："
                         f"{out.stderr[:400]}")
    return out.stdout


def parse_readings(content, lane_label):
    """解析派生输出为 readings 列表；fail-closed：不可解析 = 该路派生失败。"""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        raise SystemExit(f"{lane_label} 派生输出无 JSON 对象（fail-closed）")
    try:
        data = json.loads(m.group(0))
        readings = data["readings"]
        assert isinstance(readings, list) and readings
        for r in readings:
            assert isinstance(r["clause"], str) and isinstance(r["text"], str)
        return [{"clause": r["clause"], "text": r["text"]} for r in readings]
    except (ValueError, KeyError, TypeError, AssertionError) as e:
        raise SystemExit(f"{lane_label} 派生输出不可解析（fail-closed）：{e}")


# ---------------- k 路派生 + 底噪 ----------------

def _families_for_k():
    """k=5 且 5 族各 1 路（AC-4：各族 >=1；多出的路按族轮转）。"""
    return [policy.FAMILIES[i % len(policy.FAMILIES)] for i in range(policy.K)]


def validate_lanes(derivations):
    """AC-4 断言：k 路、输入含族标记、各族 >=1 路。违反 → SystemExit。"""
    lanes = derivations["lanes"]
    if len(lanes) != policy.K:
        raise SystemExit(f"k={len(lanes)} 期望 {policy.K}")
    fams = [l["family"] for l in lanes]
    missing = [f for f in policy.FAMILIES if f not in fams]
    if missing:
        raise SystemExit(f"族覆盖缺口：{missing}（各族>=1 路是 AC-4 前置）")
    for l in lanes:
        if not l.get("family_marker"):
            raise SystemExit(f"lane{l['lane']} 输入缺族标记（AC-4）")
    return True


def derive_all(spec_text, clause_ids, replay_dir=None):
    """k=5 跨族冷上下文独立派生 → derivations dict（live/replay 同构）。"""
    mode = "replay" if replay_dir else "live"
    families = _families_for_k()
    if mode == "live":
        ready, missing = policy.live_routes_ready()
        if not ready:
            raise SystemExit(f"live 跨族路由不全（缺 {missing}）——伪跨族拒跑（fail-closed，"
                             f"见 policy.py FAMILY_ROUTES；回放/自测用 --replay-dir）")
        if not os.environ.get("LLM_API_KEY"):
            raise SystemExit("LLM_API_KEY 未设置（live 模式必需；离线用 --replay-dir）")
    spec_sha = "sha256:" + hashlib.sha256(
        spec_text.encode("utf-8")).hexdigest()
    lanes = []
    for i, family in enumerate(families, 1):
        prompt = build_prompt(spec_text, clause_ids, family, i)
        invoke = f"entropy-derive-{RUN_NONCE}-{hashlib.sha256(spec_text.encode()).hexdigest()[:8]}-lane{i}"
        if mode == "replay":
            content = call_wrapper(prompt, f"replay-{family}", policy.DERIVE_ROLE,
                                   invoke, replay_file=os.path.join(replay_dir, f"derive-lane{i}.json"))
        else:
            model, _ = policy.FAMILY_ROUTES[family]
            content = call_wrapper(prompt, model, policy.DERIVE_ROLE, invoke,
                                   base_url=policy.route_base_url(family))
        lanes.append({
            "lane": i, "family": family,
            "model": f"replay-{family}" if mode == "replay" else policy.FAMILY_ROUTES[family][0],
            "context": "cold",                 # 冷上下文标记（互不可见、无残留）
            "family_marker": family in prompt,  # AC-4：输入含族标记
            "readings": parse_readings(content, f"lane{i}({family})"),
        })
    derivations = {"schema": "entropy-derivations/v1", "mode": mode,
                   "spec_sha256": spec_sha, "k": policy.K, "lanes": lanes}
    validate_lanes(derivations)
    return derivations


def resample_noise(spec_text, clause_ids, replay_dir=None):
    """底噪（AC-2）：同族同提示重采样 m 次 → m 份读法样本。"""
    mode = "replay" if replay_dir else "live"
    family = policy.NOISE_FAMILY
    prompt = build_prompt(spec_text, clause_ids, family, 0)  # lane 0 = 底噪通道
    samples = []
    for s in range(1, policy.NOISE_RESAMPLE_M + 1):
        invoke = f"entropy-noise-{RUN_NONCE}-{hashlib.sha256(spec_text.encode()).hexdigest()[:8]}-s{s}"
        if mode == "replay":
            content = call_wrapper(prompt, f"replay-{family}", policy.NOISE_ROLE,
                                   invoke, replay_file=os.path.join(replay_dir, f"noise-sample{s}.json"))
        else:
            model, _ = policy.FAMILY_ROUTES[family]
            content = call_wrapper(prompt, model, policy.NOISE_ROLE, invoke,
                                   base_url=policy.route_base_url(family))
        samples.append({"sample": s, "family": family,
                        "readings": parse_readings(content, f"noise-s{s}")})
    return {"schema": "entropy-noise/v1", "mode": mode,
            "family": family, "resample_m": policy.NOISE_RESAMPLE_M,
            "samples": samples}


def main(argv=None):
    ap = argparse.ArgumentParser(description="k=5 跨族冷上下文独立重派生（LLM 经计量 wrapper）")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--replay-dir")
    args = ap.parse_args(argv)
    spec_text = open(args.spec, encoding="utf-8").read()
    clause_ids = [c["id"] for c in parse_spec_clauses(spec_text)]
    if not clause_ids:
        raise SystemExit("spec 未解析到任何条款（`- ID: 文本` 形态）——条款坐标无从谈起")
    os.makedirs(args.out_dir, exist_ok=True)
    derivations = derive_all(spec_text, clause_ids, args.replay_dir)
    noise = resample_noise(spec_text, clause_ids, args.replay_dir)
    for name, data in (("derivations.json", derivations), ("noise.json", noise)):
        with open(os.path.join(args.out_dir, name), "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    fams = [l["family"] for l in derivations["lanes"]]
    print(f"派生完成：k={derivations['k']} 族={fams}（各族>=1 路）"
          f"底噪 m={noise['resample_m']} → {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
