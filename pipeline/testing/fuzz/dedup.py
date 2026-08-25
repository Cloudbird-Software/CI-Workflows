#!/usr/bin/env python3
"""dedup —— 崩溃样本栈哈希去重（IR-0004 AC-3）。

输入一组崩溃 traceback 文本，按「异常类型 + 归一化栈帧序列」计算 sha256 指纹：
  - 栈帧取 `File "path", line N, in func` 行；
  - 归一化：文件路径取 basename、行号丢弃（默认，重编译不裂变指纹；
    --strict-lines 可保留行号作严格模式）、函数名保留；
  - 异常类型取 traceback 末行 `SomeError: message` 的类名部分（消息丢弃，
    消息含易变数据不参与指纹）。
输出唯一指纹清单 + 每指纹重复计数。

CLI：
  python dedup.py --dir crashes/            # 目录内 *.txt
  python dedup.py --files a.txt b.txt       # 显式文件
  python dedup.py --stdin < crashes.txt     # 单样本
  （可选 --out fingerprints.json --markdown report.md --strict-lines）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

FRAME_RE = re.compile(r'^\s*File\s+"(?P<file>[^"]+)",\s+line\s+(?P<line>\d+),\s+in\s+(?P<func>.+?)\s*$')
EXC_RE = re.compile(r"^(?P<exc>[A-Za-z_][A-Za-z0-9_.]*)(\s*:.*)?$")


def parse_traceback(text):
    """返回 (frames, exception_type)；frames 为 (file_basename, func[, line]) 元组列表。"""
    frames = []
    exception = "NO-EXCEPTION"
    for raw in text.splitlines():
        m = FRAME_RE.match(raw)
        if m:
            frames.append((Path(m.group("file")).name, m.group("func").strip(), int(m.group("line"))))
    for raw in reversed([l for l in text.splitlines() if l.strip()]):
        m = EXC_RE.match(raw.strip())
        if m and not raw.lstrip().startswith("File "):
            exception = m.group("exc")
        break
    return frames, exception


def fingerprint(text, strict_lines=False):
    """计算单个崩溃文本的栈指纹（sha256 hex）。"""
    frames, exception = parse_traceback(text)
    if not frames:
        # 无栈帧样本：退化用全文归一化（压空白）哈希，仍可机械去重
        normalized = " ".join(text.split())
        return hashlib.sha256(("NO-TRACEBACK|" + normalized).encode("utf-8")).hexdigest(), exception
    parts = []
    for frame in frames:
        if strict_lines:
            parts.append("%s:%s:%d" % frame)
        else:
            parts.append("%s:%s" % (frame[0], frame[1]))
    payload = exception + "|" + "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), exception


def dedup_texts(texts, labels=None, strict_lines=False):
    """输入 [(label, text)]，输出去重报告 dict（纯机械分组）。"""
    labels = labels or [str(i) for i in range(len(texts))]
    groups = {}
    for label, text in zip(labels, texts):
        fp, exception = fingerprint(text, strict_lines=strict_lines)
        frames, _ = parse_traceback(text)
        group = groups.setdefault(
            fp,
            {
                "fingerprint": fp,
                "count": 0,
                "inputs": [],
                "exception_type": exception,
                "top_frames": ["%s:%s" % (f[0], f[1]) for f in frames[-3:]],
            },
        )
        group["count"] += 1
        group["inputs"].append(label)
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["fingerprint"]))
    return {
        "algorithm": "sha256(exception + '|' + join(basename:func[, :line]))" + ("+line" if strict_lines else ""),
        "total_inputs": len(texts),
        "unique_fingerprints": len(groups),
        "groups": ordered,
    }


def _markdown(report):
    lines = ["# 崩溃栈去重报告", ""]
    lines.append("- 输入样本: %d" % report["total_inputs"])
    lines.append("- 唯一指纹: %d" % report["unique_fingerprints"])
    lines.append("- 算法: `%s`" % report["algorithm"])
    lines.append("")
    lines.append("| 指纹(前16) | 重复计数 | 异常 | 顶层帧 |")
    lines.append("|---|---|---|---|")
    for group in report["groups"]:
        lines.append(
            "| `%s` | %d | %s | %s |"
            % (group["fingerprint"][:16], group["count"], group["exception_type"], " <- ".join(group["top_frames"]))
        )
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="崩溃样本栈哈希去重（AC-3）")
    parser.add_argument("--dir", help="崩溃文本目录（*.txt）")
    parser.add_argument("--files", nargs="*", help="崩溃文本文件列表")
    parser.add_argument("--stdin", action="store_true", help="从 stdin 读单样本")
    parser.add_argument("--out", help="JSON 报告输出路径")
    parser.add_argument("--markdown", help="markdown 报告输出路径")
    parser.add_argument("--strict-lines", action="store_true", help="指纹保留行号（严格模式）")
    args = parser.parse_args(argv)

    labels, texts = [], []
    if args.dir:
        for path in sorted(Path(args.dir).glob("*.txt")):
            labels.append(path.name)
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
    if args.files:
        for name in args.files:
            labels.append(Path(name).name)
            texts.append(Path(name).read_text(encoding="utf-8", errors="replace"))
    if args.stdin:
        labels.append("stdin")
        texts.append(sys.stdin.read())
    if not texts:
        print("FATAL: 未提供输入（--dir / --files / --stdin 三选一）", file=sys.stderr)
        return 2

    report = dedup_texts(texts, labels=labels, strict_lines=args.strict_lines)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8", newline="\n")
    if args.markdown:
        Path(args.markdown).write_text(_markdown(report), encoding="utf-8", newline="\n")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
