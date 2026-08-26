#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm_verifier.py —— LLM-as-a-Verifier 判定入口（W3-C3 .github#279，ADR-0072/0062）

接入开源 LLM-as-a-Verifier 实践：
  - 一卡一 criteria 文件，机器可追溯卡的 AC 列表；
  - 每次 run 前对 endpoint 做三探测（logprobs / top_logprobs / prefill+structured_outputs），
    不满足最低要求即 fail-closed；
  - 逐 criterion K 次重复评估，输出结构化连续分 + 阈值 gate；
  - token 账落盘，并与 ADR-0062 metering 账本交叉核对，偏差超阈值作废转人工。

运行模式：
  verify   完整判定（探测 → 评分 → 计量核对 → 报告）
  probe    仅执行三探测并写报告
  score    跳过探测，直接评分（调用方已确保 endpoint 合规）

退出码：
  0 = verdict=survived（非阻断）
  1 = verdict=insufficient 或 void（阻断/作废）
  2 = 配置/环境/探测/计量核对异常（fail-closed）
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
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # noqa: BLE001
    print("FATAL: 需要 PyYAML（pip install pyyaml==6.0.3）", file=sys.stderr)
    sys.exit(2)

# 可选 llm-verifier 库：提供 fine_grained_reward / TokenUsage 能力；
# 未安装时回退到自研解析（replay/直连均可）。
try:
    import llm_verifier as _llmv
    from llm_verifier.fine_grained_reward import call_verifier, create_openai_client
    _LLMV_AVAILABLE = True
except Exception:  # noqa: BLE001
    _LLMV_AVAILABLE = False
    _llmv = None  # type: ignore

HERE = os.path.dirname(os.path.abspath(__file__))
METERING_PY = os.path.join(HERE, "..", "metering", "metering.py")
DEFAULT_LEDGER = os.environ.get("GATE_METERING_DIR", ".metering")
DEFAULT_BASE_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
DEFAULT_MODEL = os.environ.get("LLM_MODEL") or os.environ.get("VERIFIER_MODEL", "glm-4.6")
DEFAULT_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
DEFAULT_K = 3
DEFAULT_DEVIATION_PCT = 5
DEFAULT_DEVIATION_ABS = 50
MIN_TOP_LOGPROBS = 1


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def err(msg: str) -> None:
    prefix = "::error::" if os.environ.get("CI") else "FATAL: "
    print(prefix + msg, file=sys.stderr)


def die(code: int, msg: str) -> None:
    err(msg)
    sys.exit(code)


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return sha256_bytes(f.read())


def git_head_sha(repo_dir: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        return out if len(out) == 40 else None
    except Exception:  # noqa: BLE001
        return None


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_criteria(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001
        die(2, f"criteria 文件不可读或 YAML 非法 {path}: {e}")
    if not isinstance(data, dict):
        die(2, f"criteria 文件根不是 mapping: {path}")
    return data


def safe_model_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._:-]", "-", name)


# ---------------------------------------------------------------------------
# 三探测（AC-10 / AC-1）
# ---------------------------------------------------------------------------

class EndpointProbe:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _request(self, body: dict, timeout: int = 30) -> tuple[int, dict]:
        req_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=req_body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:500]
            return e.code, {"_http_error": body_text}
        except Exception as e:  # noqa: BLE001
            return 0, {"_transport_error": str(e)}

    def probe_logprobs(self) -> dict:
        status, resp = self._request({
            "model": self.model,
            "messages": [{"role": "user", "content": "Return exactly one token."}],
            "max_tokens": 1,
            "logprobs": True,
        })
        choice = (resp.get("choices") or [{}])[0]
        lp = choice.get("logprobs")
        return {
            "status": status,
            "available": isinstance(lp, dict) and "content" in lp,
            "sample": lp,
        }

    def probe_top_logprobs(self, limit: int = 5) -> dict:
        status, resp = self._request({
            "model": self.model,
            "messages": [{"role": "user", "content": "Return exactly one token."}],
            "max_tokens": 1,
            "logprobs": True,
            "top_logprobs": limit,
        })
        choice = (resp.get("choices") or [{}])[0]
        lp = choice.get("logprobs")
        content = (lp or {}).get("content") or []
        top = []
        if content:
            top = content[0].get("top_logprobs") or []
        actual = min(len(top), limit)
        return {
            "status": status,
            "requested": limit,
            "actual": actual,
            "truncated": actual < limit,
            "supported": status == 200 and actual >= MIN_TOP_LOGPROBS,
        }

    def probe_structured_outputs(self) -> dict:
        status, resp = self._request({
            "model": self.model,
            "messages": [{"role": "user", "content": 'Return JSON {"ok": true} and nothing else.'}],
            "max_tokens": 64,
            "response_format": {"type": "json_object"},
        })
        ok = False
        if status == 200:
            try:
                text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content", "")
                parsed = json.loads(text)
                ok = parsed.get("ok") is True
            except Exception:  # noqa: BLE001
                pass
        return {"status": status, "supported": ok}

    def probe_prefill(self) -> dict:
        body = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": 'Continue the JSON: {"ok":'},
                {"role": "assistant", "content": "{"},
            ],
            "max_tokens": 8,
            "logprobs": True,
            "top_logprobs": 5,
            "extra_body": {"add_generation_prompt": False, "continue_final_message": True},
        }
        status, resp = self._request(body)
        ok = False
        if status == 200:
            text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            ok = text.startswith('"') or text.startswith("true") or text.startswith("false")
        return {"status": status, "supported": ok}

    def run(self) -> dict:
        lp = self.probe_logprobs()
        top = self.probe_top_logprobs(5)
        so = self.probe_structured_outputs()
        pf = self.probe_prefill()
        precision_note = ""
        if top.get("truncated"):
            precision_note = f"top_logprobs 被截断为 {top['actual']}/{top['requested']}，评分精度折损"
        elif not top.get("supported"):
            precision_note = "top_logprobs 不可用，fine-grained reward 退化为文本解析"
        return {
            "logprobs": lp["available"],
            "top_logprobs_limit": top["actual"],
            "top_logprobs_supported": top["supported"],
            "structured_outputs": so["supported"],
            "prefill": pf["supported"],
            "precision_note": precision_note,
            "detail": {"logprobs": lp, "top_logprobs": top, "structured_outputs": so, "prefill": pf},
        }


# ---------------------------------------------------------------------------
# LLM 调用 + 计量落账
# ---------------------------------------------------------------------------

class VerifierClient:
    def __init__(self, base_url: str, api_key: str, model: str, ledger_dir: str,
                 run_id: str, replay: dict | None = None):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.ledger_dir = ledger_dir
        self.run_id = run_id
        self.replay = replay or {}
        self.invoke_ids: list[str] = []
        self.expected_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.llmv_usage_before: dict | None = None
        if _LLMV_AVAILABLE:
            self.llmv_usage_before = _llmv.token_usage()

    def _replay_key(self, purpose: str, criterion_id: str = "", idx: int = 0) -> str:
        return f"{purpose}:{criterion_id}:{idx}"

    def _emit_metering(self, invoke_id: str, req_file: str, resp_file: str,
                       prompt_file: str, system_file: str, content_file: str,
                       ts_start: str, latency_ms: int, http_status: int,
                       exit_status: str, max_tokens: int, temperature: float | None) -> None:
        args = [
            sys.executable, METERING_PY, "emit",
            "--ledger", self.ledger_dir,
            "--invoke-id", invoke_id,
            "--role", "verifier",
            "--model", self.model,
            "--prompt-file", prompt_file,
            "--system-file", system_file,
            "--request-file", req_file,
            "--resp-file", resp_file,
            "--content-out", content_file,
            "--ts-start", ts_start,
            "--latency-ms", str(latency_ms),
            "--http-status", str(http_status),
            "--exit-status", exit_status,
            "--max-tokens", str(max_tokens),
        ]
        if temperature is not None:
            args += ["--temperature", str(temperature)]
        env = os.environ.copy()
        env["METERING_PYTHON"] = sys.executable
        try:
            subprocess.run(args, check=True, capture_output=True, text=True, env=env)
        except subprocess.CalledProcessError as e:
            die(2, f"metering emit 失败（invoke_id={invoke_id}）: {e.stderr}")

    def _call_api(self, purpose: str, messages: list[dict], criterion_id: str = "", idx: int = 0,
                  max_tokens: int = 256, temperature: float | None = None,
                  response_format: dict | None = None, logprobs: bool = True,
                  top_logprobs: int | None = None) -> dict:
        invoke_id = f"verifier-{self.run_id}-{safe_model_name(criterion_id or purpose)}-{idx}"
        self.invoke_ids.append(invoke_id)
        body: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if response_format:
            body["response_format"] = response_format
        if logprobs:
            body["logprobs"] = True
        if top_logprobs is not None:
            body["top_logprobs"] = top_logprobs

        # replay 模式：直接返回录制响应
        replay_key = self._replay_key(purpose, criterion_id, idx)
        if replay_key in self.replay:
            return dict(self.replay[replay_key])

        if not self.api_key:
            die(2, f"LLM_API_KEY 未设置，无法调用 {purpose}")

        # 若 llm-verifier 库可用且是 OpenAI 兼容端点，走库的 call_verifier 以支持 fine-grained reward
        if _LLMV_AVAILABLE and purpose == "score":
            try:
                client = create_openai_client(base_url=self.base_url, api_key=self.api_key)
                prompt_text = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
                text, tokens, position_logprobs = call_verifier(client, prompt_text, model=self.model)
                usage = _llmv.token_usage()
                delta = {
                    "prompt_tokens": usage["input_tokens"] - (self.llmv_usage_before or {}).get("input_tokens", 0),
                    "completion_tokens": usage["output_tokens"] - (self.llmv_usage_before or {}).get("output_tokens", 0),
                    "total_tokens": 0,
                }
                delta["total_tokens"] = delta["prompt_tokens"] + delta["completion_tokens"]
                self.llmv_usage_before = usage.copy()
                return {
                    "content": text,
                    "tokens": tokens,
                    "position_logprobs": position_logprobs,
                    "usage": delta,
                }
            except Exception as e:  # noqa: BLE001
                err(f"llm-verifier 库调用失败，回退到直连: {e}")

        # 直连模式
        req_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=req_body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                http_status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            http_status = e.code
            latency_ms = int((time.time() - t0) * 1000)
            return self._record_call(invoke_id, body, raw, http_status, latency_ms, max_tokens, temperature)
        except Exception as e:  # noqa: BLE001
            latency_ms = int((time.time() - t0) * 1000)
            return self._record_call(invoke_id, body, json.dumps({"_transport_error": str(e)}),
                                     0, latency_ms, max_tokens, temperature)
        latency_ms = int((time.time() - t0) * 1000)
        return self._record_call(invoke_id, body, raw, http_status, latency_ms, max_tokens, temperature)

    def _record_call(self, invoke_id: str, request_body: dict, response_text: str,
                     http_status: int, latency_ms: int, max_tokens: int,
                     temperature: float | None) -> dict:
        resp_obj = json.loads(response_text) if response_text else {}
        choice = (resp_obj.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content", "")
        usage = resp_obj.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self.expected_usage[k] += int(usage.get(k) or 0)

        ts_start = now_iso()
        tmp = tempfile.mkdtemp(prefix="llmv-")
        try:
            req_file = os.path.join(tmp, "req.json")
            resp_file = os.path.join(tmp, "resp.json")
            prompt_file = os.path.join(tmp, "prompt.txt")
            system_file = os.path.join(tmp, "system.txt")
            content_file = os.path.join(tmp, "content.txt")
            Path(req_file).write_text(json.dumps(request_body, ensure_ascii=False, separators=(",", ":")),
                                      encoding="utf-8", newline="\n")
            Path(resp_file).write_text(response_text, encoding="utf-8", newline="\n")
            prompt = "\n".join(m.get("content", "") for m in request_body.get("messages", [])
                               if m.get("role") in ("user", "system"))
            Path(prompt_file).write_text(prompt, encoding="utf-8", newline="\n")
            Path(system_file).write_text("", encoding="utf-8", newline="\n")
            Path(content_file).write_text(content, encoding="utf-8", newline="\n")
            exit_status = "ok" if 200 <= http_status < 300 else f"error:http_{http_status}"
            self._emit_metering(invoke_id, req_file, resp_file, prompt_file, system_file,
                                content_file, ts_start, latency_ms, http_status,
                                exit_status, max_tokens, temperature)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

        return {"content": content, "usage": usage, "http_status": http_status,
                "invoke_id": invoke_id}

    def score_criterion(self, criterion: dict, issue_text: str, spec_text: str,
                        k: int, idx_base: int = 0) -> dict:
        cid = criterion["id"]
        prompt_template = (
            "You are an expert verifier evaluating whether a card implementation satisfies its "
            "acceptance criterion.\n\n"
            "**Card issue:**\n{issue}\n\n"
            "**Spec excerpt:**\n{spec}\n\n"
            "**Criterion {cid} — {name}:**\n{text}\n\n"
            "Score how well the evidence satisfies this single criterion on a continuous 0.0–1.0 scale. "
            "Respond with exactly one JSON object and no other text:\n"
            '{"score": <float 0.0-1.0>, "reason": "<concise rationale>"}'
        )
        prompt = prompt_template.format(
            issue=issue_text or "(none)",
            spec=spec_text or "(none)",
            cid=cid,
            name=criterion.get("name", ""),
            text=criterion.get("text", ""),
        )
        scores = []
        reasons = []
        for i in range(k):
            response = self._call_api(
                purpose="score",
                messages=[{"role": "user", "content": prompt}],
                criterion_id=cid,
                idx=idx_base + i,
                max_tokens=256,
                temperature=0.2,
                response_format={"type": "json_object"},
                logprobs=True,
                top_logprobs=5,
            )
            content = response.get("content", "")
            parsed = self._parse_score(content)
            if parsed is None:
                die(2, f"criterion {cid} 第 {i+1}/{k} 次评分响应无法解析: {content[:200]}")
            scores.append(parsed["score"])
            reasons.append(parsed.get("reason", ""))
        aggregated = float(sum(scores)) / len(scores)
        return {"id": cid, "scores": scores, "aggregated": round(aggregated, 6),
                "reasons": reasons, "threshold": criterion.get("threshold", 0.7)}

    def _parse_score(self, text: str) -> dict | None:
        # 先尝试完整 JSON 对象
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "score" in obj:
                return {"score": float(obj["score"]), "reason": obj.get("reason", "")}
        except Exception:  # noqa: BLE001
            pass
        # 回退：从文本中提取 {"score": ...} 片段
        for m in re.finditer(r'\{[^}]*"score"\s*:\s*([0-9.]+)[^}]*\}', text):
            try:
                return {"score": float(m.group(1)), "reason": ""}
            except ValueError:
                continue
        return None

    def cross_check_usage(self, deviation_pct: int, deviation_abs: int) -> dict:
        if not os.path.isdir(self.ledger_dir):
            die(2, f"metering 账本目录不存在：{self.ledger_dir}")
        ledger_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for fn in os.listdir(self.ledger_dir):
            if not fn.startswith("records-") or not fn.endswith(".jsonl"):
                continue
            path = os.path.join(self.ledger_dir, fn)
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("role") != "verifier":
                    continue
                if rec.get("invoke_id") not in self.invoke_ids:
                    continue
                u = rec.get("usage") or {}
                ledger_total["prompt_tokens"] += int(u.get("prompt_tokens", 0))
                ledger_total["completion_tokens"] += int(u.get("completion_tokens", 0))
                ledger_total["total_tokens"] += int(u.get("total_tokens", 0))

        deviations = {}
        for k in ledger_total:
            expected = self.expected_usage[k]
            actual = ledger_total[k]
            diff = abs(expected - actual)
            pct = (diff / expected * 100) if expected else (0 if diff == 0 else 100)
            deviations[k] = {"expected": expected, "actual": actual, "diff": diff,
                             "pct": round(pct, 2), "ok": pct <= deviation_pct and diff <= deviation_abs}
        all_ok = all(v["ok"] for v in deviations.values())
        return {"ledger_dir": os.path.abspath(self.ledger_dir), "ledger_total": ledger_total,
                "expected_usage": self.expected_usage, "deviations": deviations,
                "ok": all_ok}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_report(args, criteria: dict, baseline_sha: str, probe: dict | None,
                 scores: list[dict], token_account: dict) -> dict:
    threshold_global = criteria.get("defaults", {}).get("threshold", 0.7)
    all_pass = all(s["aggregated"] >= s.get("threshold", threshold_global) for s in scores)
    verdict = "survived" if all_pass else "insufficient"
    if not token_account.get("ok"):
        verdict = "void"
    return {
        "schema": "llm-verifier-report/v1",
        "run_id": args.run_id,
        "ts": now_iso(),
        "card_id": args.card_id or criteria.get("card", ""),
        "issue": args.issue or criteria.get("card", ""),
        "spec_path": args.spec_path or criteria.get("spec", ""),
        "criteria_file": os.path.abspath(args.criteria),
        "criteria_sha256": sha256_file(args.criteria),
        "baseline_sha": baseline_sha,
        "probe": probe,
        "k": args.k,
        "pivots": args.pivots,
        "criteria_scores": scores,
        "threshold_global": threshold_global,
        "verdict": verdict,
        "blocking": verdict != "survived",
        "token_account": token_account,
        "ledger_dir": os.path.abspath(args.ledger_dir),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="llm_verifier.py", description="LLM-as-a-Verifier 判定入口")
    ap.add_argument("cmd", choices=["verify", "probe", "score"])
    ap.add_argument("--criteria", default="", help="criteria YAML 文件路径")
    ap.add_argument("--issue-body", default="", help="issue body 文本文件路径")
    ap.add_argument("--spec-path", default="", help="spec 文件路径")
    ap.add_argument("--card-id", default="", help="卡 ID")
    ap.add_argument("--run-id", default="", help="run id（默认 时间戳）")
    ap.add_argument("--repo-dir", default=".", help="已检出仓库根目录（用于基准 SHA）")
    ap.add_argument("--ledger-dir", default=DEFAULT_LEDGER, help="metering 账本目录")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenAI 兼容 endpoint")
    ap.add_argument("--api-key", default=DEFAULT_API_KEY, help="LLM API key")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="模型名")
    ap.add_argument("--k", type=int, default=DEFAULT_K, help="每 criterion 重复评估次数")
    ap.add_argument("--pivots", type=int, default=1, help="成本旋钮：pivots（当前保留为记录）")
    ap.add_argument("--replay-file", default="", help="离线回放 JSON（自测/审计重放）")
    ap.add_argument("--deviation-pct", type=int, default=DEFAULT_DEVIATION_PCT,
                    help="token 账相对偏差阈值（%%）")
    ap.add_argument("--deviation-abs", type=int, default=DEFAULT_DEVIATION_ABS,
                    help="token 账绝对偏差阈值（tokens）")
    ap.add_argument("--report-out", default="llm-verifier-report.json", help="报告输出路径")
    ap.add_argument("--skip-probe", action="store_true", help="verify 时跳过探测（仅调试用）")
    args = ap.parse_args(argv)

    if not args.run_id:
        args.run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    criteria = {}
    if args.cmd in ("verify", "score"):
        if not args.criteria:
            die(2, "verify/score 模式需要 --criteria")
        criteria = load_criteria(args.criteria)

    baseline_sha = git_head_sha(args.repo_dir) or ""
    if not baseline_sha:
        die(2, f"无法获取仓库 HEAD SHA（repo_dir={args.repo_dir}），基准为空 fail-closed")

    replay = load_json(args.replay_file) if args.replay_file else None

    probe = None
    if args.cmd == "verify" and not args.skip_probe:
        if replay and "probe" in replay:
            probe = replay["probe"]
        else:
            probe = EndpointProbe(args.base_url, args.api_key, args.model).run()
        if not probe.get("logprobs"):
            report = build_report(args, criteria, baseline_sha, probe, [], {
                "ok": False, "reason": "endpoint 不返回 logprobs，fail-closed",
                "ledger_dir": os.path.abspath(args.ledger_dir),
            })
            Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report_out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                             encoding="utf-8", newline="\n")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            err("endpoint 不返回 logprobs —— AC-1 fail-closed")
            return 2
        if not probe.get("top_logprobs_supported"):
            err("endpoint top_logprobs 不满足最低要求 —— AC-10 fail-closed")
            return 2
        if not (probe.get("prefill") or probe.get("structured_outputs")):
            err("endpoint 既不支持 prefill 也不支持 structured_outputs —— AC-10 fail-closed")
            return 2

    if args.cmd == "probe":
        probe = EndpointProbe(args.base_url, args.api_key, args.model).run()
        Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_out).write_text(json.dumps(probe, ensure_ascii=False, indent=2),
                                         encoding="utf-8", newline="\n")
        print(json.dumps(probe, ensure_ascii=False, indent=2))
        return 0 if probe["logprobs"] and probe["top_logprobs_supported"] else 2

    issue_text = ""
    if args.issue_body:
        try:
            issue_text = Path(args.issue_body).read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            die(2, f"issue body 不可读: {e}")
    spec_text = ""
    if args.spec_path:
        try:
            spec_text = Path(args.spec_path).read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            die(2, f"spec 不可读: {e}")

    client = VerifierClient(args.base_url, args.api_key, args.model, args.ledger_dir,
                            args.run_id, replay=replay)

    scores: list[dict] = []
    for criterion in criteria.get("criteria", []):
        scores.append(client.score_criterion(criterion, issue_text, spec_text, args.k))

    token_account = client.cross_check_usage(args.deviation_pct, args.deviation_abs)
    token_account["invoke_ids"] = client.invoke_ids
    if not token_account["ok"]:
        token_account["reason"] = (
            f"token 账与 metering 偏差超阈值（相对>{args.deviation_pct}% 或绝对>{args.deviation_abs} tokens）"
        )

    report = build_report(args, criteria, baseline_sha, probe, scores, token_account)
    Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report_out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not token_account["ok"]:
        err(token_account["reason"])
        return 2
    return 0 if report["verdict"] == "survived" else 1


if __name__ == "__main__":
    sys.exit(main())
