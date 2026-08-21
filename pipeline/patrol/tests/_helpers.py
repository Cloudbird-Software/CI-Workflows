#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集成自测共享件（unittest discover 不收集本文件——非 test* 前缀）。"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PATROL = os.path.join(HERE, "..", "patrol.py")
DEMO = os.path.join(HERE, "..", "demo-target")
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
POLICY = os.path.join(DEMO, "policy-demo.yaml")
REPLAY = os.path.join(DEMO, "llm-replay.json")
REPO = "Cloudbird-Software/CI-Workflows"


def patrol_cli(*args, env_extra=None):
    env = dict(os.environ)
    env.pop("LLM_API_KEY", None)  # 自测零真实 LLM——显式清空防止本地环境泄漏进判定
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, PATROL, *args], capture_output=True,
                          text=True, env=env, timeout=600)


def run_patrol(state, out, run_id, seed, clock, replay=REPLAY):
    r = patrol_cli("run", "--policy", POLICY, "--state", state, "--out", out,
                   "--target-base", ROOT, "--repo", REPO, "--run-id", run_id,
                   "--seed", str(seed), "--clock", clock,
                   *(["--llm-replay", replay] if replay else []))
    if r.returncode != 0:
        raise AssertionError(f"patrol run rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def tmpdir(prefix):
    return tempfile.mkdtemp(prefix=prefix)
