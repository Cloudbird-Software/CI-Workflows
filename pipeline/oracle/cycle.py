#!/usr/bin/env python3
"""oracle 换代脚本（Cloudbird-Software · IR-0004 rev6 · AC-11 换代机械化）。

只换代，不修补——机械执行，无自由裁量：
  --old <sha>：定位 frozen_sha==old 且 status==frozen 的旧代条目 → status=retired
  --new <sha>：若存在 frozen_sha==new 的 candidate 条目 → 向其 generations 追加
               {sha:new, frozen_at, note} 并置 status=frozen；若不存在 → 克隆旧代
               元数据创建新条目（frozen_sha=new，generations=[{sha:new}]，status=frozen）。
  旧代条目的 generations 全程不动（只追加语义）；任何校验失败即拒绝落盘。

用法：
  python oracle/cycle.py --registry <yaml> --old <old40hex> --new <new40hex> [--note T]

退出码：0=换代完成；1=旧代不存在/非 frozen、新代非 candidate、形状非法、注册表非法。
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    from .registry import HEX40_RE, load_registry, now_iso, save_registry, validate_registry
except ImportError:  # 以脚本方式直接运行
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(_here))
    sys.path.insert(0, _here)
    from registry import HEX40_RE, load_registry, now_iso, save_registry, validate_registry

COPY_FIELDS = ("name", "host_repo", "target_surface", "cluster",
               "decorrelation_reason", "hard_zone", "soft_zone")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cycle.py", description="oracle 换代（只追加，不修补）")
    parser.add_argument("--registry", required=True, help="注册表 YAML 路径")
    parser.add_argument("--old", required=True, help="旧代 frozen_sha（须为当前 frozen 条目）")
    parser.add_argument("--new", required=True, help="新代 sha（40 位小写十六进制）")
    parser.add_argument("--note", default="", help="换代备注（默认 cycle <old8>→<new8>）")
    args = parser.parse_args(argv)

    for label, sha in (("--old", args.old), ("--new", args.new)):
        if not HEX40_RE.match(sha):
            print("cycle: %s 形状非法（须 40 位小写十六进制）: %r" % (label, sha), file=sys.stderr)
            return 1
    if args.old == args.new:
        print("cycle: --old 与 --new 相同，无换代可言", file=sys.stderr)
        return 1

    data, err = load_registry(args.registry)
    if err:
        print("cycle: %s" % err, file=sys.stderr)
        return 1
    errors = validate_registry(data)
    if errors:
        for e in errors:
            print("cycle: 注册表非法 %s" % e, file=sys.stderr)
        return 1

    entries = data["entries"]
    olds = [e for e in entries if e.get("frozen_sha") == args.old]
    if len(olds) != 1 or olds[0].get("status") != "frozen":
        print("cycle: 旧代 %s… 不唯一或 status 非 frozen（换代只从 frozen 起步）" % args.old[:8],
              file=sys.stderr)
        return 1
    old_entry = olds[0]

    note = args.note or ("cycle %s->%s" % (args.old[:8], args.new[:8]))
    stamped = now_iso()

    news = [e for e in entries if e.get("frozen_sha") == args.new]
    old_entry["status"] = "retired"  # 仅内存修改；任何失败路径都不落盘
    if news:
        new_entry = news[0]
        if new_entry.get("status") != "candidate":
            print("cycle: 新代 %s… 的 status=%r，非 candidate（只换代不修补，拒绝执行）"
                  % (args.new[:8], new_entry.get("status")), file=sys.stderr)
            return 1
        gens = new_entry["generations"]
        if gens and gens[-1].get("sha") == args.new:
            pass  # 首代已登记过（如注册时即冻结），仅提升状态，避免重复追加
        elif any(g.get("sha") == args.new for g in gens):
            print("cycle: 新代 sha 已存在于历史中段（只追加，不回滚）", file=sys.stderr)
            return 1
        else:
            gens.append({"sha": args.new, "frozen_at": stamped, "note": note})
        new_entry["status"] = "frozen"
        target = new_entry
    else:
        clone = {k: old_entry[k] for k in COPY_FIELDS}
        clone["frozen_sha"] = args.new
        clone["status"] = "frozen"
        clone["generations"] = [{"sha": args.new, "frozen_at": stamped, "note": note}]
        entries.append(clone)
        target = clone

    errors = validate_registry(data)
    if errors:
        for e in errors:
            print("cycle: 换代后校验失败（不落盘）%s" % e, file=sys.stderr)
        return 1
    if not save_registry(args.registry, data):
        return 1
    print("cycled: %s… (retired) -> %s… (frozen, generations=%d)"
          % (args.old[:8], args.new[:8], len(target["generations"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
