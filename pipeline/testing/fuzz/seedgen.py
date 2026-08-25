#!/usr/bin/env python3
"""seedgen —— schema 感知边界值种子生成器（IR-0004 AC-3，纯机械）。

读 JSON Schema，按内置生成器类（15 类 ≥ 规格要求的 ~12 类）产出边界值种子集：

  1  empty              空值/空容器（{} / [] / "" / 0）
  2  min_boundary       最小边界精确命中（minLength/minItems/minimum）
  3  max_boundary       最大边界精确命中（maxLength/maxItems/maximum）
  4  below_min          越下界 1
  5  above_max          越上界 1
  6  unicode_edge       Unicode 边界（组合符/星面/RTL/零宽/NUL/多语）
  7  type_confusion     类型混淆（字段替换为错误类型值）
  8  deep_nesting       嵌套深度极限（32/64 层）
  9  numeric_extremes   数值极值（0/-1/1e18/1.79e308/-0.0）
  10 null_values        全 null
  11 long_string        超长字符串（10000 字符）
  12 missing_required   缺失必填字段（逐个）
  13 extra_unknown_field 多余未知字段
  14 enum_boundary      枚举首/末/非法值
  15 whitespace_only    仅空白字符串

支持的 JSON Schema 关键字（子集）：type/properties/required/items/enum/
minimum/maximum/minLength/maxLength/minItems/maxItems/$defs/$ref(#/$defs/...)。

CLI：
  python seedgen.py --schema schema.json --out-dir corpus/
  python seedgen.py --list-classes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

try:  # 双模式导入：包内 / 独立脚本
    from pipeline.testing import _yamlmini  # noqa: F401  (确保包路径可用)
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

SEED_CLASSES = [
    ("empty", "空值/空容器（{} / [] / 空串 / 0）", 1),
    ("min_boundary", "最小边界精确命中 minLength/minItems/minimum", 1),
    ("max_boundary", "最大边界精确命中 maxLength/maxItems/maximum", 1),
    ("below_min", "越下界 1（min-1）", 1),
    ("above_max", "越上界 1（max+1）", 1),
    ("unicode_edge", "Unicode 边界：组合符/星面/RTL/零宽/NUL/多语种", 6),
    ("type_confusion", "类型混淆：字段替换为声明类型之外的值", 6),
    ("deep_nesting", "嵌套深度极限：32/64 层嵌套数组", 2),
    ("numeric_extremes", "数值极值：0/-1/1e18/1.79e308/-0.0", 5),
    ("null_values", "所有字段置 null", 1),
    ("long_string", "超长字符串 10000 字符", 1),
    ("missing_required", "逐个缺失必填字段", 3),
    ("extra_unknown_field", "注入未知多余字段", 1),
    ("enum_boundary", "枚举首值/末值/非法值", 3),
    ("whitespace_only", "仅空白字符字符串（空格/制表/换行）", 1),
]

UNICODE_VALUES = [
    "e\u0301",              # NFD 分解形式（组合符）
    "\U0001D54F",           # 星面辅助平面（代理对边界）
    "\u202eba\u202c",       # RTL 覆盖 + POP 恢复
    "a\u200bb",             # 零宽空格
    "\u0000x",              # NUL 字节
    "\u4e2d\u6587\u3052\u3099\u00e9",  # CJK + 五十音浊点组合 + 重音符
]

NUMERIC_EXTREMES = [0, -1, 10 ** 18, 1.7976931348623157e308, -0.0]

WRONG_TYPE_VALUES = [12345, "42", 4.5, True, ["x"], {"x": 1}, None]

DEFAULT_STRING_MAX = 64


def json_type_of(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise ValueError("未知值类型 %r" % type(value))


def resolve_ref(schema, root):
    hops = 0
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise ValueError("只支持本地指针 $ref（#/$defs/...），收到 %r" % ref)
        node = root
        for part in ref[2:].split("/"):
            try:
                node = node[part]
            except (KeyError, TypeError) as exc:
                raise ValueError("$ref 指针无法解析: %r" % ref) from exc
        schema = node
        hops += 1
        if hops > 8:
            raise ValueError("$ref 解析超过 8 跳，疑似自引用")
    return schema


def declared_type(schema, root):
    schema = resolve_ref(schema, root)
    t = schema.get("type")
    if isinstance(t, list):
        for cand in t:
            if cand != "null":
                return cand
        return t[0] if t else "string"
    if t:
        return t
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    if "enum" in schema and schema["enum"]:
        return json_type_of(schema["enum"][0])
    return "string"


# --------------------------------------------------------------- 标量构建


def _string_value(schema, cls, variant):
    if "enum" in schema and cls not in ("enum_boundary", "type_confusion", "null_values"):
        enum = schema["enum"]
        return enum[variant % len(enum)]
    min_len = schema.get("minLength")
    max_len = schema.get("maxLength")
    if cls == "empty":
        return ""
    if cls == "null_values":
        return None
    if cls == "unicode_edge":
        return UNICODE_VALUES[variant % len(UNICODE_VALUES)]
    if cls == "long_string":
        return "x" * 10000
    if cls == "whitespace_only":
        return " \t\n"
    if cls == "enum_boundary":
        enum = schema.get("enum")
        if enum:
            return [enum[0], enum[-1], "ENUM-INVALID-\u2603"][variant % 3]
        return "ENUM-INVALID-\u2603"
    if cls == "min_boundary":
        return "a" * max(1, min_len or 1)
    if cls == "max_boundary":
        return "a" * (max_len or DEFAULT_STRING_MAX)
    if cls == "below_min":
        return "a" * max(0, (min_len or 1) - 1)
    if cls == "above_max":
        return "a" * ((max_len or DEFAULT_STRING_MAX) + 1)
    return "a" * max(1, min(min_len or 1, 16))


def _number_value(schema, cls, variant, integer):
    lo = schema.get("minimum")
    hi = schema.get("maximum")
    if cls == "empty":
        return 0
    if cls == "null_values":
        return None
    if cls == "numeric_extremes":
        value = NUMERIC_EXTREMES[variant % len(NUMERIC_EXTREMES)]
        return int(value) if integer and isinstance(value, float) and value.is_integer() else value
    if cls == "min_boundary":
        return lo if lo is not None else 0
    if cls == "max_boundary":
        return hi if hi is not None else 1000
    if cls == "below_min":
        return (lo if lo is not None else 0) - 1
    if cls == "above_max":
        return (hi if hi is not None else 1000) + 1
    return 1


def _scalar_value(schema, cls, variant, root):
    t = declared_type(schema, root)
    if cls == "null_values":
        return None
    if t == "string":
        return _string_value(schema, cls, variant)
    if t == "integer":
        return _number_value(schema, cls, variant, integer=True)
    if t == "number":
        return _number_value(schema, cls, variant, integer=False)
    if t == "boolean":
        return False if cls == "empty" else True
    if t == "null":
        return None
    return "a"


# --------------------------------------------------------------- 结构构建


def _base_object(schema, cls, variant, root):
    props = schema.get("properties", {})
    return {key: value_for(prop, cls, variant, root) for key, prop in props.items()}


def _object_value(schema, cls, variant, root):
    props = schema.get("properties", {})
    required = schema.get("required", []) or []
    if cls == "empty":
        return {}
    if cls == "missing_required":
        if not required:
            return _base_object(schema, "default", variant, root)
        drop = required[variant % len(required)]
        doc = _base_object(schema, "default", variant, root)
        doc.pop(drop, None)
        return doc
    if cls == "extra_unknown_field":
        doc = _base_object(schema, "default", variant, root)
        doc["__extra__"] = {"injected": True}
        return doc
    if cls == "deep_nesting":
        depth = (32, 64)[variant % 2]
        nested = "x"
        for _ in range(depth):
            nested = [nested]
        if props:
            doc = _base_object(schema, "default", variant, root)
            first = sorted(props)[0]
            doc[first] = nested
            return doc
        return nested
    return _base_object(schema, cls, variant, root)


def _array_value(schema, cls, variant, root):
    item = schema.get("items", {})
    min_items = schema.get("minItems")
    max_items = schema.get("maxItems")
    if cls == "empty":
        return []
    if cls == "null_values":
        return None
    if cls == "min_boundary":
        n = max(1, min_items or 1)
    elif cls == "max_boundary":
        n = max_items or 4
    elif cls == "below_min":
        n = max(0, (min_items or 1) - 1)
    elif cls == "above_max":
        n = (max_items or 4) + 1
    else:
        n = 2
    return [value_for(item, cls, variant + i, root) for i in range(n)]


def _object_type_confusion(schema, variant, root):
    """对象级类型混淆：仅替换一个字段为声明类型之外的值，其余字段取默认。"""
    props = schema.get("properties", {})
    doc = _base_object(schema, "default", variant, root)
    if not props:
        return _wrong_value("object", variant)
    field = sorted(props)[variant % len(props)]
    doc[field] = _wrong_value(declared_type(props[field], root), variant)
    return doc


def value_for(schema, cls, variant, root):
    schema = resolve_ref(schema, root)
    t = declared_type(schema, root)
    if cls == "type_confusion":
        if t == "object":
            return _object_type_confusion(schema, variant, root)
        return _wrong_value(t, variant)
    if t == "object":
        return _object_value(schema, cls, variant, root)
    if t == "array":
        return _array_value(schema, cls, variant, root)
    return _scalar_value(schema, cls, variant, root)


def _wrong_value(declared, variant):
    candidates = [v for v in WRONG_TYPE_VALUES if not _type_accepts(declared, v)]
    if not candidates:
        return None
    return candidates[variant % len(candidates)]


def _type_accepts(declared, value):
    jt = json_type_of(value)
    if declared == "number":
        return jt in ("number", "integer")
    return jt == declared


# --------------------------------------------------------------- 生成入口


def generate(schema, max_seeds=None):
    """返回 [(seed_class, variant, value)] 列表（纯机械，无随机）。"""
    seeds = []
    for cls, _desc, variants in SEED_CLASSES:
        for variant in range(variants):
            seeds.append((cls, variant, value_for(schema, cls, variant, schema)))
    if max_seeds is not None:
        seeds = seeds[:max_seeds]
    return seeds


def write_corpus(schema, out_dir, max_seeds=None):
    """写种子文件 + manifest.json，返回 manifest dict。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    seeds = generate(schema, max_seeds=max_seeds)
    manifest_seeds = []
    for index, (cls, variant, value) in enumerate(seeds, start=1):
        name = "seed-%04d-%s.json" % (index, cls)
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        (out / name).write_text(text, encoding="utf-8", newline="\n")
        manifest_seeds.append(
            {
                "file": name,
                "seed_class": cls,
                "variant": variant,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    manifest = {
        "generator": "pipeline/testing/fuzz/seedgen.py",
        "schema_classes": len(SEED_CLASSES),
        "seed_count": len(manifest_seeds),
        "seeds": manifest_seeds,
    }
    # 注意文件名与 corpus.py 的 manifest.json 区分，避免同目录混用时清单互相覆盖
    (out / "seedgen-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="schema 感知边界值种子生成器（AC-3）")
    parser.add_argument("--schema", help="JSON Schema 文件路径")
    parser.add_argument("--out-dir", default="corpus", help="种子输出目录（默认 corpus/）")
    parser.add_argument("--max-seeds", type=int, default=None, help="种子数上限（调试用）")
    parser.add_argument("--list-classes", action="store_true", help="列出内置生成器类")
    args = parser.parse_args(argv)

    if args.list_classes:
        for cls, desc, variants in SEED_CLASSES:
            print("%-20s x%d  %s" % (cls, variants, desc))
        print("total classes: %d" % len(SEED_CLASSES))
        return 0
    if not args.schema:
        parser.error("--schema 必填（或用 --list-classes）")

    schema_path = Path(args.schema)
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print("FATAL: schema 读取/解析失败: %s" % exc, file=sys.stderr)
        return 2

    try:
        manifest = write_corpus(schema, args.out_dir, max_seeds=args.max_seeds)
    except ValueError as exc:
        print("FATAL: schema 遍历失败: %s" % exc, file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "schema": str(schema_path),
                "out_dir": str(Path(args.out_dir)),
                "seed_classes": manifest["schema_classes"],
                "seeds": manifest["seed_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
