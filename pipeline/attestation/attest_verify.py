#!/usr/bin/env python3
"""attest_verify.py —— 签名证据包机械验证器（IR-0006 W4-R3 / #420 / AC-8f / INV-01）

零 LLM、零自报采信：对证据包逐项复算（部署可回溯的机械锚点）——
  1. 产物 digest 复算 == attestation.subject.digest.sha256
  2. SBOM 从产物重算 == bundle/sbom.json（逐文件比对）且 canonical digest
     == attestation.materials.sbom_sha256
  3. RS256 验签（openssl dgst -verify，公钥仓内公开 pipeline/attestation/keys/）
     ——签名对去 signature 字段的 canonical JSON
  4. git_commit 锚点在位（回溯链：产物→attestation→commit→evidence 判定记录）

用法：
  attest_verify.py --bundle <dir> --artifact <file> --pubkey <pem> \
      [--expect-commit <sha>] [--expect-card <owner/repo#n>] [--content-only]
  --content-only：跳过产物字节级 digest 复算，只做内容级 SBOM 重算+验签+锚点
  （回溯场景：产物由 git archive 重建，tar/gzip 字节受工具版本影响——文件
  内容 digest 才是稳定锚；字节级检查仍在位供首发验证用）。
退出码：0=验签绿 | 1=验证失败（digest 不符/签名坏/锚点缺失）| 2=基础设施错误
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from attest_pack import sha256_file, sbom_from_artifact, BUNDLE_TYPE  # noqa: E402


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def rs256_verify(pubkey: str, message: bytes, sig_b64: str) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf, \
            tempfile.NamedTemporaryFile(suffix=".sig", delete=False) as sf:
        kf.write(pubkey.encode())
        sf.write(base64.b64decode(sig_b64))
    try:
        p = subprocess.run(["openssl", "dgst", "-sha256", "-verify", kf.name, "-signature", sf.name],
                           input=message, capture_output=True)
    finally:
        os.unlink(kf.name)
        os.unlink(sf.name)
    return p.returncode == 0


def fail(code: int, msg: str) -> int:
    print(f"REJECT {msg}", file=sys.stderr)
    return code


def main() -> int:
    def opt(name: str) -> str:
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else ""

    bundle = opt("--bundle")
    artifact = opt("--artifact")
    pubkey_path = opt("--pubkey")
    expect_commit = opt("--expect-commit")
    expect_card = opt("--expect-card")
    if not all([bundle, artifact, pubkey_path]):
        print(__doc__)
        return 2
    try:
        att = load_json(os.path.join(bundle, "attestation.json"))
        sbom = load_json(os.path.join(bundle, "sbom.json"))
        with open(pubkey_path, encoding="utf-8") as f:
            pubkey = f.read()
    except (OSError, json.JSONDecodeError) as e:
        return fail(2, f"证据包/公钥不可读: {e}")
    if not os.path.isfile(artifact):
        return fail(2, f"产物不存在: {artifact}")

    # 1. 产物 digest（--content-only 跳过：回溯重建场景见 docstring）
    digest = sha256_file(artifact)
    if att.get("_type") != BUNDLE_TYPE:
        return fail(1, f"_type 非 {BUNDLE_TYPE}")
    if "--content-only" not in sys.argv and \
            att.get("subject", {}).get("digest", {}).get("sha256") != digest:
        return fail(1, f"产物 digest 不符（attestation={att.get('subject', {}).get('digest', {}).get('sha256', '?')[:12]} 实算={digest[:12]}）")

    # 2. SBOM 重算（逐文件）+ canonical digest 对 materials
    recomputed = sbom_from_artifact(artifact, sbom.get("source", {}).get("repo", ""),
                                    sbom.get("source", {}).get("commit", ""))
    got_files = sbom.get("files")
    if got_files != recomputed["files"]:
        return fail(1, "SBOM 文件清单不符（产物内容与清单漂移）")
    canon = json.dumps({k: v for k, v in sbom.items()}, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"))
    if hashlib.sha256(canon.encode("utf-8")).hexdigest() != att.get("materials", {}).get("sbom_sha256"):
        return fail(1, "SBOM canonical digest 与 attestation.materials 不符")

    # 3. RS256 验签（去 signature 字段重签消息）
    sig = att.get("signature", {})
    if sig.get("alg") != "RS256" or not sig.get("sig"):
        return fail(1, "签名结构缺失")
    message = json.dumps({k: v for k, v in att.items() if k != "signature"},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not rs256_verify(pubkey, message, sig["sig"]):
        return fail(1, "RS256 验签失败（签名坏或公钥不符）")

    # 4. 回溯锚点
    commit = att.get("materials", {}).get("git_commit")
    if not commit:
        return fail(1, "materials.git_commit 缺失（回溯链断）")
    if expect_commit and commit != expect_commit:
        return fail(1, f"commit 不符（期望={expect_commit[:12]} 实际={commit[:12]}）")
    card = att.get("predicate", {}).get("card")
    if not card:
        return fail(1, "predicate.card 缺失（evidence join key 断）")
    if expect_card and card != expect_card:
        return fail(1, f"card 不符（期望={expect_card} 实际={card}）")

    print(f"OK    证据包验证绿（artifact={digest[:12]} files={len(got_files)} commit={commit[:12]}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
