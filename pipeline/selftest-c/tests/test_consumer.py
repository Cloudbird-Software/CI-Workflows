"""consumer.py 测试：空目录 exit 0、append 链断裂 exit 1、攻击查询生成、SHA 作废。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.testlib import BASE_SHA, OTHER_BASE
from fanout import consumer


def chain_lines(records):
    """按 consumer 的链规则生成合法 JSONL 文本。"""
    prev = consumer.GENESIS
    out = []
    for rec in records:
        rec = dict(rec)
        rec["prev_hash"] = prev
        out.append(json.dumps(rec, ensure_ascii=False))
        prev = consumer.record_hash(rec)
    return "\n".join(out) + "\n"


RECORDS = [
    {"card_id": "C-1", "spec_hash": "s" * 40, "base_sha": BASE_SHA,
     "type": "assumption", "payload": {"text": "假设：分词器不处理宽字符"}},
    {"card_id": "C-2", "spec_hash": "s" * 40, "base_sha": BASE_SHA,
     "type": "eliminated_route",
     "payload": {"route": "正则替代手写分词", "points": ["宽字符计数差异", "边界符缺失回退"]}},
]


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "products"
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, extra=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = consumer.main(["--products-dir", str(self.dir)] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def test_empty_dir_with_empty_ok_exit_0(self):
        code, out, err = self.run_main(["--empty-ok"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out),
                         {"consumed": 0, "reason": "no products (fan-out 未使用)"})

    def test_empty_dir_without_flag_exit_1(self):
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("不得静默停摆", err)

    def test_missing_dir_exit_1(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = consumer.main(["--products-dir", str(self.dir / "nope")])
        self.assertEqual(code, 1)
        self.assertIn("目录不存在", err.getvalue())

    def test_valid_chain_and_attack_queries(self):
        (self.dir / "cards.jsonl").write_text(chain_lines(RECORDS),
                                              encoding="utf-8", newline="\n")
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["consumed"], 2)
        queries = [q["query"] for q in result["attack_queries"]]
        self.assertEqual(len(queries), 3)
        self.assertIn("champion 是否覆盖：正则替代手写分词", queries)
        self.assertIn("champion 是否覆盖：宽字符计数差异", queries)
        self.assertIn("champion 是否覆盖：边界符缺失回退", queries)
        self.assertTrue(all(q["card_id"] == "C-2"
                            for q in result["attack_queries"]))

    def test_broken_chain_exit_1(self):
        text = chain_lines(RECORDS)
        text = text.replace('"C-1"', '"C-9"', 1)  # 篡改首行 → 次行 prev_hash 断链
        (self.dir / "cards.jsonl").write_text(text, encoding="utf-8", newline="\n")
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("断裂", out)

    def test_genesis_prev_required(self):
        rec = dict(RECORDS[0])
        rec["prev_hash"] = "deadbeef" * 8  # 首行必须 64 个 '0'
        (self.dir / "cards.jsonl").write_text(
            json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("断裂", out)

    def test_base_sha_mismatch_invalidated_exit_1(self):
        (self.dir / "cards.jsonl").write_text(chain_lines(RECORDS),
                                              encoding="utf-8", newline="\n")
        code, out, _ = self.run_main(["--expect-base-sha", OTHER_BASE])
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual(len(result["invalidations"]), 2)  # 作废留痕
        self.assertIn("作废", json.dumps(result["invalidations"], ensure_ascii=False))

    def test_base_sha_match_exit_0(self):
        (self.dir / "cards.jsonl").write_text(chain_lines(RECORDS),
                                              encoding="utf-8", newline="\n")
        code, out, _ = self.run_main(["--expect-base-sha", BASE_SHA])
        self.assertEqual(code, 0)
        self.assertNotIn("invalidations", json.loads(out))

    def test_bad_type_exit_1(self):
        rec = dict(RECORDS[0])
        rec["type"] = "vibe_check"
        rec["prev_hash"] = consumer.GENESIS
        (self.dir / "cards.jsonl").write_text(
            json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("非法 type", out)

    def test_missing_field_exit_1(self):
        rec = dict(RECORDS[0])
        rec["prev_hash"] = consumer.GENESIS
        del rec["spec_hash"]
        (self.dir / "cards.jsonl").write_text(
            json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("必填字段缺失", out)


if __name__ == "__main__":
    unittest.main()
