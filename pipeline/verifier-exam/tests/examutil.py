# -*- coding: utf-8 -*-
"""测试共享件：合成考试集/fixture 构造器（零真实 LLM——全部回放）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_exam  # noqa: E402

HERE = Path(__file__).resolve().parents[1]


def synth_exam(tmp: Path, n_rb2=20, n_llm=20, n_canary=24, prefix="s") -> Path:
    """构造合成考试集（含按同一约定复算的 manifest）——分项逻辑测试的基底。"""
    d = tmp / f"exam-{prefix}"
    d.mkdir(parents=True, exist_ok=True)

    def write(name, items):
        body = "".join(json.dumps(it, ensure_ascii=False, sort_keys=True) + "\n" for it in items)
        (d / name).write_text(body, encoding="utf-8")

    rb2 = [{"id": f"{prefix}rb-{i:04d}", "subset": "rewardbench2-generative", "category": "synth",
            "prompt": f"合成任务 {i}", "responses": [f"正确回答 {i}", f"错误回答 {i}"],
            "label": "response0"} for i in range(n_rb2)]
    llm = [{"id": f"{prefix}ll-{i:04d}", "subset": "llmbar-adversarial",
            "adversarial_form": "obvious" if i % 2 else "subtle",
            "prompt": f"对抗任务 {i}", "responses": [f"实质正确 {i}", f"花哨错误 {i}"],
            "label": "response0"} for i in range(n_llm)]
    can = [{"id": f"{prefix}nc-{i:04d}", "subset": "null-canary",
            "canary_type": ["empty-response", "template-parrot", "refusal"][i % 3],
            "prompt": f"良性任务 {i}", "response": "", "expected": "negative"}
           for i in range(n_canary)]
    write("rewardbench2-generative.jsonl", rb2)
    write("llmbar-adversarial.jsonl", llm)
    write("null-canaries.jsonl", can)
    files, freeze = run_exam.compute_freeze(d)
    (d / "manifest.json").write_text(json.dumps(
        {"schema": "verifier-exam/manifest/v1", "exam_id": "synth", "version": "9.9.9",
         "files": files, "freeze_hash": freeze}, ensure_ascii=False), encoding="utf-8")
    return d


def gold_fixture(items_by_file: dict, flip_canary: str = None, swap_flips: int = 0) -> dict:
    """由条目黄金标签构造满分回放轨道；flip_canary=翻转一只金丝雀；
    swap_flips=换序呈现时翻转前 k 条成对判定（双序噪声注入器）。"""
    fx = {"fixture_id": "synth-gold", "pairwise": {}, "canary": {}}
    for it in items_by_file.get("rewardbench2-generative", []) + items_by_file.get("llmbar-adversarial", []):
        fx["pairwise"][it["id"]] = "resp0" if it["label"] == "response0" else "resp1"
    for it in items_by_file.get("null-canaries", []):
        fx["canary"][it["id"]] = "negative"
    if flip_canary:
        fx["fixture_id"] = "synth-canary-miss"
        fx["canary"][flip_canary] = "positive"
    if swap_flips:
        fx["fixture_id"] = f"synth-order-noise-{swap_flips}"
        swapped = dict(fx["pairwise"])
        for iid in list(swapped)[:swap_flips]:
            swapped[iid] = "resp1" if swapped[iid] == "resp0" else "resp0"
        fx["pairwise_swapped"] = swapped
    return fx


def write_json(path: Path, obj: dict) -> Path:
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


def judge_config(tmp: Path, judge_id="synth-judge", **sampling) -> Path:
    cfg = {"judge_id": judge_id, "model_alias": "synth-model", "prompt_version": "v1",
           "sampling": {"temperature": 0.0, "top_p": 1.0, "max_tokens": 256,
                        "thinking": "disabled", "seed": 7, **sampling}}
    return write_json(tmp / f"judge-{judge_id}.json", cfg)


def prompts_dir(tmp: Path, mutate=False) -> Path:
    """prompt 版本目录（拷真源；mutate=True 改一字节——prompt 改动即重考测试）。"""
    src = HERE / "prompts" / "v1"
    dst = tmp / "prompts" / "v1"
    dst.mkdir(parents=True, exist_ok=True)
    for p in src.iterdir():
        text = p.read_text(encoding="utf-8")
        if mutate and p.name == "pairwise-judge.md":
            text = text.replace("裁判", "裁判（校订）")
        (dst / p.name).write_text(text, encoding="utf-8")
    return dst.parent


def run_main(exam_dir: Path, cfg: Path, fixture: Path, out: Path,
             prompts: Path = None, policy: Path = None, run_id="test") -> tuple:
    argv = ["--exam-dir", str(exam_dir), "--policy", str(policy or HERE / "exam-policy.yaml"),
            "--prompts", str(prompts or HERE / "prompts"), "--judge-config", str(cfg),
            "--judge-mode", "replay", "--replay-fixture", str(fixture),
            "--out", str(out), "--run-id", run_id]
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_exam.main(argv)
    return rc, buf.getvalue()


def load_record(out: Path) -> dict:
    files = list(out.glob("*.jsonl"))
    assert len(files) == 1, f"应恰有一个成绩存档文件: {files}"
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])
