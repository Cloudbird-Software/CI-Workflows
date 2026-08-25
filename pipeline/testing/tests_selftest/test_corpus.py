"""corpus：语料库 add/list/growth（AC-3）。"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.testing.fuzz import corpus, seedgen

SCHEMA = json.loads(
    Path(__file__).resolve().parent.joinpath("fixtures", "fuzz-schema.json").read_text(encoding="utf-8")
)


class TestCorpus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.seeds_dir = self.base / "seeds"
        seedgen.write_corpus(SCHEMA, self.seeds_dir)
        self.corpus_dir = self.base / "corpus"

    def test_add_and_dedup_skip(self):
        results = corpus.add_paths(self.corpus_dir, self.seeds_dir, generator="seedgen")
        self.assertGreater(results["add"], 20)
        self.assertEqual(results["skip_duplicate"], 0)
        manifest = corpus.load_manifest(self.corpus_dir)
        self.assertEqual(len(manifest["seeds"]), results["add"])
        # 幂等再入：全部 skip_duplicate，清单不增长
        again = corpus.add_paths(self.corpus_dir, self.seeds_dir, generator="seedgen")
        self.assertEqual(again["add"], 0)
        self.assertEqual(again["skip_duplicate"], results["add"])
        self.assertEqual(len(corpus.load_manifest(self.corpus_dir)["seeds"]), results["add"])

    def test_growth_curve_cumulative(self):
        corpus.add_paths(self.corpus_dir, self.seeds_dir, generator="seedgen")
        rows = corpus.growth_curve(self.corpus_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cumulative_seeds"], len(corpus.load_manifest(self.corpus_dir)["seeds"]))
        self.assertGreater(rows[0]["added"], 0)

    def test_ledger_is_append_only_jsonl(self):
        corpus.add_paths(self.corpus_dir, self.seeds_dir, generator="seedgen")
        ledger_path = self.corpus_dir / "ledger.jsonl"
        self.assertTrue(ledger_path.exists())
        events = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(events), len(corpus.load_manifest(self.corpus_dir)["seeds"]))
        self.assertTrue(all(e["event"] in ("add", "skip_duplicate") for e in events))


if __name__ == "__main__":
    unittest.main()
