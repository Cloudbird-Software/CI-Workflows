#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verifier 入职考试 runner（W5-C3 .github#226 / ADR-0072；宪法 §4C 持证上岗）。

范式署名（宪法 §9 规则 #3）：LLM-as-a-Verifier（arXiv:2607.05391）；
考试集形态 = RewardBench2 生成式赛道 + LLMBar 对抗子集；双序一致率 =
MT-Bench 位置交换协议（arXiv:2306.05685）；金丝雀 = null-model 攻击实证
（arXiv:2410.07137）。考试集冻结于 archive evalsets/verifier-exam/v1/。

纪律（ADR-0072）：
- 冻结校验 fail-closed：manifest 逐文件 sha256 + freeze_hash 复算不过 = exit 2
  （考试集被篡改/漂移，不许开考——"改集=新版本号"的运行时执行点）。
- 任一分项不过 = 拒上岗 exit 1；成绩仍按 judge_id@exam_version@prompt_hash12
  落档（JSONL，模型换代/prompt 改动即重考，档案可追溯）。
- 输入隔离（对抗防御，决策 7）：黄金标签/gold_rationale/expected 永不进入判官
  输入——判官只见 prompt 与 responses/response。
- 回放模式（--judge-mode replay）：零真实 LLM——fixture 决策表模拟候选判官；
  记录 judge_mode=replay，此类成绩不可用于 verifier 持证判定（防"回放满分=持证"；
  执照注册层随 ADR-0085 退役，回放成绩仅 shadow 观察）。
- api 模式：真实判官调用一律经 scripts/llm-call.sh（一切 LLM 调用唯一计量
  入口，INV-06 / ADR-0048）；采样参数全锁定进成绩记录。

用法：
  python3 pipeline/verifier-exam/run_exam.py --exam-dir pipeline/verifier-exam/examset/v1 \
    --policy pipeline/verifier-exam/exam-policy.yaml --prompts pipeline/verifier-exam/prompts \
    --judge-config pipeline/verifier-exam/judge-configs/glm-4.5-air.json \
    --judge-mode replay --replay-fixture pipeline/verifier-exam/fixtures/judge-gold.json \
    --out verifier-exam/results
退出码：0=全过持证；1=拒上岗（分项不过）；2=配置/冻结校验失败（不许开考）。
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

VALID_PAIRWISE = ("resp0", "resp1")   # canonical 判定词表（fixture=resp0/resp1；条目 label=response0/1 归一）


class ExamError(Exception):
    """配置/冻结校验失败（exit 2）"""


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---- 冻结校验（manifest/v1）----

def compute_freeze(exam_dir: Path) -> tuple:
    """复算考试集逐文件哈希与 freeze_hash（与 archive 生成侧约定一致：
    freeze_hash = sha256(按文件名排序的 '<name> <sha256>\\n' 行)）。"""
    files = {}
    for p in sorted(exam_dir.glob("*.jsonl")):
        raw = p.read_bytes()   # 哈希按原始字节（CRLF 混入即破坏冻结——改集=新版本）
        files[p.name] = {"sha256": sha256_bytes(raw),
                         "items": sum(1 for l in raw.decode("utf-8").splitlines() if l.strip())}
    lines = "".join(f"{n} {files[n]['sha256']}\n" for n in sorted(files))
    return files, sha256_bytes(lines.encode("utf-8"))


def load_and_verify(exam_dir: Path) -> dict:
    """加载 manifest 并复算冻结哈希；任何漂移抛 ExamError（fail-closed）。"""
    mpath = exam_dir / "manifest.json"
    if not mpath.is_file():
        raise ExamError(f"manifest 缺失: {mpath}")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    files, freeze = compute_freeze(exam_dir)
    for name, info in manifest.get("files", {}).items():
        if name not in files:
            raise ExamError(f"manifest 声明的文件不存在: {name}")
        if files[name]["sha256"] != info.get("sha256"):
            raise ExamError(f"冻结校验失败: {name} 哈希不符（改集=开新版本目录，ADR-0072）")
        if files[name]["items"] != info.get("items"):
            raise ExamError(f"冻结校验失败: {name} 条目数不符")
    for name in files:
        if name not in manifest.get("files", {}):
            raise ExamError(f"未在 manifest 登记的文件: {name}")
    if freeze != manifest.get("freeze_hash"):
        raise ExamError(f"freeze_hash 不符: 复算={freeze} 声明={manifest.get('freeze_hash')}")
    return manifest


def load_items(exam_dir: Path) -> dict:
    """按文件名（去扩展）分组加载条目；只保留判官可见字段的构建原料。"""
    out = {}
    for p in sorted(exam_dir.glob("*.jsonl")):
        out[p.stem] = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]
    return out


# ---- prompt_hash（prompt 改动即重考的锚）----

def compute_prompt_hash(prompts_dir: Path, version_subdir: str) -> str:
    d = prompts_dir / version_subdir
    if not d.is_dir():
        raise ExamError(f"prompt 版本目录缺失: {d}")
    h = hashlib.sha256()
    for p in sorted(d.iterdir()):
        if not p.is_file():
            continue
        h.update(p.name.encode("utf-8")); h.update(b"\0")
        h.update(p.read_bytes()); h.update(b"\0")
    return h.hexdigest()


def archive_key(judge_id: str, exam_version: str, prompt_hash: str) -> str:
    """成绩存档键（ADR-0072 决策 2）：judge_id@版本@prompt_hash12。
    采样参数不进键（全锁定记录在档，键语义=换模型或换 prompt 即重考）。"""
    return f"{judge_id}@{exam_version}@{prompt_hash[:12]}"


# ---- 判官适配器 ----

class ReplayJudge:
    """回放判官（零真实 LLM）：fixture 决策表模拟候选判官行为。

    pairwise 取值：'resp0'/'resp1'（内容序无关=健康判官）或 'first'/'second'
    （位置策略=位置偏差靶子）；pairwise_swapped 提供换序呈现时的独立轨道。
    canary 取值：'negative'/'positive'。缺表且无 mode = 配置错误（fail-closed）。
    """

    def __init__(self, fixture: dict):
        if not isinstance(fixture, dict):
            raise ExamError("回放 fixture 必须是 JSON 对象")
        self.fixture_id = fixture.get("fixture_id", "<unnamed>")
        self.pairwise = fixture.get("pairwise") or {}
        self.pairwise_swapped = fixture.get("pairwise_swapped") or {}
        self.pairwise_mode = fixture.get("pairwise_mode")
        self.canary = fixture.get("canary") or {}
        self.canary_mode = fixture.get("canary_mode")

    def judge_pairwise(self, item: dict, order: str) -> str:
        """order: 'ab'（canonical 序）或 'ba'（位置交换序）；返回 canonical 判定。"""
        table = self.pairwise if order == "ab" else (self.pairwise_swapped or self.pairwise)
        v = table.get(item["id"], self.pairwise_mode)
        if v is None:
            raise ExamError(f"回放 fixture 未覆盖条目且无 pairwise_mode: {item['id']}")
        if v in ("resp0", "resp1"):
            return v
        if v == "first":   # 呈现位第一：ab 序=resp0，ba 序=resp1（位置偏差）
            return "resp0" if order == "ab" else "resp1"
        if v == "second":
            return "resp1" if order == "ab" else "resp0"
        raise ExamError(f"回放 fixture 非法取值 {v!r}: {item['id']}")

    def judge_canary(self, item: dict) -> str:
        v = self.canary.get(item["id"], self.canary_mode)
        if v not in ("negative", "positive"):
            raise ExamError(f"回放 fixture 金丝雀取值非法/缺失: {item['id']}")
        return v


def parse_verdict(text: str, kind: str) -> str:
    """严格输出协议解析（api 模式）：取最后一个 VERDICT 行；不可解析='unparseable'
    （计为错误——fail-closed：说不清判定=判定错误）。"""
    import re
    pat = (r"VERDICT:\s*(response0|response1)\s*$" if kind == "pairwise"
           else r"VERDICT:\s*(negative|positive)\s*$")
    hits = re.findall(pat, text.strip(), re.M | re.I)
    return hits[-1].lower() if hits else "unparseable"


class ApiJudge:
    """真实判官：经 scripts/llm-call.sh（计量唯一入口）调用 provider；采样参数锁定。"""

    def __init__(self, judge_config: dict, prompts_dir: Path, repo_root: Path):
        self.cfg = judge_config
        s = judge_config.get("sampling") or {}
        self.model = judge_config["model_alias"]
        self.base_args = ["bash", str(repo_root / "scripts" / "llm-call.sh"), "--model", self.model]
        if s.get("temperature") is not None:
            self.base_args += ["--temperature", str(s["temperature"])]
        if s.get("max_tokens") is not None:
            self.base_args += ["--max-tokens", str(s["max_tokens"])]
        if s.get("thinking"):
            self.base_args += ["--thinking", str(s["thinking"])]
        if s.get("seed") is not None:
            self.base_args += ["--seed", str(s["seed"])]
        self.tag = f"verifier-exam@{judge_config.get('judge_id')}"
        self.pw_tpl = (prompts_dir / judge_config["prompt_version"] / "pairwise-judge.md").read_text(encoding="utf-8")
        self.can_tpl = (prompts_dir / judge_config["prompt_version"] / "canary-judge.md").read_text(encoding="utf-8")
        self.tmp = Path("/tmp") if Path("/tmp").exists() else Path(".")

    def _call(self, user_text: str) -> str:
        pf = self.tmp / "verifier-exam-prompt.txt"
        pf.write_text(user_text, encoding="utf-8")
        r = subprocess.run(self.base_args + ["--prompt-file", str(pf), "--tag", self.tag],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return ""  # 调用失败=不可解析=计错（不中断整场考试，逐条记录）
        return r.stdout

    def judge_pairwise(self, item: dict, order: str) -> str:
        a, b = item["responses"] if order == "ab" else item["responses"][::-1]
        text = (self.pw_tpl.replace("{{PROMPT}}", item["prompt"])
                .replace("{{RESPONSE_A}}", a).replace("{{RESPONSE_B}}", b))
        v = parse_verdict(self._call(text), "pairwise")
        # 归一到 canonical 词表 resp0/resp1；不可解析=计错（fail-closed）
        return v.replace("response", "resp") if v != "unparseable" else "resp-none"

    def judge_canary(self, item: dict) -> str:
        text = (self.can_tpl.replace("{{PROMPT}}", item["prompt"])
                .replace("{{RESPONSE}}", item["response"]))
        v = parse_verdict(self._call(text), "canary")
        return v if v != "unparseable" else "positive"  # 判不清=不判负（fail-closed 方向）


# ---- 分项与门 ----

def _pairwise_verdicts(judge, items, order):
    return {it["id"]: judge.judge_pairwise(it, order) for it in items}


def run_sections(judge, items_by_file: dict, policy: dict) -> dict:
    rb2 = items_by_file.get("rewardbench2-generative", [])
    llm = items_by_file.get("llmbar-adversarial", [])
    canaries = items_by_file.get("null-canaries", [])
    sections = {}

    def acc(items, v_ab):
        ok = sum(1 for it in items
                 if v_ab.get(it["id"]) == ("resp0" if it["label"] == "response0" else "resp1"))
        return ok, (ok / len(items) if items else 0.0)

    v_ab = _pairwise_verdicts(judge, rb2 + llm, "ab")
    ok, rate = acc(rb2, v_ab)
    sections["rewardbench2_generative"] = {"n": len(rb2), "correct": ok, "accuracy": rate}
    ok, rate = acc(llm, v_ab)
    sections["llmbar_adversarial"] = {"n": len(llm), "correct": ok, "accuracy": rate}

    # 金丝雀（AC-2）：逐条判负断言，100% 才过
    can_v = {it["id"]: judge.judge_canary(it) for it in canaries}
    neg = sum(1 for v in can_v.values() if v == "negative")
    sections["null_canary"] = {"n": len(canaries), "negative": neg,
                               "negative_rate": (neg / len(canaries)) if canaries else 0.0}

    # 双序一致率：canonical 序 vs 位置交换序（复用 ab 序判定，另跑 ba 序）。
    # 一致计数只认双方都是合法判定且相等——垃圾判定彼此相同不算一致（保守方向）。
    v_ba = _pairwise_verdicts(judge, rb2 + llm, "ba")
    pw = rb2 + llm
    agree = sum(1 for it in pw if v_ab.get(it["id"]) in VALID_PAIRWISE
                and v_ab.get(it["id"]) == v_ba.get(it["id"]))
    sections["dual_order"] = {"n": len(pw), "agree": agree,
                              "agreement": (agree / len(pw)) if pw else 0.0}
    return sections


METRIC_KEY = {"accuracy": "accuracy", "negative_rate": "negative_rate", "agreement": "agreement"}


def gate(sections: dict, policy: dict) -> bool:
    """任一分项 value < min → False（拒上岗）。阈值真源=policy，不硬编码。"""
    for name, spec in policy.get("sections", {}).items():
        if name not in sections:
            raise ExamError(f"policy 声明的分项缺结果: {name}")
        metric = METRIC_KEY[spec["metric"]]
        sections[name]["metric"] = metric
        sections[name]["threshold"] = spec["min"]
        sections[name]["pass"] = sections[name][metric] >= spec["min"]
    return all(sections[n]["pass"] for n in policy.get("sections", {}))


def build_record(judge_config: dict, manifest: dict, prompt_hash: str, judge_mode: str,
                 sections: dict, overall: bool, run_id: str) -> dict:
    ver = manifest["version"]
    key = archive_key(judge_config["judge_id"], ver, prompt_hash)
    return {
        "schema": "verifier-exam/result/v1",
        "archive_key": key,
        "judge_id": judge_config["judge_id"],
        "model_alias": judge_config["model_alias"],
        "prompt_version": judge_config["prompt_version"],
        "prompt_hash": prompt_hash,
        "exam_version": ver,
        "frozen_exam_sha256": manifest["freeze_hash"],
        "judge_mode": judge_mode,           # replay 成绩不可注册执照（verifier-license.py 拒绝）
        "sampling": judge_config.get("sampling", {}),   # 采样参数全锁定记录
        "sections": sections,
        "overall_pass": overall,
        "run_id": run_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def write_result(out_dir: Path, record: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{record['archive_key']}.jsonl"
    with path.open("a", encoding="utf-8") as f:   # 同键多次考试=追加（历史可追溯）
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="verifier 入职考试（ADR-0072）")
    ap.add_argument("--exam-dir", default=str(here / "examset" / "v1"))
    ap.add_argument("--policy", default=str(here / "exam-policy.yaml"))
    ap.add_argument("--prompts", default=str(here / "prompts"))
    ap.add_argument("--judge-config", default=str(here / "judge-configs" / "glm-4.5-air.json"))
    ap.add_argument("--judge-mode", choices=["replay", "api"], default="replay")
    ap.add_argument("--replay-fixture", default=str(here / "fixtures" / "judge-gold.json"))
    ap.add_argument("--out", default="verifier-exam/results")
    ap.add_argument("--run-id", default="local")
    args = ap.parse_args(argv)
    try:
        import yaml  # policy/pin 解析（CI 已装；本地自测同）
        policy = yaml.safe_load(Path(args.policy).read_text(encoding="utf-8"))
        manifest = load_and_verify(Path(args.exam_dir))
        judge_config = json.loads(Path(args.judge_config).read_text(encoding="utf-8"))
        prompt_hash = compute_prompt_hash(Path(args.prompts), policy["prompt_version"])
        if args.judge_mode == "replay":
            judge = ReplayJudge(json.loads(Path(args.replay_fixture).read_text(encoding="utf-8")))
        else:
            judge = ApiJudge(judge_config, Path(args.prompts), here.parents[1])
        sections = run_sections(judge, load_items(Path(args.exam_dir)), policy)
        overall = gate(sections, policy)
        record = build_record(judge_config, manifest, prompt_hash, args.judge_mode,
                              sections, overall, args.run_id)
        path = write_result(Path(args.out), record)
        summary = {k: {"value": v[v["metric"]], "min": v["threshold"], "pass": v["pass"]}
                   for k, v in sections.items()}
        print(json.dumps({"archive_key": record["archive_key"], "overall_pass": overall,
                          "results_file": str(path), "sections": summary},
                         ensure_ascii=False, indent=2))
        if not overall:
            failed = [k for k, v in sections.items() if not v["pass"]]
            print(f"::error::拒上岗——分项不过: {failed}（ADR-0072：任一不过即拒上岗）", file=sys.stderr)
            return 1
        return 0
    except ExamError as e:
        print(f"::error::考试配置/冻结校验失败（不许开考）: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
