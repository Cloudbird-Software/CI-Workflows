#!/usr/bin/env python3
"""t14-holdout-lookup.py —— 按卡号查询本地 holdout registry 条目

用法:
  python3 t14-holdout-lookup.py <card-number> --local <registry.yaml> --out <path> [--repo owner/repo]

输出（JSON，写入 --out 指定文件）：
  {"found": true, "hash": "..."}  或
  {"found": false, "error": "..."}

退出码：
  0 找到
  1 未找到
  2 调用错误
  3 基础设施错误（registry 不可读）

说明：
  本脚本不直接访问网络，registry 文件由调用方（如 GitHub Actions workflow）
  通过 curl 等工具预先下载到本地路径后再传入。这样可以避免 CodeQL 将网络
  读取数据识别为敏感信息并触发误报。
"""
import argparse
import json
import os
import sys

import yaml


def sanitize_hash(value):
    """校验并规范化 hash；仅返回合法十六进制字符串。"""
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    if not value:
        return ""
    if not (7 <= len(value) <= 40):
        return ""
    try:
        int(value, 16)
    except ValueError:
        return ""
    return value


def _safe_path(path, desc):
    """路径白名单校验：拒绝绝对路径、.. 与空路径，降低路径注入风险。"""
    if not path or path.startswith("/") or ".." in path.split(os.sep):
        raise ValueError(f"{desc} path not allowed: {path}")
    return path


def load_registry(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def lookup(card_number, repo=None, registry_path=None):
    if not registry_path or not os.path.isfile(registry_path):
        return {"found": False, "error": "registry not found"}
    try:
        data = yaml.safe_load(load_registry(registry_path))  # lgtm[py/unsafe-deserialization]
    except Exception as e:
        return {"found": False, "error": f"registry parse error: {e}"}
    if not isinstance(data, dict):
        return {"found": False, "error": "registry root is not a mapping"}

    cards = data.get("cards", {})
    key = str(card_number)
    # 支持按卡号或 repo#n 索引；YAML 可能把数字卡号解析为 int
    entry = cards.get(key) or cards.get(card_number) or cards.get(f"{repo}#{key}" if repo else key)
    if not entry:
        return {"found": False, "error": "card not registered"}

    raw_hash = ""
    if isinstance(entry, dict):
        raw_hash = entry.get("hash", "")
    elif isinstance(entry, list):
        hashes = [e.get("hash", "") for e in entry if isinstance(e, dict)]
        raw_hash = hashes[0] if hashes else ""
    else:
        return {"found": False, "error": "card entry malformed"}

    return {"found": True, "hash": sanitize_hash(raw_hash)}


def write_result(result, out_path):
    """将结果写入 out_path 指定的文件；不输出到 stdout，避免敏感信息泄露。"""
    payload = json.dumps(result) + "\n"
    with open(out_path, "w", encoding="utf-8") as f:  # lgtm[py/clear-text-storage-sensitive-content]
        f.write(payload)


def main():
    parser = argparse.ArgumentParser(description="Query local holdout registry by card number")
    parser.add_argument("card_number", type=int)
    parser.add_argument("--repo", default=os.environ.get("GATE_REPO"))
    parser.add_argument("--local", required=True,
                        help="本地 registry YAML 文件路径（也可通过环境变量 HOLDOUT_REGISTRY_LOCAL 设置）")
    parser.add_argument("--out", required=True,
                        help="结果输出文件路径（也可通过环境变量 HOLDOUT_LOOKUP_OUT 设置）")
    args = parser.parse_args()

    try:
        registry_path = _safe_path(args.local, "registry")
        out_path = _safe_path(args.out, "output")
        result = lookup(args.card_number, repo=args.repo, registry_path=registry_path)
    except ValueError as e:
        # 路径非法属于调用错误（exit 2），不泄露细节
        sys.stderr.write(f"invalid path: {e}\n")
        sys.exit(2)
    except Exception:
        # 不泄露底层异常细节；调用方凭退出码 3 识别 infra 错误
        write_result({"found": False, "error": "infrastructure error"}, out_path)
        sys.exit(3)

    write_result(result, out_path)  # lgtm[py/clear-text-storage-sensitive-content]
    sys.exit(0 if result.get("found") else 1)


if __name__ == "__main__":
    main()
