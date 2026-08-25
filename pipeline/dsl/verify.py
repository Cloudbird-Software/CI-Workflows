#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify.py —— spec hash 溯源校验（gate 用；IR-0004 AC-8 / IFACE-04）。

对编译生成物做三重机械校验（任一不符=exit 1，fail-closed）：
  1. spec-hash：重算 spec.md 全文字节 sha256，比对生成物头部声明
     （改 spec 一字即红——全文 hash 语义）；
  2. ac-hash：逐条重算 AC 的 sha256（id\\0given\\0when\\0then），比对头部
     声明（判据文本变更即红；增删 AC 同样红）；
  3. 应然内容：以确定性渲染（compile.render）重算生成物全文，与实际内容
     逐字节比对——手改正文但 hash 头不动同样检出（无"只改头不检身"盲区）。

红处置（输出必含）：
  手改生成测试：改 spec 后重新编译，或删除本文件由实施卡重写

与 T-13（ADR-0035 test-integrity）的口径对齐（AC-8 承接声明，不改 T-13）：
本工具管辖 DSL 编译生成物（直接红）；一般测试文件四形态归 T-13；同一文件
双命中时从严者生效。与 g060/spec-check.py 并存分工：spec-check.py 管
frontmatter 结构与注入双扫，本工具管 hash 溯源，重叠面从严者生效。

注意：--generated/--spec 以参数原样参与应然内容渲染（regenerate 行），编译
与校验须以同形路径调用（组织约定：仓根相对路径）。

用法:
  python pipeline/dsl/verify.py --generated specs/IR-0007/suite/generated/test_ir_0007.py \
      --spec specs/IR-0007/spec.md

退出码: 0=绿  1=红  2=用法错误
"""
import argparse
import re
import sys
from pathlib import Path

try:
    from pipeline.dsl.compile import (  # 包内引用
        SpecError, ac_sha256, load_spec, render,
    )
except ImportError:  # 脚本直跑（sys.path[0]=pipeline/dsl）
    from compile import (  # noqa: F401
        SpecError, ac_sha256, load_spec, render,
    )

REMEDIATE = "手改生成测试：改 spec 后重新编译，或删除本文件由实施卡重写"

_SPEC_HASH_LINE = re.compile(r"^# spec-hash: ([0-9a-f]{64})$", re.M)
_AC_HASH_LINE = re.compile(r"^#   - ([^:\n]+): ([0-9a-f]{64})$", re.M)


def verify_files(generated_path: str, spec_path: str):
    """三重校验。返回 (是否绿, 问题清单)；不抛业务异常（全折为红问题）。"""
    problems = []
    try:
        spec_hash, acs = load_spec(spec_path)
    except SpecError as e:
        return False, [f"spec 解析失败: {e}"]
    expected = render(spec_hash, acs, str(spec_path), str(generated_path))

    gp = Path(generated_path)
    if not gp.is_file():
        return False, [
            f"生成物缺失: {gp}（编译: python pipeline/dsl/compile.py "
            f"--spec {spec_path} --out {gp}）"
        ]
    raw = gp.read_bytes()
    try:
        actual = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, [f"生成物非 UTF-8: {gp}"]

    # (1)+(2) 头部声明比对
    m = _SPEC_HASH_LINE.search(actual)
    header_acs = _AC_HASH_LINE.findall(actual)
    if m is None or not header_acs:
        problems.append(
            "生成物缺少 hash 溯源头（spec-hash / ac-hash 行）——非编译产物或头部被破坏"
        )
    else:
        if m.group(1) != spec_hash:
            problems.append(
                f"spec-hash 不符: 头部 {m.group(1)} ≠ 现算 {spec_hash}"
                "（spec 全文已变更——任何一字改动都改变全文 hash）"
            )
        current = {ac["id"]: ac_sha256(ac) for ac in acs}
        declared = dict(header_acs)
        for key in sorted(set(current) | set(declared)):
            if current.get(key) != declared.get(key):
                problems.append(
                    f"ac-hash 不符 [{key}]: 头部 {declared.get(key)} ≠ 现算 "
                    f"{current.get(key)}（判据文本已变更——改判据必须回 spec 层）"
                )
    # (3) 应然内容逐字节比对（手改正文、hash 头不动也检出）
    if actual != expected:
        problems.append(
            "生成物与应然内容不一致（确定性重渲染逐字节比对——hash 头未动的"
            "手改正文同样检出）"
        )
    return (not problems), problems


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        prog="pipeline/dsl/verify.py", description="spec hash 溯源校验（IR-0004 AC-8 gate）"
    )
    ap.add_argument("--generated", required=True, help="编译生成的测试骨架文件")
    ap.add_argument("--spec", required=True, help="来源 spec.md")
    args = ap.parse_args(argv)

    ok, problems = verify_files(args.generated, args.spec)
    if ok:
        print(
            f"[verify] GREEN: {args.generated}——spec-hash / 逐 AC hash / 应然内容 全部一致"
        )
        return 0
    print(f"[verify] RED: {args.generated}", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    print(f"  处置: {REMEDIATE}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
