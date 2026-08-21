#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 可读性 rubric shadow（AC-4；ADR-0072 决策 5；宪法 §4C T2 检查项①）。

五维 rubric（判据分解+每档锚定描述=契约真源 rubrics/ai-readability-v1.yaml）：
可定位性 / 入口清晰度 / 模块深度充分性 / 命名词表一致性 / 示例文档新鲜度。

shadow 纪律（ADR-0072）：出分**仅记录不阻断**——本模块任何打分路径 exit 0
（除非 rubric/context 不可读等配置错误 exit 2，fail-visible）；升 veto 走
宪法 §5 信任门，本文件不授权。标注负债申报：数据不足的维度显式记
annotation_debt（status=insufficient-data + 原因），不许静默打分。

- --scores-fixture：回放打分（零真实 LLM；shadow 数据管道先于判官上线）。
- --api：真实判官经 scripts/llm-call.sh（计量唯一入口）按锚档打连续分（后续接通）。

用法：
  python3 pipeline/verifier-exam/rubric_shadow.py --repo-root . \
      --rubric pipeline/verifier-exam/rubrics/ai-readability-v1.yaml \
      --scores-fixture pipeline/verifier-exam/fixtures/rubric-scores-ci.json \
      --out rubric-shadow/records.jsonl
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = "verifier-rubric-shadow/v1"
DIMENSIONS = ("locatability", "entry_clarity", "module_depth",
              "naming_vocabulary", "example_freshness")


class RubricError(Exception):
    """配置失败（exit 2）"""


def utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_context(repo_root: Path) -> dict:
    """确定性上下文提取（无 LLM）：rubric 各维度的客观事实输入。
    数据不足的事实字段直接成为该维度的负债依据——显式申报，不静默打分。"""
    root = Path(repo_root)

    def exists(*names):
        return {n: (root / n).is_file() for n in names}

    def newest(patterns):
        ts = [p.stat().st_mtime for pat in patterns for p in root.rglob(pat)
              if ".git/" not in p.as_posix()]
        return max(ts) if ts else None

    py = sorted(root.rglob("*.py"))
    stems = [p.stem for p in py]
    snake = sum(1 for s in stems if re.fullmatch(r"[a-z0-9_]+", s))
    doc_ts, code_ts = newest(["*.md"]), newest(["*.py", "*.sh"])
    lag = None
    if doc_ts and code_ts and code_ts > doc_ts:
        lag = round((code_ts - doc_ts) / 86400, 1)   # 文档落后代码天数（正=陈旧）
    readme = ""
    rp = root / "README.md"
    if rp.is_file():
        readme = rp.read_text(encoding="utf-8", errors="replace")
    # 入口清晰度事实探针：README 宣称的路径引用有多少真实存在
    claimed = set(re.findall(r"(?:pipeline|scripts|policy|standards)/[\w\-./]+", readme))
    resolved = {c for c in claimed if (root / c).exists() or (root / (c + ".py")).exists()
                or (root / (c + ".sh")).exists()}
    return {
        "schema": "verifier-rubric-shadow/context/v1",
        "repo_root": str(root),
        "locatability": {"entry_docs": exists("AGENTS.md", "README.md", "Makefile")},
        "entry_clarity": {"claimed_paths": len(claimed), "resolved_paths": len(resolved),
                          "sample_unresolved": sorted(claimed - resolved)[:5]},
        "module_depth": {"top_level_dirs": sorted(p.name for p in root.iterdir() if p.is_dir()
                                                  and p.name != ".git")},
        "naming_vocabulary": {"python_files": len(stems), "snake_case": snake,
                              "ratio": round(snake / len(stems), 3) if stems else None},
        "example_freshness": {"doc_lag_days": lag},
        "ts": utcnow_iso(),
    }


def debt_from_context(context: dict) -> list:
    """从上下文事实推导标注负债（哪些维度数据不足=负债，显式申报）。"""
    debts = []
    led = context.get("locatability", {}).get("entry_docs", {})
    if not led.get("AGENTS.md"):
        debts.append({"dimension": "locatability",
                      "reason": "仓内无 AGENTS.md 入口协议块——'从 AGENTS.md 出发'维度缺主数据源",
                      "status": "insufficient-data"})
    ef = context.get("example_freshness", {})
    if ef.get("doc_lag_days") is None:
        debts.append({"dimension": "example_freshness",
                      "reason": "示例新鲜度需示例-代码配对数据（当前仓无带执行示例集），mtime 代理不足以支撑锚档判定",
                      "status": "insufficient-data"})
    return debts


def score_replay(context: dict, fixture: dict) -> dict:
    """回放打分：fixture 提供 dimension→score；缺失维度=负债（不打分）。"""
    dims = {}
    for d in DIMENSIONS:
        v = fixture.get(d)
        if isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0:
            dims[d] = {"score": round(float(v), 3), "basis": fixture.get("fixture_id", "replay")}
        else:
            dims[d] = {"score": None, "basis": "no-data"}
    return dims


def score_api(context: dict, rubric: dict, judge_cfg: dict, repo_root: Path) -> dict:
    """真实判官：锚档→连续分。经 scripts/llm-call.sh（计量唯一入口，ADR-0048）。"""
    dims_txt = "\n".join(
        f"- {d['id']}（{d['zh']}）：{d['question']}\n  锚档：{json.dumps(d['anchors'], ensure_ascii=False)}"
        for d in rubric["dimensions"])
    prompt = (f"按以下五维 rubric 对仓库上下文打分，每维输出一行 `DIM <id> <0..1连续分>`，"
              f"并引用所依据锚档。\n{dims_txt}\n\n上下文（JSON）：\n"
              f"{json.dumps(context, ensure_ascii=False)}\n\n"
              "最后按序输出五行，格式严格为 `DIM <id> <score>`。")
    pf = Path("rubric-prompt.txt")
    pf.write_text(prompt, encoding="utf-8")
    s = judge_cfg.get("sampling", {})
    args = ["bash", str(Path(repo_root) / "scripts" / "llm-call.sh"),
            "--model", judge_cfg["model_alias"], "--prompt-file", str(pf),
            "--tag", f"rubric-shadow@{judge_cfg.get('judge_id', 'unknown')}"]
    if s.get("temperature") is not None:
        args += ["--temperature", str(s["temperature"])]
    r = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RubricError(f"llm-call.sh 失败 rc={r.returncode}: {r.stderr[:200]}")
    dims = {}
    for d in DIMENSIONS:
        m = re.search(rf"^DIM\s+{d}\s+([01](?:\.\d+)?)\s*$", r.stdout, re.M | re.I)
        dims[d] = {"score": float(m.group(1)) if m else None,
                   "basis": "api" if m else "unparseable"}
    return dims


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="AI 可读性 rubric shadow（ADR-0072 决策 5）")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--rubric", default=str(here / "rubrics" / "ai-readability-v1.yaml"))
    ap.add_argument("--scores-fixture", default=str(here / "fixtures" / "rubric-scores-ci.json"))
    ap.add_argument("--judge-config", default=None, help="api 模式的判官配置（与 run_exam 同构）")
    ap.add_argument("--out", default="rubric-shadow/records.jsonl")
    ap.add_argument("--run-id", default="local")
    args = ap.parse_args(argv)
    try:
        import yaml
        rubric = yaml.safe_load(Path(args.rubric).read_text(encoding="utf-8"))
        dim_ids = {d["id"] for d in rubric["dimensions"]}
        if dim_ids != set(DIMENSIONS):
            raise RubricError(f"rubric 维度集不符契约: {sorted(dim_ids)}")
        context = build_context(Path(args.repo_root))
        if args.judge_config:
            judge_cfg = json.loads(Path(args.judge_config).read_text(encoding="utf-8"))
            dims = score_api(context, rubric, judge_cfg, here.parents[1])
        else:
            fixture = json.loads(Path(args.scores_fixture).read_text(encoding="utf-8"))
            dims = score_replay(context, fixture)
        debt = debt_from_context(context)
        # 无分维度（fixture/上下文均无数据）同样入负债——不许静默缺维
        have = {d["dimension"] for d in debt}
        for d, v in dims.items():
            if v["score"] is None and d not in have:
                debt.append({"dimension": d, "status": "insufficient-data",
                             "reason": "该维无打分数据（回放轨道/上下文均不足以支撑锚档判定）"})
        scored = [d for d, v in dims.items() if v["score"] is not None]
        record = {
            "schema": SCHEMA, "ts": utcnow_iso(), "run_id": args.run_id,
            "rubric_id": f"{rubric['id']}/v{rubric['version']}",
            "repo": str(Path(args.repo_root).resolve()),
            "dimensions": dims,
            "dimensional_mean": (round(sum(dims[d]["score"] for d in scored) / len(scored), 3)
                                 if scored else None),
            "scored_dimensions": len(scored),
            "annotation_debt": debt,   # 负债申报字段（AC-4）：显式记录，不静默
            "annotation_debt_count": len(debt),
            "blocking": False,         # shadow：仅记录不阻断（ADR-0072 决策 5）
        }
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        print(json.dumps({k: record[k] for k in
                          ("rubric_id", "dimensional_mean", "scored_dimensions",
                           "annotation_debt_count", "blocking")}, ensure_ascii=False))
        return 0
    except RubricError as e:
        print(f"::error::rubric shadow 配置失败: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
