#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_holdout_registry.py —— holdout_registry.py 单元测试（W2-C4 .github#276 / AC-3 / AC-17）

覆盖：
  - verify_entry_hash（sealed_sha256 校验 + 篡改检测 + 文件 sha256）
  - check_pr_references（PR 引用一致性：未知 id / hash 不一致）
  - check_register_identity（验证者 APP 未创建降级 / 显式 id 允许 / 非验证者拒绝）
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import holdout_registry as hr


def _make_entry(eid: str, files: list[tuple[str, bytes]]) -> dict:
    """构造合法 holdout 条目（sealed_sha256 用 ADR-0056 canonical 公式）。"""
    file_objs = []
    for name, content in files:
        file_objs.append({"name": name, "sha256": hashlib.sha256(content).hexdigest(),
                          "content_b64": base64.b64encode(content).decode("ascii")})
    payload = {"kind": "sealed-test-set", "schema": "holdout-unseal/1",
               "runner": "pytest", "files": file_objs}
    sealed = hashlib.sha256(hr.canon(payload).encode("utf-8")).hexdigest()
    return {"id": eid, "type": "e2e-scenario", "payload": payload,
            "sealed_sha256": sealed, "ir_ref": "#263",
            "created_at": "2026-08-23T00:00:00Z", "sealed_by": "owner:test"}


class TestVerifyEntryHash(unittest.TestCase):
    def test_valid_entry(self):
        e = _make_entry("HO-0001", [("test_x.py", b"def test_x():\n    assert True\n")])
        ok, reason = hr.verify_entry_hash(e)
        self.assertTrue(ok, reason)

    def test_tampered_payload(self):
        e = _make_entry("HO-0001", [("test_x.py", b"def test_x():\n    assert True\n")])
        e["payload"]["files"][0]["name"] = "tampered.py"
        ok, reason = hr.verify_entry_hash(e)
        self.assertFalse(ok)
        self.assertIn("sealed_sha256 不符", reason)

    def test_bad_sealed_field(self):
        e = _make_entry("HO-0001", [("test_x.py", b"x")])
        e["sealed_sha256"] = "a" * 64
        ok, _ = hr.verify_entry_hash(e)
        self.assertFalse(ok)

    def test_file_sha_mismatch(self):
        # 改变 content_b64 为不同内容，但保留文件 sha256 字段为旧值；
        # 重算 sealed_sha256 使条目级校验通过，从而隔离出「文件级 sha 不符」路径。
        e = _make_entry("HO-0001", [("test_x.py", b"def test_x():\n    assert True\n")])
        new_content = b"def test_x():\n    assert False  # tampered\n"
        e["payload"]["files"][0]["content_b64"] = base64.b64encode(new_content).decode("ascii")
        # sha256 字段保留旧值（与 new_content 不符）
        e["sealed_sha256"] = hr.sha_hex(e["payload"])
        ok, reason = hr.verify_entry_hash(e)
        self.assertFalse(ok)
        self.assertIn("文件 sha256 不符", reason)


class TestCheckPrReferences(unittest.TestCase):
    def setUp(self):
        self.index = {"HO-0001": "a" * 64, "HO-0002": "c" * 64}

    def test_unknown_id(self):
        body = "Refs holdout HO-0009"
        r = hr.check_pr_references(body, self.index)
        self.assertFalse(r["ok"])
        self.assertIn("HO-0009", r["violations"][0])

    def test_valid_reference(self):
        body = "Refs holdout HO-0001 sha256: " + "a" * 64
        r = hr.check_pr_references(body, self.index)
        self.assertTrue(r["ok"])

    def test_hash_mismatch(self):
        body = "Refs holdout HO-0001 sha256: " + "b" * 64
        r = hr.check_pr_references(body, self.index)
        self.assertFalse(r["ok"])


class TestRegisterIdentity(unittest.TestCase):
    def test_degraded_when_no_verifier(self):
        # 无 VERIFIER_APP_ID → 降级允许（degraded）
        r = hr.check_register_identity("cloudbrid-agent[bot]")
        self.assertTrue(r["allowed"])
        self.assertTrue(r.get("degraded"))

    def test_verifier_allowed(self):
        os.environ["VERIFIER_APP_ID"] = "999999"
        try:
            r = hr.check_register_identity("verifier-app:999999")
            self.assertTrue(r["allowed"])
        finally:
            del os.environ["VERIFIER_APP_ID"]

    def test_non_verifier_rejected(self):
        os.environ["VERIFIER_APP_ID"] = "999999"
        try:
            r = hr.check_register_identity("cloudbrid-agent[bot]")
            self.assertFalse(r["allowed"])
            self.assertIn("403", r["reason"])
        finally:
            del os.environ["VERIFIER_APP_ID"]


class TestLoadEntries(unittest.TestCase):
    def test_load_valid(self):
        with tempfile.TemporaryDirectory() as td:
            entries_dir = os.path.join(td, "entries")
            os.makedirs(entries_dir)
            e = _make_entry("HO-0001", [("test_x.py", b"def test_x():\n    pass\n")])
            with open(os.path.join(entries_dir, "HO-0001.json"), "w", newline="\n") as f:
                json.dump(e, f)
            loaded = hr.load_entries(td)
            self.assertIn("HO-0001", loaded)
            idx = hr.build_hash_index(loaded)
            self.assertEqual(idx["HO-0001"], e["sealed_sha256"])


if __name__ == "__main__":
    unittest.main()
