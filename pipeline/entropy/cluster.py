#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cluster.py —— 双向蕴含聚簇 + 语义熵（W4-C1 .github#220，ADR-0066 决策 2/3）。

零 LLM/零网络/零第三方依赖的确定性判定链（AC-4 静态可证：
tests/test_static_zero_llm.py 扫本文件+judge.py+policy.py 的 import 面黑名单）。
方法 = Semantic Entropy 双向蕴含聚簇（Kuhn/Gal/Farquhar 等，Nature 2024）：
A↔B 互蕴含归同簇（并查集传递闭包），簇数与熵在此计算——铁律：聚簇与计数
绝不退化为投票（多数决不出现，簇内样本数只进熵权重）。

蕴含引擎可插拔（--engine）：
  heuristic     零依赖默认：双向归一化 token 覆盖率 + 结构等价（JSON 同构）。
                诚实局限：同义改写在 token 覆盖不足时会裂簇（保守方向=可能
                高估分歧）；生产部署应换 deberta-mnli。
  deberta-mnli  外部 NLI 服务形态（ADR-0066："DeBERTa-MNLI 或等效"，版本钉
                死入 registry）：CI 不装重模型，适配器 nli_deberta.py 惰性
                import（不在默认 import 面）——部署形态见该文件 docstring。

用法:
  python3 cluster.py --derivations d.json [--noise n.json] \
      [--engine heuristic] --out c.json
出: clusters.json（per-clause 跨族簇 + 全局 lane 簇 + 底噪自簇 B）——确定性：
    同输入同输出（无时间戳/随机源）。
"""
import argparse
import json
import math
import re
import sys
import unicodedata

import policy

# ---------------- 归一化与启发式蕴含（零依赖） ----------------

_WORD = re.compile(r"[a-z0-9]+")


def token_set(text):
    """归一化 token 集：拉丁/数字按词；CJK 连续段取字符 unigram+bigram 并集。

    集合（非多重集）containment 对语序调整与功能词重复不敏感（AC-1
    "同义不同措辞不误报"的机制）；bigram 保留区分度、unigram 抬高同义改写
    的召回。零分词依赖的近似——局限见模块 docstring。
    """
    text = unicodedata.normalize("NFKC", text).lower()
    out = set()
    for run in re.split(r"[\s\W]+", text, flags=re.UNICODE):
        if not run:
            continue
        if re.fullmatch(r"[a-z0-9]+", run):
            out.add(run)
        else:  # CJK 段（或其他非拉丁文字）
            out.update(run)
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _coverage(a_set, b_set):
    """a 对 b 的内容覆盖（entails(a,b) 分）：b 的 token 集被 a 包含的比例。"""
    if not b_set:
        return 1.0
    return len(a_set & b_set) / len(b_set)


def _structural_equal(a, b):
    """结构等价启发式：两侧都解析为 JSON 且 canonical 序列化相同 → 等价。"""
    try:
        ja, jb = json.loads(a), json.loads(b)
    except (ValueError, TypeError):
        return False
    return json.dumps(ja, sort_keys=True, ensure_ascii=False) == json.dumps(
        jb, sort_keys=True, ensure_ascii=False)


def heuristic_bidirectional(a, b, theta=None):
    """零依赖双向蕴含判定：双向覆盖率均 >= theta，或结构等价。"""
    theta = policy.HEURISTIC_THETA if theta is None else theta
    if a.strip() == b.strip():
        return True
    if _structural_equal(a, b):
        return True
    ta, tb = token_set(a), token_set(b)
    return _coverage(ta, tb) >= theta and _coverage(tb, ta) >= theta


# ---------------- 引擎注册表（可插拔） ----------------

ENGINES = {"heuristic": heuristic_bidirectional}


def get_engine(name):
    """按名取双向蕴含判定函数。deberta-mnli 走惰性 import（不进默认 import 面）。"""
    if name in ENGINES:
        return ENGINES[name]
    if name == "deberta-mnli":
        import nli_deberta  # noqa: E402  惰性加载：仅显式选用时进 import 面
        return nli_deberta.bidirectional
    raise SystemExit(f"未知蕴含引擎：{name}（可用：heuristic / deberta-mnli）")


# ---------------- 聚簇（并查集传递闭包）与熵 ----------------

class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def cluster_texts(texts, entail):
    """簇 = 双向蕴含关系的传递闭包。返回 [[索引...]...]（簇内升序、簇间按首索引）。"""
    n = len(texts)
    uf = UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if entail(texts[i], texts[j]):
                uf.union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def semantic_entropy(cluster_sizes):
    """语义熵（nats）：p_i=|簇_i|/n，H=-Σ p ln p。空集/单簇 → 0。"""
    n = sum(cluster_sizes)
    if n == 0 or len(cluster_sizes) <= 1:
        return 0.0
    return -sum((c / n) * math.log(c / n) for c in cluster_sizes)


# ---------------- 主流程：派生/底噪 → 簇 ----------------

def _per_clause_clusters(readings_by_clause, lane_ids, entail):
    """按条款聚簇跨族读法。readings_by_clause: {clause_id: [lane 读法文本...]}"""
    out = {}
    for clause, texts in readings_by_clause.items():
        groups = cluster_texts(texts, entail)
        out[clause] = {
            "clusters": len(groups),
            "members": [[lane_ids[i] for i in g] for g in groups],
            "readings": [[texts[i] for i in g] for g in groups],
            "entropy_nats": round(semantic_entropy([len(g) for g in groups]), 4),
        }
    return out


def _lane_digests(lanes):
    """全局 lane 摘要 = 该 lane 全部条款读法的归一拼接（供全局聚簇）。"""
    return ["\n".join(r["text"] for r in lane["readings"]) for lane in lanes]


def build_clusters(derivations, noise=None, engine="heuristic"):
    entail = get_engine(engine)
    lanes = derivations["lanes"]
    lane_ids = [f"lane{l['lane']}" for l in lanes]
    readings_by_clause = {}
    for lane in lanes:
        for r in lane["readings"]:
            readings_by_clause.setdefault(r["clause"], []).append(r["text"])
    per_clause = _per_clause_clusters(readings_by_clause, lane_ids, entail)
    g_groups = cluster_texts(_lane_digests(lanes), entail)
    result = {
        "schema": "entropy-clusters/v1",
        "engine": engine,
        "k": len(lanes),
        "global_lane_clusters": len(g_groups),
        "global_members": [[lane_ids[i] for i in g] for g in g_groups],
        "per_clause": per_clause,
        "noise": None,
    }
    if noise:
        # 底噪：同族 m 次重采样按条款自聚簇；B = 各条款自簇数的保守最大值
        samples = noise["samples"]
        s_ids = [f"sample{s['sample']}" for s in samples]
        nr = {}
        for s in samples:
            for r in s["readings"]:
                nr.setdefault(r["clause"], []).append(r["text"])
        n_per_clause = _per_clause_clusters(nr, s_ids, entail)
        result["noise"] = {
            "family": noise.get("family"),
            "resample_m": len(samples),
            "self_clusters_per_clause": {c: v["clusters"] for c, v in n_per_clause.items()},
            "B": max((v["clusters"] for v in n_per_clause.values()), default=0),
        }
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="双向蕴含聚簇（零 LLM 确定性）")
    ap.add_argument("--derivations", required=True)
    ap.add_argument("--noise")
    ap.add_argument("--engine", default="heuristic",
                    choices=["heuristic", "deberta-mnli"])
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    derivations = json.load(open(args.derivations, encoding="utf-8"))
    noise = json.load(open(args.noise, encoding="utf-8")) if args.noise else None
    result = build_clusters(derivations, noise, args.engine)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    hot = {c: v["clusters"] for c, v in result["per_clause"].items()}
    print(f"聚簇完成：engine={args.engine} 全局 lane 簇={result['global_lane_clusters']} "
          f"per-clause={hot} B={result['noise']['B'] if result['noise'] else 'N/A'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
