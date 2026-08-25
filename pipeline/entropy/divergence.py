#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""divergence.py —— 骨架解读方差仪器（IR-0004 AC-9 rev6 仪器化，PM 可选用）。

输入：N 份骨架 md（--dir 目录，每份四节，节标题=行首 `#`*1-6 + 空格 + 节名）：
  ## 路线陈述 / ## 接口签名 / ## 测试草案 / ## 假设清单
  节内条目=非空行（`- ` / `* ` 弹壳剥除；其余 `#` 行忽略）。

机械计算产物（全部确定性：排序、无时间戳、无随机源；UTF-8 + LF）：
  divergence-report.json
    - pairs：两两字符 3-gram Jaccard 相似度（参数版本化留痕：
      {algo: "3gram-jaccard", version: "v1", threshold: 0.85}；超阈值=
      疑似串通/拷贝，防人为放大交集——AC-9 独立性校验）；
    - convergence_pct：趋同度=100×|测试草案交集|/|测试草案并集|；
    - contract_divergences / route_divergences：分歧正交分解——关键词启发式
      归类（接口签名/测试草案节条目集不一致=契约；路线陈述节=路线），
      每条判断附证据片段（only_in_a / only_in_b + 命中关键词）；
    - assumptions_union：假设清单去重并集（spec 缺口显式清单）；
    - test_intersection：N 份测试草案交集（验收标准候选）；
    - test_union_minus_intersection：并集减交集（红队输入燃料）。
  fanout-products.jsonl（IR-0004 AC-13 / IFACE-03 燃料目录契约——append-only
    由目录消费侧校验，本工具只生产）：
    {type: skeleton_divergence|assumption, card_id, spec_hash, base_sha, ...}
    ——card_id/spec_hash/base_sha 均为 CLI 参数（base_sha 动态传入）。

与既有 pipeline/entropy 语义熵仪器（ADR-0066）分工：本仪器测"多份骨架解读
方差"（fan-out 独立性+燃料），语义熵测"单 spec 条款歧义是否达 bug 级"；
互不替代（AC-9 / DECISION-05）。

用法:
  python pipeline/entropy/divergence.py --dir skeletons/ --out out/ \
      --card-id CARD-07 --spec-hash <sha256> --base-sha <sha> [--threshold 0.85]

退出码: 0=绿（计算完成）  1=输入红（缺节/空目录）  2=用法错误
"""
import argparse
import itertools
import json
import re
import sys
import unicodedata
from pathlib import Path

SECTIONS = ("路线陈述", "接口签名", "测试草案", "假设清单")
CONTRACT_SECTIONS = ("接口签名", "测试草案")
ROUTE_SECTIONS = ("路线陈述",)
CONTRACT_KEYWORDS = ("接口", "签名", "测试", "契约", "函数", "参数", "返回", "test", "assert", "api", "->")
ROUTE_KEYWORDS = ("路线", "方案", "策略", "架构", "流程", "读取", "写入", "分发", "同步", "异步")
ALGO_NAME = "3gram-jaccard"
ALGO_VERSION = "v1"
DEFAULT_THRESHOLD = 0.85
NORMALIZATION = ("NFKC+ASCII小写+去空白与标点（\\W）→ 字符 3-gram 集（短于 3 的非空串"
                 "整体作单 gram；空串=空集）→ |∩|/|∪|（双空集=1.0）")
EVIDENCE_MAX_ITEMS = 3
EVIDENCE_SNIPPET_LEN = 80


class EntropyError(Exception):
    """输入不可计算（目录空 / 骨架缺节）——友好可读。"""


# ---------------- 归一化与相似度（版本 v1） ----------------

def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"\W+", "", s, flags=re.UNICODE)


def ngrams(s: str, n: int = 3):
    t = normalize_text(s)
    if not t:
        return set()
    return {t[i:i + n] for i in range(max(len(t) - n + 1, 1))}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0  # 双空=完全一致
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similarity(text_a: str, text_b: str) -> float:
    return jaccard(ngrams(text_a), ngrams(text_b))


# ---------------- 骨架解析 ----------------

_HEADING = re.compile(r"^\s*#{1,6}\s*(\S+)\s*$")
_BULLET = re.compile(r"^\s*[-*]\s+")


def parse_skeleton(text: str, source: str = "<skeleton>"):
    """四节解析 → {节名: [条目原文]}；缺节/空节=EntropyError（fail-closed）。"""
    text = text.replace("\r\n", "\n")
    sections = {name: [] for name in SECTIONS}
    seen = set()
    current = None
    for raw in text.split("\n"):
        m = _HEADING.match(raw)
        if m:  # 命中任一标题：目标节开节，其余标题视为节边界
            current = m.group(1) if m.group(1) in SECTIONS else None
            if current:
                seen.add(current)
            continue
        if current is None:
            continue
        line = _BULLET.sub("", raw.rstrip()).strip()
        if line:
            sections[current].append(line)
    missing = [name for name in SECTIONS if name not in seen]
    if missing:
        raise EntropyError(
            f"{source}: 缺少节 {('/'.join(missing))}（每份骨架须四节: "
            + "/".join(SECTIONS) + "）"
        )
    empty = [name for name in SECTIONS if name in seen and not sections[name]]
    if empty:
        raise EntropyError(
            f"{source}: 节 {('/'.join(empty))} 为空（条目不可为空）"
        )
    return sections


def _item_map(items):
    """条目原文 → 规范键去重（首现代表）。"""
    out = {}
    for it in items:
        out.setdefault(normalize_text(it), it)
    return out


def _diff_evidence(items_a, items_b, section, keywords, kind_label):
    ka, kb = _item_map(items_a), _item_map(items_b)
    if set(ka) == set(kb):
        return None
    only_a = [ka[k] for k in sorted(set(ka) - set(kb))]
    only_b = [kb[k] for k in sorted(set(kb) - set(ka))]
    hit = sorted({
        kw for kw in keywords
        if any(kw in it for it in only_a + only_b)
    })
    snippet = lambda xs: [x[:EVIDENCE_SNIPPET_LEN] for x in xs[:EVIDENCE_MAX_ITEMS]]
    return {
        "section": section,
        "classified_by": f"节关键词『{section}』分歧→{kind_label}（启发式词表）",
        "only_in_a": snippet(only_a),
        "only_in_b": snippet(only_b),
        "only_in_a_count": len(only_a),
        "only_in_b_count": len(only_b),
        "keyword_hits": hit,
    }


def analyze(skeleton_dir, threshold=DEFAULT_THRESHOLD):
    """纯计算：目录 → 报告 dict（确定性）。"""
    d = Path(skeleton_dir)
    if not d.is_dir():
        raise EntropyError(f"骨架目录不存在: {d}")
    files = sorted(p for p in d.glob("*.md") if p.is_file())
    if not files:
        raise EntropyError(f"骨架目录无 .md 文件: {d}")
    skels, raws = {}, {}
    for p in files:
        raw = p.read_text(encoding="utf-8-sig")
        raws[p.name] = raw
        skels[p.name] = parse_skeleton(raw, p.name)
    names = sorted(skels)

    pairs = []
    contract_div, route_div = [], []
    for a, b in itertools.combinations(names, 2):
        sim = similarity(raws[a], raws[b])
        pairs.append({
            "a": a, "b": b,
            "similarity": round(sim, 4),
            "algorithm_version": ALGO_VERSION,
            "threshold": threshold,
            "suspected_collusion": sim > threshold,
            "section_similarity": {
                sec: round(similarity("\n".join(skels[a][sec]),
                                      "\n".join(skels[b][sec])), 4)
                for sec in SECTIONS
            },
        })
        for secs, kws, bucket, kind, label in (
            (CONTRACT_SECTIONS, CONTRACT_KEYWORDS, contract_div, "contract", "契约"),
            (ROUTE_SECTIONS, ROUTE_KEYWORDS, route_div, "route", "路线"),
        ):
            evidence = [e for sec in secs
                        if (e := _diff_evidence(skels[a][sec], skels[b][sec], sec,
                                                kws, label))]
            if evidence:
                bucket.append({
                    "a": a, "b": b, "kind": kind,
                    "pair_similarity": round(sim, 4),
                    "evidence": evidence,
                })

    # 测试草案交集/并集（代表原文取字典序首份）
    test_maps = {n: _item_map(skels[n]["测试草案"]) for n in names}
    inter_keys = set.intersection(*(set(m) for m in test_maps.values()))
    union_keys = set.union(*(set(m) for m in test_maps.values()))
    pick = lambda k: next(test_maps[n][k] for n in names if k in test_maps[n])
    test_intersection = sorted((pick(k) for k in inter_keys))
    test_union_minus = sorted(
        (pick(k) for k in union_keys - inter_keys)
    )
    as_maps = (_item_map(skels[n]["假设清单"]) for n in names)
    as_union = sorted({it for m in as_maps for it in m.values()})

    convergence = round(100.0 * len(inter_keys) / len(union_keys), 2) if union_keys else 0.0
    mean_sim = round(sum(p["similarity"] for p in pairs) / len(pairs), 4)
    return {
        "schema": "skeleton-divergence/v1",
        "algorithm": {
            "algo": ALGO_NAME, "version": ALGO_VERSION,
            "threshold": threshold, "normalization": NORMALIZATION,
        },
        "files": names,
        "pairs": pairs,
        "collusion_suspects": [
            {"a": p["a"], "b": p["b"], "similarity": p["similarity"]}
            for p in pairs if p["suspected_collusion"]
        ],
        "convergence_pct": convergence,
        "mean_pair_similarity": mean_sim,
        "contract_divergences": contract_div,
        "route_divergences": route_div,
        "assumptions_union": as_union,
        "test_intersection": test_intersection,
        "test_union_minus_intersection": test_union_minus,
    }


def fanout_records(report, card_id, spec_hash, base_sha):
    """IR-0004 AC-13 / IFACE-03 燃料记录（四必填字段 + 明细）。"""
    records = []
    for div in report["contract_divergences"] + report["route_divergences"]:
        records.append({
            "type": "skeleton_divergence", "card_id": card_id,
            "spec_hash": spec_hash, "base_sha": base_sha,
            "detail": {"a": div["a"], "b": div["b"], "kind": div["kind"],
                       "sections": [e["section"] for e in div["evidence"]]},
        })
    for item in report["assumptions_union"]:
        records.append({
            "type": "assumption", "card_id": card_id,
            "spec_hash": spec_hash, "base_sha": base_sha, "item": item,
        })
    return records


def write_outputs(report, out_dir, card_id, spec_hash, base_sha):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "divergence-report.json", "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    with open(out / "fanout-products.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for rec in fanout_records(report, card_id, spec_hash, base_sha):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        prog="pipeline/entropy/divergence.py",
        description="骨架解读方差仪器（IR-0004 AC-9 rev6，PM 可选用）",
    )
    ap.add_argument("--dir", required=True, help="骨架目录（N 份 md，四节结构）")
    ap.add_argument("--out", required=True, help="产物输出目录")
    ap.add_argument("--card-id", required=True, help="关联实施卡 ID（燃料记录字段）")
    ap.add_argument("--spec-hash", required=True, help="来源 spec hash（燃料记录字段）")
    ap.add_argument("--base-sha", required=True,
                    help="基准 SHA（消费侧机械核对用，动态传入）")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"串通判定阈值（默认 {DEFAULT_THRESHOLD}）")
    args = ap.parse_args(argv)
    try:
        report = analyze(args.dir, threshold=args.threshold)
        write_outputs(report, args.out, args.card_id, args.spec_hash, args.base_sha)
    except EntropyError as e:
        print(f"[divergence] RED: {e}", file=sys.stderr)
        return 1
    print(
        f"[divergence] GREEN: files={len(report['files'])} "
        f"pairs={len(report['pairs'])} convergence={report['convergence_pct']}% "
        f"suspects={len(report['collusion_suspects'])} "
        f"contract={len(report['contract_divergences'])} "
        f"route={len(report['route_divergences'])} "
        f"assumptions={len(report['assumptions_union'])} → {args.out}/"
        "divergence-report.json + fanout-products.jsonl"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
