#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉锚一致性自测（W2-C4 AC-1 供应链）：SBOM ↔ action.yml ↔ install-ocr.sh 三处
sha256/版本/release-commit 必须一致——任一处静默漂移即红（零网络）。"""
import json
import os
import re
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
OCR = os.path.normpath(os.path.join(HERE, ".."))
SBOM = os.path.join(OCR, "sbom", "ocr-v1.9.9.cdx.json")
ACTION = os.path.join(OCR, "action.yml")
INSTALL = os.path.join(OCR, "install-ocr.sh")

SHA_RE = re.compile(r"\b[0-9a-f]{64}\b")
COMMIT_RE = re.compile(r"\b[0-9a-f]{40}\b")


class TestPins(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SBOM, encoding="utf-8") as f:
            cls.sbom = json.load(f)
        with open(ACTION, encoding="utf-8") as f:
            cls.action = yaml.safe_load(f)
        with open(INSTALL, encoding="utf-8") as f:
            cls.install = f.read()
        cls.comp = cls.sbom["components"][0]

    def test_sha256_pinned_everywhere_and_equal(self):
        """版本+hash 双锚定：三处 sha256 一致（AC-1）。"""
        sbom_sha = self.comp["hashes"][0]["content"]
        action_sha = self.action["inputs"]["sha256"]["default"]
        install_sha = next(iter(SHA_RE.findall(self.install)))
        self.assertEqual(sbom_sha, action_sha)
        self.assertEqual(sbom_sha, install_sha)
        self.assertEqual(self.comp["version"], "1.9.9")
        self.assertEqual(self.action["inputs"]["version"]["default"], "1.9.9")

    def test_release_commit_reference(self):
        """release 源 commit SHA 溯源锚点（action.yml 与 SBOM 一致）。"""
        action_commit = COMMIT_RE.search(
            self.action["inputs"]["release-commit"]["default"]).group(0)
        sbom_vcs = [r["url"] for r in self.comp["externalReferences"] if r["type"] == "vcs"][0]
        self.assertIn(action_commit, sbom_vcs)

    def test_telemetry_disabled_declared(self):
        """telemetry 关闭声明在案（ADR-0063 决策 1：显式禁用 + SBOM 声明）。"""
        props = {p["name"]: p["value"] for p in self.comp["properties"]}
        self.assertTrue(props["cloudbird:telemetry"].startswith("disabled-explicitly"))
        self.assertIn('"telemetry":{"enabled":false}', self.install)


if __name__ == "__main__":
    unittest.main()
