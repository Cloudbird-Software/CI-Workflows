"""seedgen：schema 感知边界种子生成（AC-3）——边界覆盖断言。"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.testing.fuzz import seedgen

SCHEMA = json.loads(
    Path(__file__).resolve().parent.joinpath("fixtures", "fuzz-schema.json").read_text(encoding="utf-8")
)


class TestSeedgen(unittest.TestCase):
    def test_generator_class_count(self):
        self.assertGreaterEqual(len(seedgen.SEED_CLASSES), 12)
        self.assertEqual(len({c for c, _, _ in seedgen.SEED_CLASSES}), len(seedgen.SEED_CLASSES))

    def test_generate_boundary_coverage(self):
        seeds = {(cls, variant): value for cls, variant, value in seedgen.generate(SCHEMA)}
        # 空/极小/极大/越界
        self.assertEqual(seeds[("empty", 0)], {})
        mins = seeds[("min_boundary", 0)]
        self.assertEqual(mins["id"], "aaa")            # minLength=3
        self.assertEqual(mins["quantity"], 1)           # minimum=1
        self.assertEqual(len(mins["tags"]), 1)          # minItems=1
        maxs = seeds[("max_boundary", 0)]
        self.assertEqual(len(maxs["id"]), 10)           # maxLength=10
        self.assertEqual(maxs["quantity"], 100)
        self.assertEqual(len(maxs["tags"]), 4)
        below = seeds[("below_min", 0)]
        self.assertEqual(len(below["id"]), 2)           # minLength-1
        self.assertEqual(below["quantity"], 0)
        self.assertEqual(below["tags"], [])
        above = seeds[("above_max", 0)]
        self.assertEqual(len(above["id"]), 11)
        self.assertEqual(above["quantity"], 101)
        self.assertEqual(len(above["tags"]), 5)

    def test_unicode_and_long_and_null_seeds(self):
        by_cls_variant = {}
        for cls, variant, value in seedgen.generate(SCHEMA):
            by_cls_variant[(cls, variant)] = value
        self.assertEqual(by_cls_variant[("unicode_edge", 0)]["id"], "e\u0301")
        self.assertEqual(by_cls_variant[("unicode_edge", 5)]["id"], "\u4e2d\u6587\u3052\u3099\u00e9")
        self.assertEqual(len(by_cls_variant[("long_string", 0)]["id"]), 10000)
        self.assertIsNone(by_cls_variant[("null_values", 0)]["id"])
        self.assertEqual(by_cls_variant[("whitespace_only", 0)]["id"], " \t\n")

    def test_type_confusion_wrong_types(self):
        confused = [(variant, value) for cls, variant, value in seedgen.generate(SCHEMA)
                    if cls == "type_confusion"]
        declared = {"id": "string", "quantity": "integer", "label": "string",
                    "tags": "array", "meta": "object", "price": "number"}
        fields = sorted(declared)
        hits = 0
        for variant, doc in confused:
            self.assertIsInstance(doc, dict)
            field = fields[variant % len(fields)]          # 与实现同一选择规则
            wrong = doc[field]
            jt = seedgen.json_type_of(wrong)
            if declared[field] == "number":
                self.assertNotIn(jt, ("number", "integer"))
            else:
                self.assertNotEqual(jt, declared[field], "%s 得到 %r（variant=%d）" % (field, wrong, variant))
            hits += 1
        self.assertGreaterEqual(hits, 6)
        self.assertEqual(len(confused), 6)

    def test_deep_nesting_and_missing_required_and_extra(self):
        seeds = {cls: value for cls, variant, value in seedgen.generate(SCHEMA)}

        def depth(node):
            if isinstance(node, list):
                return 1 + max((depth(x) for x in node), default=0)
            return 0

        nesting_docs = [v for c, var, v in seedgen.generate(SCHEMA) if c == "deep_nesting"]
        self.assertEqual(sorted(depth(d["id"]) for d in nesting_docs), [32, 64])
        # 逐个缺失必填（3 条）：每条少一个 required 键
        missing_docs = [v for c, var, v in seedgen.generate(SCHEMA) if c == "missing_required"]
        base_keys = set(SCHEMA["properties"])
        dropped = set()
        for doc in missing_docs:
            dropped |= base_keys - set(doc)
        self.assertEqual(dropped, set(SCHEMA["required"]))
        extra = seeds["extra_unknown_field"]
        self.assertIn("__extra__", extra)

    def test_enum_boundary(self):
        enum_docs = [v for c, var, v in seedgen.generate(SCHEMA) if c == "enum_boundary"]
        values = {d["label"] for d in enum_docs}
        self.assertIn("alpha", values)                  # 首值
        self.assertIn("gamma", values)                  # 末值
        self.assertIn("ENUM-INVALID-\u2603", values)    # 非法值

    def test_write_corpus_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = seedgen.write_corpus(SCHEMA, tmp)
            files = list(Path(tmp).glob("seed-*.json"))
            self.assertEqual(len(files), manifest["seed_count"])
            self.assertGreaterEqual(manifest["seed_count"], 25)
            self.assertGreaterEqual(manifest["schema_classes"], 12)
            # 每个种子都是合法 JSON 且带边界类标记
            for entry in manifest["seeds"]:
                json.loads(Path(tmp, entry["file"]).read_text(encoding="utf-8"))
            self.assertEqual(len({e["seed_class"] for e in manifest["seeds"]}), 15)


if __name__ == "__main__":
    unittest.main()
