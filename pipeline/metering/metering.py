#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metering.py —— 计量 wrapper 核心（W2-C3 .github#216，ADR-0062）

宪法 §4A/BEH-09 的机内实现。一切 LLM 调用经 metering-wrapper.sh 落到本模块：

子命令（bash 入口见 metering-wrapper.sh / metering-verify.sh / ledger-sync.sh）：
  mkreq     组装 provider 请求体（stdout 单行 JSON）——wrapper 用作 curl -d
  emit      一次 invoke 恰一条记录：解析响应（流式分片在此聚合——spike T8 教训，
            分片绝不单独成记录）、过 record.schema.json 断言、挂 hash 链追加 JSONL、
            可选产出 llm-usage/v1 兼容件（spec-pr.py 等既有下游零改动）
  verify    验链：逐条重算 record_sha256 + prev 链 + schema + invoke_id 去重
  aggregate 按角色档归账（cost-check LLM 预算通道数据源，AC-4）；先验链后归账
  relink    本地账本续接到远端基链（ledger-sync 合并用；两侧先各自验链）

账本形态：目录内 records-<ISO 周>.jsonl，每周片一条链（可独立验证）。
fail-closed：schema 不过/链断/账本不可读 → 非零退出，不静默降级。

退出码：0=成功 | 2=参数/环境错误 | 3=账本/记录无效（不可信数据不落链不入账）
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "record.schema.json")
RECORD_SCHEMA_ID = "metering-record/v1"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


def sha256_file(path):
    with open(path, "rb") as f:
        return sha256_bytes(f.read())


def canonical(obj):
    """链哈希的规范化序列化：排序键 + 紧凑分隔（ensure_ascii=False 保中文字节稳定）"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def record_hash(record):
    return sha256_bytes(canonical({k: v for k, v in record.items() if k != "record_sha256"}).encode("utf-8"))


# ---------------- 最小 JSON Schema 校验器（零三方依赖——runner 镜像不保证 jsonschema） ----------------
# 仅实现本仓 schema 用到的子集：type/const/enum/pattern/minLength/maxLength/
# minimum/maximum/required/properties/items/additionalProperties。schema 演进超出
# 子集 = fail-closed（校验器报"不支持的关键字"，不假装通过）。
def schema_check(value, schema, path, errs):
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        pymap = {"object": dict, "array": list, "string": str, "integer": int,
                 "number": (int, float), "boolean": bool, "null": type(None)}
        okt = False
        for tt in types:
            py = pymap.get(tt)
            if py is None:
                errs.append(f"{path}: 校验器不支持类型 {tt}"); return
            if isinstance(value, py) and not (tt == "integer" and isinstance(value, bool)) \
               and not (tt == "number" and isinstance(value, bool)):
                okt = True
        if not okt:
            errs.append(f"{path}: 类型应为 {t}，实际 {type(value).__name__}"); return
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: 应为常量 {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: 不在枚举 {schema['enum']!r}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{path}: 不匹配模式 {schema['pattern']}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: 短于 minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: 超出 maxLength {schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: 小于 minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: 大于 maximum {schema['maximum']}")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}: 缺必填 {k}")
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                schema_check(v, props[k], f"{path}.{k}", errs)
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: 多余字段 {k}")
            else:
                ap = schema.get("additionalProperties")
                if isinstance(ap, dict):  # 附加属性统一 schema 形态
                    schema_check(v, ap, f"{path}.{k}", errs)
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            schema_check(item, schema["items"], f"{path}[{i}]", errs)
    for kw in schema:
        if kw not in ("type", "const", "enum", "pattern", "minLength", "maxLength", "minimum",
                      "maximum", "required", "properties", "items", "additionalProperties",
                      "description", "$schema", "$id", "title", "minItems", "maxItems"):
            errs.append(f"{path}: 校验器不支持关键字 {kw}（fail-closed，勿静默放过）")
        if kw in ("minItems", "maxItems"):  # 显式支持，避免上面误报
            pass
    if isinstance(value, list) and "minItems" in schema and len(value) < schema["minItems"]:
        errs.append(f"{path}: 少于 minItems {schema['minItems']}")


def load_schema():
    try:
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001 —— 账本可信度依赖 schema 可读，任何失败即 fail-closed
        die(2, f"FATAL: record.schema.json 不可读：{e}")


def validate_record(record, schema, where):
    errs = []
    schema_check(record, schema, where, errs)
    return errs


# ---------------- 响应解析（invoke 聚合的唯一实现点——AC-1） ----------------
def parse_response(path, stream):
    """返回 (content, chunks, usage)。流式：所有分片聚合为一次 invoke 的内容与用量；
    非流式：单 body。usage 缺失返回 None（由 emit 按 fail-closed 处理）。"""
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        die(2, f"FATAL: 响应文件不可读 {path}: {e}")
    if not stream:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            return "", 0, None, f"响应非 JSON：{e}"
        choices = body.get("choices") or []
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        return content or "", 1, body.get("usage")
    parts, chunks, usage = [], 0, None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue  # SSE 注释/心跳行
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue  # 杂音行不计入分片
        chunks += 1
        choices = obj.get("choices") or []
        if choices:
            c = (choices[0].get("delta") or {}).get("content")
            if c:
                parts.append(c)
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]  # 多块带 usage 时取最后一块（OpenAI 兼容含 include_usage 终块）
    return "".join(parts), chunks, usage


def usage_tuple(usage):
    if not usage:
        return (0, 0, 0)
    g = lambda k: int(usage.get(k) or 0)  # noqa: E731
    return (g("prompt_tokens"), g("completion_tokens"), g("total_tokens"))


# ---------------- 账本读写 ----------------
def shard_files(ledger_dir):
    if not os.path.isdir(ledger_dir):
        return []
    return sorted(
        f for f in os.listdir(ledger_dir)
        if re.match(r"^records-\d{4}-W\d{2}\.jsonl$", f) and f.endswith(".jsonl")
    )


def read_records(path):
    """逐行读账本片。返回 (records, errs)；坏行（非法 JSON）记 err 不中断（verify 汇总报）。"""
    records, errs = [], []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                errs.append(f"{path}:{i + 1}: 非法 JSON（{e}）")
    return records, errs


def verify_ledger(ledger_dir, schema, files=None):
    """验全部（或指定）片：schema + 自哈希 + prev 链 + 索引 + invoke_id 唯一。
    返回 ({file: [records]}, [errs])。errs 非空即不可信。"""
    out, errs, seen_ids = {}, [], {}
    for fn in (files if files is not None else shard_files(ledger_dir)):
        path = os.path.join(ledger_dir, fn)
        records, rerrs = read_records(path)
        errs.extend(rerrs)
        prev = None
        for i, rec in enumerate(records):
            where = f"{fn}:{i + 1}"
            errs.extend(validate_record(rec, schema, where))
            if rec.get("record_sha256") != record_hash(rec):
                errs.append(f"{where}: record_sha256 重算不符（记录被改或未按 canonical 形态生成）")
            if rec.get("prev_record_sha256") != prev:
                errs.append(f"{where}: prev_record_sha256 断链（期望 {prev}）")
            if rec.get("record_index") != i:
                errs.append(f"{where}: record_index 应为 {i}")
            iid = rec.get("invoke_id")
            if iid:
                if iid in seen_ids:
                    errs.append(f"{where}: invoke_id 重复 {iid}（首次见 {seen_ids[iid]}——违反一次 invoke 一条记录）")
                else:
                    seen_ids[iid] = where
            prev = rec.get("record_sha256")
        out[fn] = records
    return out, errs


# ---------------- 子命令 ----------------
def cmd_mkreq(a):
    prompt = open(a.prompt_file, encoding="utf-8").read()
    system = open(a.system_file, encoding="utf-8").read() if a.system_file else ""
    req = {"model": a.model,
           "messages": ([{"role": "system", "content": system}] if system else [])
                       + [{"role": "user", "content": prompt}]}
    for k, v in (("max_tokens", a.max_tokens), ("temperature", a.temperature), ("top_p", a.top_p),
                 ("seed", a.seed)):
        if v is not None:
            req[k] = v
    if a.thinking:
        req["thinking"] = {"type": a.thinking}  # GLM 4.5+ 推理开关（disabled=计量类小调用必需）
    if a.stream:
        req["stream"] = True
        req["stream_options"] = {"include_usage": True}  # 终块带 usage——聚合计量的数据保障
    print(json.dumps(req, ensure_ascii=False, separators=(",", ":")))


def cmd_emit(a):
    schema = load_schema()
    ts_end = now_iso()
    exit_status = a.exit_status
    content, chunks, usage = "", 0, None
    if a.resp_file and os.path.isfile(a.resp_file):
        content, chunks, usage = parse_response(a.resp_file, a.stream)
    pt, ct, tt = usage_tuple(usage)
    if exit_status == "ok":
        # 自检：2xx 却拿不到内容 = 计量失败；usage 缺失允许继续（部分 provider
        # 如 LongCat 默认不返回 usage，此时记 0 token 但不阻断流程）
        if not content:
            exit_status = "error:metering"
        elif tt <= 0:
            # usage 缺失——记 0 token 并打印警告，不阻断
            print(f"warning: provider 响应缺 usage（记 0 token），content_len={len(content)}", file=sys.stderr)
    os.makedirs(a.ledger, exist_ok=True)
    ts = a.ts_start or ts_end
    # prompt 经文件读入（不走 argv：多行文本经 MSYS→Windows argv 会换行损坏）
    prompt_text = open(a.prompt_file, encoding="utf-8").read()
    system_text = open(a.system_file, encoding="utf-8").read() if a.system_file else ""
    shard = "records-{}-W{:02d}.jsonl".format(
        *dt.datetime.strptime(ts[:10], "%Y-%m-%d").date().isocalendar()[:2])
    shard_path = os.path.join(a.ledger, shard)
    prev, index = None, 0
    if os.path.isfile(shard_path):
        records, _ = read_records(shard_path)
        if records:
            prev, index = records[-1]["record_sha256"], len(records)
    # invoke_id 去重（聚合键=invoke_id，ADR-0062 决策 2）：同 id 二次落记录即拒绝
    for fn in shard_files(a.ledger):
        recs, _ = read_records(os.path.join(a.ledger, fn))
        for rec in recs:
            if rec.get("invoke_id") == a.invoke_id:
                die(3, f"invoke_id 重复：{a.invoke_id} 已存在于 {fn}（一次 invoke 恰一条记录——拒绝二次落账）")
    artifacts = [{"name": "request", "sha256": sha256_file(a.request_file), "bytes": os.path.getsize(a.request_file)}]
    if a.resp_file and os.path.isfile(a.resp_file):
        artifacts.append({"name": "response", "sha256": sha256_file(a.resp_file), "bytes": os.path.getsize(a.resp_file)})
    record = {
        "schema": RECORD_SCHEMA_ID, "invoke_id": a.invoke_id, "role": a.role, "model": a.model,
        "ts_start": ts, "ts_end": ts_end,
        "prompt_version": sha256_bytes((prompt_text + "\n" + system_text).encode("utf-8")),
        "prompt_bytes": len(prompt_text.encode("utf-8")), "seed": a.seed,
        "sampling": {"max_tokens": a.max_tokens, "temperature": a.temperature,
                     "top_p": a.top_p, "thinking": a.thinking or None},
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": tt},
        "latency_ms": a.latency_ms, "http_status": a.http_status, "exit_status": exit_status,
        "stream": a.stream, "chunks": max(chunks, 1),
        "artifacts": artifacts, "record_index": index, "prev_record_sha256": prev,
        "record_sha256": None,
    }
    record["record_sha256"] = record_hash(record)
    errs = validate_record(record, schema, "record")
    if errs or exit_status == "error:metering":
        side = os.path.join(a.ledger, f"invalid-{a.invoke_id}.json")
        with open(side, "w", encoding="utf-8", newline="\n") as f:
            json.dump(record, f, ensure_ascii=False)
        die(3, "计量自检失败（记录不落链）：\n  " + "\n  ".join(errs or ["2xx 响应缺内容或 usage——error:metering"])
            + f"\n  旁证已写 {side}")
    with open(shard_path, "a", encoding="utf-8", newline="\n") as f:
        f.write(canonical(record) + "\n")
    if a.content_out is not None:
        with open(a.content_out, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    # llm-usage/v1 兼容件（BEH-09 既有下游 spec-pr.py / run 摘要零改动）
    if a.usage_compat_dir and exit_status == "ok":
        os.makedirs(a.usage_compat_dir, exist_ok=True)
        art = {x["name"]: x["sha256"] for x in artifacts}
        compat = {"schema": "llm-usage/v1", "ts": ts, "tag": a.role, "model": a.model,
                  "prompt_version": record["prompt_version"], "prompt_bytes": record["prompt_bytes"],
                  "seed": a.seed, "thinking": a.thinking or None,
                  "sampling": {"max_tokens": a.max_tokens, "temperature": a.temperature},
                  "usage": record["usage"], "latency_ms": a.latency_ms, "http_status": a.http_status,
                  "request_sha256": art.get("request"), "response_sha256": art.get("response")}
        cf = os.path.join(a.usage_compat_dir, f"usage-{ts.replace(':', '')}-{a.role}.json")
        with open(cf, "w", encoding="utf-8", newline="\n") as f:
            json.dump(compat, f, ensure_ascii=False)
        print(f"usage-compat → {cf}", file=sys.stderr)
    print(canonical(record))


def cmd_verify(a):
    schema = load_schema()
    if a.file:
        base = os.path.dirname(os.path.abspath(a.file)) or "."
        _, errs = verify_ledger(base, schema, files=[os.path.basename(a.file)])
        files = [os.path.basename(a.file)]
        d = base
    else:
        if not os.path.isdir(a.dir):
            die(2, f"账本目录不存在：{a.dir}")
        files = shard_files(a.dir)
        if not files:
            die(2, f"账本目录无 records-*.jsonl：{a.dir}")
        _, errs = verify_ledger(a.dir, schema)
        d = a.dir
    if errs:
        for e in errs:
            print(f"CHAIN {e}", file=sys.stderr)
        die(3, f"验链失败：{len(errs)} 处（可信度破坏——产物链不可用）")
    for fn in files:
        recs, _ = read_records(os.path.join(d, fn))
        print(f"OK {fn}: {len(recs)} 条，链完整")
    return 0


def cmd_aggregate(a):
    schema = load_schema()
    files = shard_files(a.dir)
    if not files:
        die(2, f"账本目录无 records-*.jsonl：{a.dir}")
    ledger, errs = verify_ledger(a.dir, schema)  # 先验链后归账——不可信数据不入账
    if errs:
        for e in errs[:10]:
            print(f"CHAIN {e}", file=sys.stderr)
        die(3, f"账本验链失败（拒绝归账）：{len(errs)} 处")
    roles, totals, n = {}, {"invokes": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, 0
    for fn in files:
        for rec in ledger[fn]:
            ts = rec.get("ts_start", "")
            if a.since and ts[:10] < a.since:
                continue
            if a.until and ts[:10] > a.until:
                continue
            if rec.get("exit_status") != "ok":
                continue  # 失败调用零 token，不进归账（记录仍在链上可审计）
            n += 1
            role = rec.get("role", "unknown")
            b = roles.setdefault(role, {"invokes": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
            b["invokes"] += 1
            b["prompt_tokens"] += rec["usage"]["prompt_tokens"]
            b["completion_tokens"] += rec["usage"]["completion_tokens"]
            b["total_tokens"] += rec["usage"]["total_tokens"]
    for role, b in roles.items():
        for k in totals:
            totals[k] += b[k]
    print(json.dumps({"since": a.since, "until": a.until, "files": files, "records": n,
                      "roles": roles, "totals": totals}, ensure_ascii=False, sort_keys=True))


def cmd_relink(a):
    """本地周片续接到远端基链（ledger-sync 用）：base 与本地片各自验链通过后，
    重写本地记录的 index/prev/hash 接到 base 尾部，输出合并片到 --out。"""
    schema = load_schema()
    base_records = []
    if a.base and os.path.isfile(a.base):
        base_records, _ = read_records(a.base)
        _, errs = verify_ledger(os.path.dirname(os.path.abspath(a.base)) or ".", schema,
                                files=[os.path.basename(a.base)])
        if errs:  # 远端链已坏：绝不覆盖（审计面）——中止让 push 不发生
            for e in errs[:10]:
                print(f"CHAIN(base) {e}", file=sys.stderr)
            die(3, "基链验链失败——拒绝合并（防覆盖掩盖篡改）")
    _, errs = verify_ledger(os.path.dirname(os.path.abspath(a.local)) or ".", schema,
                            files=[os.path.basename(a.local)])
    if errs:
        for e in errs[:10]:
            print(f"CHAIN(local) {e}", file=sys.stderr)
        die(3, "本地账本验链失败——拒绝合并")
    merged, prev = list(base_records), (base_records[-1]["record_sha256"] if base_records else None)
    recs, _ = read_records(a.local)
    for rec in recs:
        rec = dict(rec)
        rec["record_index"] = len(merged)
        rec["prev_record_sha256"] = prev
        rec["record_sha256"] = record_hash(rec)
        merged.append(rec)
        prev = rec["record_sha256"]
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        for rec in merged:
            f.write(canonical(rec) + "\n")
    print(f"relink → {a.out}（base {len(base_records)} + local {len(recs)} 条）")


def main():
    ap = argparse.ArgumentParser(prog="metering.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("mkreq")
    p.add_argument("--model", required=True)
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--system-file")
    for f in ("max_tokens", "seed"):
        p.add_argument(f"--{f.replace('_', '-')}", dest=f, type=int)
    p.add_argument("--temperature", type=float)
    p.add_argument("--top-p", type=float)
    p.add_argument("--thinking", choices=["disabled", "enabled"])
    p.add_argument("--stream", action="store_true")
    p.set_defaults(fn=cmd_mkreq)

    p = sub.add_parser("emit")
    p.add_argument("--ledger", required=True, help="GATE_METERING_DIR（JSONL 账本目录）")
    p.add_argument("--invoke-id", default=None,
                   help="缺省 uuid4——显式传入用于跨分片对齐（一次 invoke 恰一条记录）")
    p.add_argument("--role", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--system-file", default="")
    p.add_argument("--ts-start")
    p.add_argument("--latency-ms", type=int, required=True)
    p.add_argument("--http-status", type=int)
    p.add_argument("--exit-status", required=True)
    p.add_argument("--stream", action="store_true")
    p.add_argument("--resp-file")
    p.add_argument("--request-file", required=True)
    p.add_argument("--content-out")
    p.add_argument("--usage-compat-dir")
    for f in ("max_tokens", "seed"):
        p.add_argument(f"--{f.replace('_', '-')}", dest=f, type=int)
    p.add_argument("--temperature", type=float)
    p.add_argument("--top-p", type=float)
    p.add_argument("--thinking", choices=["disabled", "enabled"])
    p.set_defaults(fn=cmd_emit)

    p = sub.add_parser("verify")
    p.add_argument("--dir", default=None, help="账本目录（验全部周片）；与 --file 互斥省略")
    p.add_argument("--file", help="单个账本片（断点定位）")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("aggregate")
    p.add_argument("--dir", required=True)
    p.add_argument("--since", help="YYYY-MM-DD（含），按 ts_start 过滤")
    p.add_argument("--until", help="YYYY-MM-DD（含）")
    p.add_argument("--json", action="store_true", help="兼容占位（aggregate 输出恒为 JSON）")
    p.set_defaults(fn=cmd_aggregate)

    p = sub.add_parser("relink")
    p.add_argument("--base", help="远端基链 jsonl（缺省/不存在=创世）")
    p.add_argument("--local", required=True, help="本地周片 jsonl（独立成链的原始记录）")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_relink)

    a = ap.parse_args()
    if getattr(a, "invoke_id", None) is None:
        a.invoke_id = uuid.uuid4().hex
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
