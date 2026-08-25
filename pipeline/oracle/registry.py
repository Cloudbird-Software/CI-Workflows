#!/usr/bin/env python3
"""oracle 注册表读写与校验（Cloudbird-Software · IR-0004 rev6 · AC-11）。

契约见 oracle-registry.schema.yaml；本模块是其校验器与读写器。
PM 优先范式：oracle 制作可选，但注册表接口必须存在且可用。

用法：
  python oracle/registry.py --registry <yaml> validate
  python oracle/registry.py --registry <yaml> register --name N --host-repo O/R \
      --target-surface S --frozen-sha <40hex> --cluster C --decorrelation-reason D \
      [--hard-zone GLOB ...] [--soft-zone GLOB ...] [--status candidate|frozen] [--note T]
  python oracle/registry.py --registry <yaml> retire (--name N | --frozen-sha <sha>)

退出码：0=成功（含幂等命中）；1=畸形注册表 / 非法操作。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

try:
    from .miniyaml import MiniYAMLError, dump_yaml, load_yaml
except ImportError:  # 以脚本方式直接运行（python oracle/registry.py ...）
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from miniyaml import MiniYAMLError, dump_yaml, load_yaml

STATUSES = ("candidate", "frozen", "retired")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
ENTRY_FIELDS = ("name", "host_repo", "target_surface", "frozen_sha", "cluster",
                "decorrelation_reason", "hard_zone", "soft_zone", "status", "generations")
STR_FIELDS = ("name", "host_repo", "target_surface", "frozen_sha", "cluster", "decorrelation_reason")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_registry(data):
    """按 oracle-registry.schema.yaml 校验；返回错误列表（空列表=合法）。

    覆盖：必填/类型、frozen_sha 形状（40 位小写十六进制）、status 枚举、
    换代只追加（历史条目不可变：sha 不得重复、frozen_at 不得倒序、
    frozen/retired 条目的最新一代必须等于 frozen_sha——篡改历史即在此检出）。
    未知字段忽略（前向兼容）。
    """
    errors = []
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return ["顶层必须是含 entries 列表的映射"]
    seen_pairs = set()
    for idx, entry in enumerate(data["entries"]):
        tag = "entries[%d]" % idx
        if not isinstance(entry, dict):
            errors.append("%s: 条目必须是映射" % tag)
            continue
        for field in ENTRY_FIELDS:
            if field not in entry:
                errors.append("%s.%s: 必填字段缺失" % (tag, field))
        for field in STR_FIELDS:
            if field in entry and (not isinstance(entry.get(field), str) or not entry[field].strip()):
                errors.append("%s.%s: 必须是非空字符串" % (tag, field))
        sha = entry.get("frozen_sha")
        if isinstance(sha, str) and not HEX40_RE.match(sha):
            errors.append("%s.frozen_sha: 形状非法（须 40 位小写十六进制）: %r" % (tag, sha))
        status = entry.get("status")
        if "status" in entry and status not in STATUSES:
            errors.append("%s.status: 非法枚举 %r（只允许 %s）" % (tag, status, "|".join(STATUSES)))
        for zfield in ("hard_zone", "soft_zone"):
            if zfield not in entry:
                continue
            zones = entry.get(zfield)
            if not isinstance(zones, list):
                errors.append("%s.%s: 必须是 glob 列表" % (tag, zfield))
                continue
            for gi, g in enumerate(zones):
                if not isinstance(g, str) or not g.strip():
                    errors.append("%s.%s[%d]: glob 必须是非空字符串" % (tag, zfield, gi))
        gens = entry.get("generations")
        if "generations" in entry and not isinstance(gens, list):
            errors.append("%s.generations: 必须是列表" % tag)
            gens = None
        if isinstance(gens, list):
            prev_at = None
            seen_sha = set()
            for gi, gen in enumerate(gens):
                gt = "%s.generations[%d]" % (tag, gi)
                gsha = gen.get("sha") if isinstance(gen, dict) else None
                if not isinstance(gsha, str) or not HEX40_RE.match(gsha):
                    errors.append("%s.sha: 形状非法（须 40 位小写十六进制）" % gt)
                else:
                    if gsha in seen_sha:
                        errors.append("%s.sha: 换代历史内 sha 重复（只追加，不得改写历史）" % gt)
                    seen_sha.add(gsha)
                frozen_at = gen.get("frozen_at") if isinstance(gen, dict) else None
                if not isinstance(frozen_at, str) or not frozen_at.strip():
                    errors.append("%s.frozen_at: 必填" % gt)
                else:
                    if prev_at is not None and frozen_at < prev_at:
                        errors.append("%s.frozen_at: 时间倒序（历史条目不可改写）" % gt)
                    prev_at = frozen_at if prev_at is None else max(prev_at, frozen_at)
                if isinstance(gen, dict) and "note" in gen and not isinstance(gen["note"], str):
                    errors.append("%s.note: 必须是字符串" % gt)
            if status in ("frozen", "retired"):
                if not gens:
                    errors.append("%s: status=%s 时 generations 不得为空" % (tag, status))
                elif isinstance(sha, str) and HEX40_RE.match(sha):
                    last = gens[-1].get("sha") if isinstance(gens[-1], dict) else None
                    if last != sha:
                        errors.append(
                            "%s: 换代不变量破坏——最新一代 %r != frozen_sha %r"
                            "（历史被篡改，或换代未按只追加语义执行）" % (tag, last, sha))
        pair = (entry.get("name"), entry.get("frozen_sha"))
        if pair in seen_pairs:
            errors.append("%s: (name, frozen_sha) 重复" % tag)
        seen_pairs.add(pair)
    return errors


def load_registry(path):
    """读取并解析注册表；返回 (data, None) 或 (None, 错误消息)。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return None, "无法读取注册表 %s: %s" % (path, exc)
    try:
        data = load_yaml(text)
    except MiniYAMLError as exc:
        return None, "YAML 解析失败 %s: %s" % (path, exc)
    return data, None


def save_registry(path, data):
    """写前校验由调用方完成；此处原子写出并读回复验（不静默）。成功返回 True。"""
    text = dump_yaml(data)
    directory = os.path.dirname(os.path.abspath(path))
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".registry-", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
        tmp = None
    except OSError as exc:
        print("registry: 写入失败 %s" % exc, file=sys.stderr)
        return False
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)
    data2, err = load_registry(path)
    if err or validate_registry(data2):
        print("registry: 写后读回校验失败（不静默放行）", file=sys.stderr)
        return False
    return True


def _build_entry(args):
    gens = []
    if args.status == "frozen":
        gens.append({"sha": args.frozen_sha, "frozen_at": now_iso(),
                     "note": args.note or "registered-frozen"})
    return {
        "name": args.name,
        "host_repo": args.host_repo,
        "target_surface": args.target_surface,
        "frozen_sha": args.frozen_sha,
        "cluster": args.cluster,
        "decorrelation_reason": args.decorrelation_reason,
        "hard_zone": list(args.hard_zone),
        "soft_zone": list(args.soft_zone),
        "status": args.status,
        "generations": gens,
    }


def _main(argv=None):
    parser = argparse.ArgumentParser(prog="registry.py", description="oracle 注册表读写与校验（AC-11）")
    parser.add_argument("--registry", required=True, help="注册表 YAML 路径")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="schema 校验（畸形→exit 1）")
    pr = sub.add_parser("register", help="新增条目（幂等，写前校验）")
    pr.add_argument("--name", required=True)
    pr.add_argument("--host-repo", required=True)
    pr.add_argument("--target-surface", required=True)
    pr.add_argument("--frozen-sha", required=True)
    pr.add_argument("--cluster", required=True)
    pr.add_argument("--decorrelation-reason", required=True)
    pr.add_argument("--hard-zone", action="append", default=[], metavar="GLOB")
    pr.add_argument("--soft-zone", action="append", default=[], metavar="GLOB")
    pr.add_argument("--status", choices=STATUSES, default="candidate")
    pr.add_argument("--note", default="")
    pt = sub.add_parser("retire", help="status→retired（只允许从 frozen）")
    pt.add_argument("--name")
    pt.add_argument("--frozen-sha")
    args = parser.parse_args(argv)

    data, err = load_registry(args.registry)
    if err:
        if args.cmd == "register" and not os.path.exists(args.registry):
            data = {"entries": []}  # 首次注册：允许新建注册表
        else:
            print("registry: %s" % err, file=sys.stderr)
            return 1
    errors = validate_registry(data)
    if errors:
        for e in errors:
            print("registry: 校验失败 %s" % e, file=sys.stderr)
        return 1

    if args.cmd == "validate":
        print("OK: %d 个条目全部合法" % len(data["entries"]))
        return 0

    if args.cmd == "register":
        if not HEX40_RE.match(args.frozen_sha):
            print("registry: --frozen-sha 形状非法（须 40 位小写十六进制）", file=sys.stderr)
            return 1
        for e in data["entries"]:
            if e.get("name") == args.name and e.get("frozen_sha") == args.frozen_sha:
                print("幂等命中：name=%s frozen_sha=%s 已存在，不重复写入"
                      % (args.name, args.frozen_sha[:8] + "…"))
                return 0
        data["entries"].append(_build_entry(args))
        errors = validate_registry(data)  # 写前校验
        if errors:
            for e in errors:
                print("registry: 写前校验失败 %s" % e, file=sys.stderr)
            return 1
        if not save_registry(args.registry, data):
            return 1
        print("registered: name=%s frozen_sha=%s status=%s"
              % (args.name, args.frozen_sha[:8] + "…", args.status))
        return 0

    if args.cmd == "retire":
        matches = [e for e in data["entries"]
                   if (args.name is None or e.get("name") == args.name)
                   and (args.frozen_sha is None or e.get("frozen_sha") == args.frozen_sha)]
        if not matches:
            print("registry: retire 未匹配到条目（--name / --frozen-sha）", file=sys.stderr)
            return 1
        if len(matches) > 1:
            print("registry: retire 匹配到 %d 个条目，须唯一（可加 --frozen-sha 收窄）" % len(matches),
                  file=sys.stderr)
            return 1
        target = matches[0]
        if target.get("status") != "frozen":
            print("registry: retire 只允许从 frozen 迁移，当前 status=%r" % target.get("status"),
                  file=sys.stderr)
            return 1
        target["status"] = "retired"
        errors = validate_registry(data)
        if errors:
            for e in errors:
                print("registry: 退役后校验失败 %s" % e, file=sys.stderr)
            return 1
        if not save_registry(args.registry, data):
            return 1
        print("retired: name=%s frozen_sha=%s（generations 保持只追加，未改动）"
              % (target["name"], target["frozen_sha"][:8] + "…"))
        return 0
    return 1


def main(argv=None):
    return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
