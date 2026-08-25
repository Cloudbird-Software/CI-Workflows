#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compile.py —— 验收 DSL 编译器（Cloudbird-Software/CI-Workflows pipeline/dsl）。

IR-0004 AC-8 rev6 / IFACE-04：spec.md frontmatter 的 acceptanceCriteria
（YAML 子集：顶层 `acceptanceCriteria:` 列表，每项字段 id/given/when/then，
单行标量）机械编译为 pytest 骨架文件——每 AC 一个 test_<ac_id 小写下划线>
函数，docstring 载 given/when/then 原文，占位断言 assert False（由实施卡填充；
手改本生成物=CI 红，见 verify.py）。

确定性铁律（verify.py 的"应然内容"比对依赖于此）：
  生成物是 (spec.md 全文字节, AC 序列, --spec/--out 参数串) 的纯函数——
  无时间戳、无随机源、无环境渗漏；同输入字节级同输出。UTF-8 + LF。
  注意：--spec/--out 以调用参数原样嵌入 regenerate 行，编译与校验须以同形
  路径调用（组织约定：仓根相对路径）。

零第三方依赖（Python 3.11 标准库 only）：frontmatter 解析为本文件内置的
子集解析器；完整 YAML 结构校验仍归 spec-check.py（g010，AC-8"并存分工"），
本关卡只取 acceptanceCriteria 节。

用法:
  python pipeline/dsl/compile.py --spec specs/IR-0007/spec.md \
      --out specs/IR-0007/suite/generated/test_ir_0007.py
  python pipeline/dsl/compile.py --spec ... --out ... --check
      # 只校验不重生成（转发 verify.py；生成物缺失/不符=exit 1，fail-closed）

退出码: 0=绿  1=红（解析失败/校验红）  2=用法错误
"""
import argparse
import hashlib
import re
import sys
from pathlib import Path

TODO_MESSAGE = (
    "TODO: 由实施卡填充——手改本生成物=CI 红（hash 头校验），改判据必须回 spec 层"
)
HASH_RE = r"[0-9a-f]{64}"


class SpecError(Exception):
    """spec 解析失败（缺 frontmatter / 缺 acceptanceCriteria / 字段缺失）——友好可读。"""


# ---------------- 基础 ----------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_frontmatter(text: str, source: str = "<spec>") -> str:
    """提取 YAML frontmatter 块（首行 '---' 到下一个 '---'，不含围栏）。"""
    lines = normalize_newlines(text).split("\n")
    if not lines or lines[0].strip() != "---":
        raise SpecError(
            f"{source}: 缺少 YAML frontmatter（首行须为 '---'，以 '---' 行闭合）"
        )
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[1:idx])
    raise SpecError(f"{source}: frontmatter 未闭合（缺结束 '---' 行）")


def _scalar(seg: str) -> str:
    """单行标量：剥引号（'...' / "..."）。"""
    v = seg.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v


def extract_acceptance_criteria(fm_text: str, source: str = "<frontmatter>"):
    """从 frontmatter 文本提取 AC 序列（保持 spec 声明顺序）。

    支持子集：`acceptanceCriteria:` 顶层键 + `- key: value` 列表项（字段
    续行缩进 2 空格），值=单行标量。其余顶层键忽略；遇到下一个零缩进非
    列表行即节终止。字段 id/given/when/then 之外的字段忽略。
    """
    lines = normalize_newlines(fm_text).split("\n")
    start = None
    for idx, raw in enumerate(lines):
        if raw.rstrip() == "acceptanceCriteria:":
            start = idx + 1
            break
    if start is None:
        raise SpecError(
            f"{source}: frontmatter 缺少 acceptanceCriteria 节"
            "（需顶层键 acceptanceCriteria: 列表，字段 id/given/when/then 单行标量）"
        )
    acs = []
    cur = None
    for raw in lines[start:]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("- "):  # 新列表项（首字段随行）
            cur = {}
            acs.append(cur)
            _fill(cur, raw[2:].strip(), source)
        elif raw.startswith("  ") and cur is not None and ":" in raw:
            _fill(cur, raw.strip(), source)
        else:  # 下一个零缩进键 → 节终止
            break
    if not acs:
        raise SpecError(f"{source}: acceptanceCriteria 为空（至少须有一条 AC）")
    for pos, ac in enumerate(acs, 1):
        for field in ("id", "given", "when", "then"):
            if not ac.get(field, "").strip():
                raise SpecError(
                    f"{source}: 第 {pos} 条 AC 缺字段 {field} 或值为空"
                    "（子集限制：id/given/when/then 须单行标量）"
                )
    return acs


def _fill(cur, seg, source):
    if ":" not in seg:
        return
    key, _, value = seg.partition(":")
    key = key.strip()
    if key in ("id", "given", "when", "then"):
        cur[key] = _scalar(value)


def ac_sha256(ac) -> str:
    """单条 AC 的 hash：对 id\\0given\\0when\\0then 规范序列化取 sha256。"""
    payload = "\x00".join((ac["id"], ac["given"], ac["when"], ac["then"]))
    return sha256_hex(payload.encode("utf-8"))


def function_name(ac_id: str) -> str:
    """AC-8 → test_ac_8（小写、非标识符字符折叠为单下划线）。"""
    slug = re.sub(r"[^0-9A-Za-z]+", "_", ac_id).strip("_").lower()
    name = "test_" + slug
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise SpecError(f"AC id 无法生成合法测试函数名: {ac_id!r} → {name!r}")
    return name


# ---------------- 确定性渲染 ----------------

def _esc_doc(text: str) -> str:
    """docstring 安全转义（最小干预：仅反斜杠与三引号，保 given/when/then 原文）。"""
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def render(spec_hash: str, acs, spec_path: str, out_path: str) -> str:
    """生成物应然内容——纯函数（verify.py 逐字节比对依赖）。UTF-8、LF。"""
    seen = {}
    body = []
    for ac in acs:
        fname = function_name(ac["id"])
        if fname in seen:
            raise SpecError(f"函数名冲突: {ac['id']!r} 与 {seen[fname]!r} → {fname}")
        seen[fname] = ac["id"]
        body.append(f"def {fname}():")
        body.append(f'    """{ac["id"]}')
        body.append("")
        body.append(f"    given: {_esc_doc(ac['given'])}")
        body.append(f"    when: {_esc_doc(ac['when'])}")
        body.append(f"    then: {_esc_doc(ac['then'])}")
        body.append('    """')
        body.append(f'    assert False, "{TODO_MESSAGE}"')
        body.append("")
        body.append("")
    head = [
        "# AUTO-GENERATED FROM spec — DO NOT EDIT",
        "# 管辖: IR-0004 AC-8 / IFACE-04——hash 溯源由 pipeline/dsl/verify.py 校验，",
        "#       手改本文件=CI 红；改判据必须回 spec 层改 spec.md 后重新编译。",
        f"# spec-hash: {spec_hash}",
        "# ac-hash:",
    ]
    head += [f"#   - {ac['id']}: {ac_sha256(ac)}" for ac in acs]
    head.append(
        f"# regenerate: python pipeline/dsl/compile.py --spec {spec_path} --out {out_path}"
    )
    return "\n".join(head + ["", ""] + body)


def load_spec(spec_path: str):
    """读 spec → (spec_hash, acs)。hash 为全文原始字节的 sha256。"""
    p = Path(spec_path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise SpecError(f"spec 读取失败: {spec_path}: {e}") from e
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise SpecError(f"spec 非 UTF-8: {spec_path}: {e}") from e
    fm = split_frontmatter(text, str(spec_path))
    return sha256_hex(raw), extract_acceptance_criteria(fm, str(spec_path))


def write_generated(content: str, out_path: str) -> None:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


# ---------------- CLI ----------------

def run_check(spec_path: str, out_path: str) -> int:
    """--check：只校验不重生成（转发 verify.py，fail-closed）。"""
    try:
        from pipeline.dsl import verify as _verify  # 包内引用
    except ImportError:
        try:
            import verify as _verify  # 脚本直跑（sys.path[0]=pipeline/dsl）
        except ImportError as e:
            print(f"[compile] RED: 校验器不可用: {e}", file=sys.stderr)
            return 1
    return _verify.main(["--generated", str(out_path), "--spec", str(spec_path)])


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        prog="pipeline/dsl/compile.py", description="验收 DSL 编译器（IR-0004 AC-8）"
    )
    ap.add_argument("--spec", required=True, help="spec.md 路径（含 frontmatter）")
    ap.add_argument("--out", required=True, help="生成 pytest 骨架文件路径")
    ap.add_argument(
        "--check", action="store_true",
        help="只校验已存在的生成物（转发 verify.py），不重生成",
    )
    args = ap.parse_args(argv)
    if args.check:
        return run_check(args.spec, args.out)
    try:
        spec_hash, acs = load_spec(args.spec)
        content = render(spec_hash, acs, args.spec, args.out)
        write_generated(content, args.out)
    except SpecError as e:
        print(f"[compile] RED: {e}", file=sys.stderr)
        return 1
    print(
        f"[compile] GREEN: {args.out}（AC×{len(acs)}, "
        f"spec-hash {spec_hash[:12]}…）——占位断言由实施卡填充"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
