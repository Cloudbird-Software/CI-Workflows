#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eval_wave.py —— optimization 波次评测 harness（IR-0006 W5-E2 / AC-10c / BEH-08）

基线/候选同 harness 同语料，只换被优化物（rules.yaml）——评测装置本身钉死，
指标差异才能归因于优化本体（四元组 pin 的 harness 侧，HO-0008 口径延伸）。

指标（与 .github governance/policy/eval-gates.yaml 声明族对齐）：
  precision   —— kept 建议中 fixed_later=true 的比例（post-fix 口径的语料化
                 近似：ground truth=fixture 标注，非自报）
  evaluated   —— kept 数（precision 分母规模；样本塌缩=评测失效）
  drop_rate   —— postprocess 三重过滤丢率（宪法 §4E：过滤率本身是指标）
  cost_usd    —— 后处理为确定性零 LLM 环节 → 0.0（诚实口径；LLM 面优化
                 须报真实 spend，本 harness 不适用）
  latency_ms  —— postprocess 主逻辑 20 次重复总耗时（均值×20，比例上界
                 语义稳定；单次亚毫秒抖动会被 ratio 误判）

用法：
  eval_wave.py --corpus corpus.jsonl --diff corpus.diff --rules <rules.yaml> --out report.json
退出码：0=报告产出 | 1=语料/规则非法 | 2=infra。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import postprocess  # noqa: E402

REPEAT = 20


def die2(msg: str) -> None:
    print(f"FATAL eval_wave: {msg}", file=sys.stderr)
    sys.exit(2)


def main() -> int:
    def opt(name: str) -> str:
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else ""

    corpus_p, diff_p, rules_p, out_p = (opt("--corpus"), opt("--diff"), opt("--rules"), opt("--out"))
    if not all([corpus_p, diff_p, rules_p, out_p]):
        print(__doc__)
        return 1

    try:
        comments, ground = [], {}
        for ln in Path(corpus_p).read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            rec = json.loads(ln)
            if rec.get("record") != "comment":
                die2(f"corpus 行 record 非法: {rec.get('record')!r}")
            if not isinstance(rec.get("fixed_later"), bool):
                die2(f"corpus idx={rec.get('idx')} 缺 fixed_later 布尔标注")
            if isinstance(rec.get("start_line"), int):
                ground[(rec["path"], rec["start_line"])] = rec["fixed_later"]
            comments.append({k: rec.get(k) for k in
                             ("path", "start_line", "end_line", "content",
                              "existing_code", "suggestion_code")})
        if not comments:
            die2("corpus 为空")
        added = postprocess.parse_unified_diff(Path(diff_p).read_text(encoding="utf-8"))
        rules = postprocess.load_rules(rules_p)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        die2(f"输入不可用: {e}")

    ocr_doc = {"status": "success", "llm": {"provider": "fixture", "model": "corpus"},
               "comments": comments}

    t0 = time.perf_counter()
    for _ in range(REPEAT):
        kept, stats = postprocess.postprocess(ocr_doc, added, rules)
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    hits = sum(1 for k in kept if ground.get((k["path"], k["start_line"]), False))
    report = {
        "metrics": {
            "precision": round(hits / len(kept), 4) if kept else 0.0,
            "evaluated": len(kept),
            "drop_rate": stats["drop_rate"],
        },
        "cost_usd": 0.0,
        "latency_ms": latency_ms,
        "provenance": (
            f"corpus@{hashlib.sha256(Path(corpus_p).read_bytes()).hexdigest()[:8]}"
            f"+rules@{hashlib.sha256(Path(rules_p).read_bytes()).hexdigest()[:8]}"
            f" kept={len(kept)} hits={hits} repeat={REPEAT}"
        ),
    }
    Path(out_p).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK    report={out_p} precision={report['metrics']['precision']} "
          f"evaluated={report['metrics']['evaluated']} drop_rate={report['metrics']['drop_rate']} "
          f"latency_ms={latency_ms}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
