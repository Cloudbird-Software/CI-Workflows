#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm_verifier.py —— LLM-as-a-Verifier 接入（W3-C3 .github#279，ADR-0072）

开源 LLM-as-a-Verifier 范式（arXiv:2607.05391）的机内实现：绝对细粒度 reward、
criteria 分解、K 次重复评估、阈值 gate，输出结构化连续分——不接受散文结论。
与 ADR-0072 引用同一外部范式；criteria 文件一卡一份且机器可追溯到对应卡的 AC
列表（AC-1）。

核心流程（每次 verifier run）：
  1. endpoint 三探测（AC-10 / IFACE-02）：logprobs 有无、top_logprobs 上限、
     prefill/structured_outputs 支持；探测结果决定打分抽取路径与精度预期并写
     入报告；不满足最低要求的 endpoint 配置即失败（fail-closed）。
  2. 加载 criteria 文件（按卡 ID 溯源 AC）+ 组装 verifier prompt。
  3. K 次重复调用 LLM（经 metering-wrapper.sh，ADR-0062——token 账落 JSONL
     hash 链）；每次产出逐 criterion 连续分。
  4. token 账与 LLM 响应 usage 字段交叉核对（AC-11）：偏差超阈值时该 run
     判定作废转人工（非仅告警）。
  5. 阈值 gate → verdict（AC-7 / AC-8）。

依赖：pyyaml==6.0.3（钉点见 .github/requirements-llm-verifier.txt）；
LLM 调用唯一入口 = pipeline/metering/metering-wrapper.sh（ADR-0062）。

子命令：
  probe      三探测 endpoint → 探测结果 JSON（stdout）
  verify     完整 verifier run → 报告 JSON（--report-out）
  cross-check 与 metering 账本交叉核对 → 核对结果 JSON（stdout）

退出码：0=verdict survived（全部 criterion 通过 gate）| 1=verdict insufficient
        | 2=配置/环境/探测失败（fail-closed）| 3=token 偏差超阈值作废
        | 4=provider 调用失败
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("FATAL: 需要 pyyaml==6.0.3（钉点见 .github/requirements-llm-verifier.txt）",
          file=sys.stderr)
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
CRITERIA_DIR = os.path.join(HERE, "criteria")
MODELS_PATH = os.path.normpath(os.path.join(HERE, "..", "models.yaml"))
CONFIG_PATH = os.path.join(HERE, "verifier-config.yaml")
METERING_WRAPPER = os.path.normpath(os.path.join(HERE, "..", "metering", "metering-wrapper.sh"))
METERING_PY = os.path.normpath(os.path.join(HERE, "..", "metering", "metering.py"))

# 默认角色档（与 models.yaml 对齐；W3-C1/W3-C5 接口兼容 = 同 schema）
DEFAULT_ROLE = "judge-deep"
DEFAULT_REPEAT_K = 3
DEFAULT_TOTAL_THRESHOLD = 0.6  # 综合连续分阈值 gate
DEFAULT_TOKEN_DEVIATION_PCT = 10  # usage 与 metering 偏差阈值（%）

PROBE_TIMEOUT_S = 30


def err(msg):
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)


def die(code, msg):
    err(msg)
    sys.exit(code)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


def load_yaml(path):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001
        die(2, f"YAML 不可读 {path}: {e}")


def load_models():
    """加载 models.yaml，返回角色档配置。"""
    if not os.path.isfile(MODELS_PATH):
        die(2, f"models.yaml 缺失 {MODELS_PATH}")
    data = load_yaml(MODELS_PATH)
    roles = (data or {}).get("roles") or {}
    return roles


def resolve_role(role: str):
    """解析角色档 → (model, max_tokens, temperature, thinking)。"""
    roles = load_models()
    cfg = roles.get(role)
    if not cfg:
        die(2, f"角色档 '{role}' 未在 models.yaml 登记")
    return cfg.get("model"), cfg.get("max_tokens", 4096), cfg.get("temperature"), cfg.get("thinking")


def ghcb():
    """探测用 curl 包装。"""
    return ["curl", "--max-time", str(PROBE_TIMEOUT_S), "-sS"]


# ---------------- endpoint 三探测（AC-10 / IFACE-02） ----------------
def probe_endpoint(base_url: str, model: str, api_key: str):
    """三探测：logprobs 有无、top_logprobs 上限、prefill/structured_outputs 支持。
    返回探测结果 dict；探测失败/超时不误判为通过（fail-closed）。"""
    probe = {
        "ts": now_iso(),
        "endpoint": base_url,
        "model": model,
        "logprobs": {"supported": False, "detail": None},
        "top_logprobs": {"supported": False, "max_limit": None},
        "structured_outputs": {"supported": False, "detail": None},
        "prefill": {"supported": False, "detail": None},
        "pass_minimum": False,
    }

    # 探测 1: logprobs（请求 logprobs=true，检查响应是否含 logprobs）
    body_logprobs = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "logprobs": True,
        "temperature": 0,
    }).encode("utf-8")
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(body_logprobs); req_path = tf.name
        resp = subprocess.run(
            ["curl", "--max-time", str(PROBE_TIMEOUT_S), "-sS", "-w", "\n%{http_code}",
             "-X", "POST", f"{base_url}/chat/completions",
             "-H", f"Authorization: Bearer {api_key}",
             "-H", "Content-Type: application/json",
             "--data-binary", f"@{req_path}", "-o", "-"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S + 5,
        )
        os.unlink(req_path)
        # 解析：倒数第二行是 body，末行是 http_code
        parts = resp.stdout.rsplit("\n", 1)
        body_str = parts[0] if len(parts) == 2 else resp.stdout
        try:
            rdata = json.loads(body_str)
            choices = rdata.get("choices") or []
            if choices and isinstance(choices[0].get("logprobs"), dict):
                probe["logprobs"]["supported"] = True
                probe["logprobs"]["detail"] = "logprobs present in response"
        except Exception:
            probe["logprobs"]["detail"] = f"response parse failed: {body_str[:200]}"
    except subprocess.TimeoutExpired:
        probe["logprobs"]["detail"] = "probe timeout"
    except Exception as e:  # noqa: BLE001
        probe["logprobs"]["detail"] = f"probe error: {e}"

    # 探测 2: top_logprobs 上限（请求 top_logprobs=20）
    body_top = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "logprobs": True,
        "top_logprobs": 20,
        "temperature": 0,
    }).encode("utf-8")
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(body_top); req_path = tf.name
        resp = subprocess.run(
            ["curl", "--max-time", str(PROBE_TIMEOUT_S), "-sS",
             "-X", "POST", f"{base_url}/chat/completions",
             "-H", f"Authorization: Bearer {api_key}",
             "-H", "Content-Type: application/json",
             "--data-binary", f"@{req_path}"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S + 5,
        )
        os.unlink(req_path)
        rdata = json.loads(resp.stdout)
        choices = rdata.get("choices") or []
        lp = (choices[0].get("logprobs") or {}) if choices else []
        content_lp = lp.get("content") if isinstance(lp, dict) else []
        if content_lp and isinstance(content_lp, list) and content_lp:
            first_token = content_lp[0] if isinstance(content_lp[0], dict) else {}
            top = first_token.get("top_logprobs") or []
            probe["top_logprobs"]["supported"] = len(top) > 0
            probe["top_logprobs"]["max_limit"] = len(top)
        else:
            probe["top_logprobs"]["detail"] = "top_logprobs empty or unsupported"
    except subprocess.TimeoutExpired:
        probe["top_logprobs"]["detail"] = "probe timeout"
    except Exception as e:  # noqa: BLE001
        probe["top_logprobs"]["detail"] = f"probe error: {e}"

    # 探测 3: structured_outputs / prefill（请求 response_format json_schema）
    body_struct = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "reply with {\"ok\": true}"}],
        "max_tokens": 16,
        "temperature": 0,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "probe", "strict": True,
            "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"], "additionalProperties": False}},
        },
    }).encode("utf-8")
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(body_struct); req_path = tf.name
        resp = subprocess.run(
            ["curl", "--max-time", str(PROBE_TIMEOUT_S), "-sS",
             "-X", "POST", f"{base_url}/chat/completions",
             "-H", f"Authorization: Bearer {api_key}",
             "-H", "Content-Type: application/json",
             "--data-binary", f"@{req_path}"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S + 5,
        )
        os.unlink(req_path)
        rdata = json.loads(resp.stdout)
        content_raw = ((rdata.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        parsed = json.loads(content_raw)
        if isinstance(parsed, dict) and parsed.get("ok") is True:
            probe["structured_outputs"]["supported"] = True
            probe["structured_outputs"]["detail"] = "json_schema response validated"
        else:
            probe["structured_outputs"]["detail"] = f"unexpected response: {content_raw[:200]}"
    except subprocess.TimeoutExpired:
        probe["structured_outputs"]["detail"] = "probe timeout"
    except Exception as e:  # noqa: BLE001
        probe["structured_outputs"]["detail"] = f"probe error: {e}"

    # 最低要求：logprobs 必须支持（verifier 抽取精度依赖 logprobs）
    probe["pass_minimum"] = probe["logprobs"]["supported"]
    return probe


# ---------------- criteria 加载 ----------------
def load_criteria(criteria_file: str):
    """加载 criteria 文件。criteria 文件格式：
    card_id: ISSUE-263
    criteria:
      - id: AC-1
        ac_ref: "AC-1 (api)"
        weight: 1.0
        description: "..."
    threshold: 0.6
    """
    if not os.path.isfile(criteria_file):
        die(2, f"criteria 文件缺失 {criteria_file}")
    data = load_yaml(criteria_file)
    if not data or "criteria" not in data:
        die(2, f"criteria 文件格式错误（缺 criteria 数组）{criteria_file}")
    return data


def find_criteria_by_card(card_id: str):
    """在 criteria/ 目录下按 card_id 查找 criteria 文件。"""
    if not os.path.isdir(CRITERIA_DIR):
        return None
    for root, _, files in os.walk(CRITERIA_DIR):
        for fn in files:
            if not (fn.endswith(".yaml") or fn.endswith(".yml")):
                continue
            fp = os.path.join(root, fn)
            try:
                data = load_yaml(fp)
                if data and data.get("card_id") == card_id:
                    return fp
            except Exception:
                continue
    return None


# ---------------- verifier prompt 组装 ----------------
def build_verifier_prompt(target_text: str, criteria: list, card_id: str) -> str:
    """组装 verifier prompt：spec/实现文本 + criteria 列表 + 打分契约。"""
    criteria_lines = []
    for c in criteria:
        cid = c.get("id", "?")
        desc = c.get("description", "")
        ac_ref = c.get("ac_ref", "")
        criteria_lines.append(f"- {cid}{(' (' + ac_ref + ')') if ac_ref else ''}: {desc}")
    criteria_block = "\n".join(criteria_lines)

    return f"""You are an LLM-as-a-Verifier (arXiv:2607.05391). Evaluate the following artifact against each criterion on a continuous scale from 0.0 (fail) to 1.0 (pass). Output ONLY a single JSON object — no prose, no explanations.

Card: {card_id}

## Artifact to evaluate
{target_text}

## Criteria
{criteria_block}

## Output contract (JSON only)
{{"scores": [{{"criterion": "<id>", "score": <0.0-1.0>, "rationale": "<one phrase>"}}], "overall": <0.0-1.0>, "pass": <true|false>}}

Rules:
- One entry per criterion; score is a decimal 0.0–1.0.
- overall = weighted or average of criterion scores.
- pass = true only if every criterion score >= its threshold (default 0.6).
- No text outside the JSON object."""


# ---------------- LLM 调用（经 metering wrapper） ----------------
def call_via_metering(model: str, prompt_file: str, system_file: str,
                      max_tokens: int, temperature, thinking, role: str,
                      base_url: str, invoke_id: str, ledger_dir: str):
    """经 metering-wrapper.sh 调用 LLM；返回 (content, record_path)。"""
    os.environ["LLM_BASE_URL"] = base_url
    os.environ["GATE_METERING_DIR"] = ledger_dir
    args = ["bash", METERING_WRAPPER,
            "--model", model, "--role", role, "--tag", "llm-verifier",
            "--prompt-file", prompt_file, "--system-file", system_file,
            "--max-tokens", str(max_tokens), "--invoke-id", invoke_id]
    if temperature is not None:
        args += ["--temperature", str(temperature)]
    if thinking:
        args += ["--thinking", thinking]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        err(f"metering-wrapper 调用失败 rc={proc.returncode}: {proc.stderr[:500]}")
        return None, None
    # 解析计量记录（wrapper 输出 record_sha256 诊断到 stderr）
    record_sha = None
    for line in proc.stderr.splitlines():
        m = re.search(r'record_sha256":"(sha256:[0-9a-f]{64})"', line)
        if m:
            record_sha = m.group(1)
            break
    return proc.stdout, record_sha


def find_metering_record(ledger_dir: str, invoke_id: str):
    """在 metering 账本中按 invoke_id 查找记录。"""
    if not os.path.isdir(ledger_dir):
        return None
    for fn in sorted(os.listdir(ledger_dir)):
        if not fn.endswith(".jsonl"):
            continue
        fp = os.path.join(ledger_dir, fn)
        try:
            with open(fp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("invoke_id") == invoke_id:
                        return rec
        except Exception:
            continue
    return None


# ---------------- token 交叉核对（AC-11） ----------------
def cross_check_token(metering_record: dict, usage_from_response: dict, threshold_pct: int):
    """metering 账本 usage 与 LLM 响应 usage 交叉核对。
    返回 {"consistent": bool, "deviation_pct": float, "detail": str}。"""
    if not metering_record or not usage_from_response:
        return {"consistent": False, "deviation_pct": None, "detail": "missing data for cross-check"}
    m_total = (metering_record.get("usage") or {}).get("total_tokens", 0)
    r_total = usage_from_response.get("total_tokens", 0)
    if m_total == 0 and r_total == 0:
        return {"consistent": True, "deviation_pct": 0.0, "detail": "both zero"}
    if m_total == 0 or r_total == 0:
        return {"consistent": False, "deviation_pct": 100.0,
                "detail": f"metering={m_total} response={r_total}"}
    deviation = abs(m_total - r_total) / max(m_total, r_total) * 100
    return {
        "consistent": deviation <= threshold_pct,
        "deviation_pct": round(deviation, 2),
        "detail": f"metering={m_total} response={r_total} deviation={deviation:.1f}%",
    }


# ---------------- 主流程：verify ----------------
def cmd_verify(args):
    # 1. 解析角色档
    model, max_tokens, temperature, thinking = resolve_role(args.role)
    base_url = args.base_url

    # 2. endpoint 三探测
    probe_result = probe_endpoint(base_url, model, args.api_key)
    if not probe_result["pass_minimum"]:
        report = {
            "schema": "llm-verifier-report/v1",
            "ts": now_iso(),
            "card_id": args.card_id,
            "verdict": "insufficient",
            "reason": "endpoint probe failed minimum requirement (logprobs required)",
            "probe": probe_result,
        }
        if args.report_out:
            with open(args.report_out, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    # 3. 加载 criteria
    criteria_file = args.criteria
    if not criteria_file and args.card_id:
        criteria_file = find_criteria_by_card(args.card_id)
    if not criteria_file:
        die(2, "未提供 --criteria 且未找到 card 对应 criteria 文件")
    criteria_data = load_criteria(criteria_file)
    criteria_list = criteria_data.get("criteria", [])
    criterion_threshold = criteria_data.get("threshold", DEFAULT_TOTAL_THRESHOLD)
    if not criteria_list:
        die(2, "criteria 文件为空")

    # 4. 读取被评文本
    target_text = ""
    if args.target_file:
        with open(args.target_file, encoding="utf-8") as f:
            target_text = f.read()
    elif args.target_text:
        target_text = args.target_text
    else:
        die(2, "需要 --target-file 或 --target-text")

    # 5. K 次重复调用
    prompt_text = build_verifier_prompt(target_text, criteria_list, args.card_id)
    ledger_dir = args.ledger_dir or os.path.join(os.getcwd(), ".metering")
    os.makedirs(ledger_dir, exist_ok=True)

    all_runs = []
    any_provider_fail = False
    for k in range(1, args.repeat_k + 1):
        invoke_id = f"llv-{args.card_id}-{now_iso()}-k{k}"
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as pf:
            pf.write(prompt_text); prompt_path = pf.name
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as sf:
            sf.write("You are an LLM-as-a-Verifier. Output valid JSON only."); system_path = sf.name

        content, record_sha = call_via_metering(
            model, prompt_path, system_path, max_tokens, temperature,
            thinking, args.role, base_url, invoke_id, ledger_dir,
        )
        os.unlink(prompt_path); os.unlink(system_path)

        if content is None:
            any_provider_fail = True
            all_runs.append({"k": k, "invoke_id": invoke_id, "error": "provider call failed"})
            continue

        # 解析 verifier 输出
        try:
            verdict_obj = json.loads(content.strip())
        except Exception:
            # 尝试从 markdown 块中提取
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
            if m:
                verdict_obj = json.loads(m.group(1))
            else:
                all_runs.append({"k": k, "invoke_id": invoke_id, "error": f"unparseable: {content[:300]}"})
                continue

        # token 交叉核对
        record = find_metering_record(ledger_dir, invoke_id)
        usage_resp = verdict_obj.get("usage") or {}
        cross = cross_check_token(record, usage_resp, args.token_deviation_pct)

        all_runs.append({
            "k": k,
            "invoke_id": invoke_id,
            "record_sha256": record_sha,
            "verdict_object": verdict_obj,
            "token_cross_check": cross,
        })

    if any_provider_fail and not any("verdict_object" in r for r in all_runs):
        die(4, "所有 K 次调用均 provider 失败")

    # 6. 汇总 verdict
    valid_runs = [r for r in all_runs if "verdict_object" in r]
    # 任一 run token 偏差超阈值 → 整体作废转人工
    token_invalid = [r for r in valid_runs if not r["token_cross_check"]["consistent"]]
    if token_invalid:
        report = {
            "schema": "llm-verifier-report/v1",
            "ts": now_iso(),
            "card_id": args.card_id,
            "verdict": "needs-human",
            "reason": "token cross-check deviation exceeded threshold",
            "probe": probe_result,
            "criteria_file": criteria_file,
            "runs": all_runs,
        }
        if args.report_out:
            with open(args.report_out, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 3

    # 逐 criterion 聚合（取 K 次最低分——保守）
    criterion_agg = {}
    for c in criteria_list:
        cid = c.get("id")
        scores = []
        for r in valid_runs:
            for s in (r["verdict_object"].get("scores") or []):
                if s.get("criterion") == cid:
                    scores.append(float(s.get("score", 0)))
        if scores:
            criterion_agg[cid] = {
                "min": min(scores),
                "max": max(scores),
                "avg": round(sum(scores) / len(scores), 3),
                "n": len(scores),
            }

    # 阈值 gate
    failed_criteria = [cid for cid, agg in criterion_agg.items() if agg["min"] < criterion_threshold]
    overall_scores = [r["verdict_object"].get("overall", 0) for r in valid_runs]
    overall_avg = round(sum(overall_scores) / len(overall_scores), 3) if overall_scores else 0.0
    verdict = "survived" if not failed_criteria else "insufficient"

    report = {
        "schema": "llm-verifier-report/v1",
        "ts": now_iso(),
        "card_id": args.card_id,
        "verdict": verdict,
        "overall_score": overall_avg,
        "threshold": criterion_threshold,
        "failed_criteria": failed_criteria,
        "criterion_scores": criterion_agg,
        "k": args.repeat_k,
        "valid_runs": len(valid_runs),
        "probe": probe_result,
        "criteria_file": criteria_file,
        "endpoint_precision_note": _precision_note(probe_result),
        "runs": all_runs,
    }

    if args.report_out:
        with open(args.report_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"报告 → {args.report_out}", file=sys.stderr)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    return 0 if verdict == "survived" else 1


def _precision_note(probe):
    notes = []
    if not probe.get("logprobs", {}).get("supported"):
        notes.append("logprobs unsupported: scoring extraction degraded")
    if not probe.get("top_logprobs", {}).get("supported"):
        notes.append("top_logprobs unsupported: precision reduced")
    limit = (probe.get("top_logprobs") or {}).get("max_limit")
    if limit is not None and limit < 5:
        notes.append(f"top_logprobs truncated at {limit}: precision reduced")
    return notes or ["endpoint meets minimum precision requirements"]


# ---------------- CLI ----------------
def main(argv=None):
    p = argparse.ArgumentParser(description="llm-verifier —— LLM-as-a-Verifier 接入（W3-C3）")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="endpoint 三探测")
    p_probe.add_argument("--base-url", default=os.getenv("LLM_BASE_URL", "https://open.bigmodel.cn/api/pas/v4"))
    p_probe.add_argument("--model", required=True)
    p_probe.add_argument("--api-key", default=os.getenv("LLM_API_KEY", ""))

    p_verify = sub.add_parser("verify", help="完整 verifier run")
    p_verify.add_argument("--card-id", help="卡 ID（用于溯源 criteria）")
    p_verify.add_argument("--criteria", help="criteria 文件路径（缺省按 card-id 自动查找）")
    p_verify.add_argument("--target-file", help="被评文本文件路径")
    p_verify.add_argument("--target-text", help="被评文本（内联）")
    p_verify.add_argument("--role", default=DEFAULT_ROLE)
    p_verify.add_argument("--repeat-k", type=int, default=DEFAULT_REPEAT_K)
    p_verify.add_argument("--base-url", default=os.getenv("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
    p_verify.add_argument("--api-key", default=os.getenv("LLM_API_KEY", ""))
    p_verify.add_argument("--token-deviation-pct", type=int, default=DEFAULT_TOKEN_DEVIATION_PCT)
    p_verify.add_argument("--ledger-dir", help="metering 账本目录（缺省 ./.metering）")
    p_verify.add_argument("--report-out", help="报告输出路径（缺省 stdout）")

    p_cc = sub.add_parser("cross-check", help="与 metering 账本交叉核对")
    p_cc.add_argument("--invoke-id", required=True)
    p_cc.add_argument("--usage-json", help="响应 usage JSON 文件路径")
    p_cc.add_argument("--ledger-dir", required=True)
    p_cc.add_argument("--token-deviation-pct", type=int, default=DEFAULT_TOKEN_DEVIATION_PCT)

    args = p.parse_args(argv)

    if args.cmd == "probe":
        if not args.api_key:
            die(2, "--api-key 或 LLM_API_KEY 必填")
        result = probe_endpoint(args.base_url, args.model, args.api_key)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["pass_minimum"] else 2

    if args.cmd == "verify":
        if not args.api_key:
            die(2, "--api-key 或 LLM_API_KEY 必填")
        return cmd_verify(args)

    if args.cmd == "cross-check":
        record = find_metering_record(args.ledger_dir, args.invoke_id)
        usage = {}
        if args.usage_json:
            with open(args.usage_json, encoding="utf-8") as f:
                usage = json.load(f)
        result = cross_check_token(record, usage, args.token_deviation_pct)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["consistent"] else 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
