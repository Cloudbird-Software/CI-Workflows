#!/usr/bin/env python3
"""attest_pack.py —— 签名证据包 v0 生成器（IR-0006 W4-R3 / #420 / SC-4 / ADR-0103）

部署产物 attestation + SBOM 生成（承 SC-4 provenance 先例；SC-4 声明的
actions/attest-build-provenance 面向 release asset，本 v0 面向治理仓部署产物
（git ref tarball）——同一语义的仓库内等价实现，无外部 SaaS 依赖）。

证据包（--out <dir>）：
  sbom.json        文件清单 SBOM（v0：产物内逐文件 sha256/size——骨架期不引
                   syft 外部依赖，T-07 syft 面向 release 场景保持声明层）
  attestation.json in-toto 风格简化 statement + RS256 签名：
                   subject=产物 digest；materials=SBOM digest+git commit；
                   predicate=card/tenant/tool；signature 对去签名字段的
                   canonical JSON 做 RS256（openssl dgst，同 gh-app-token 先例）

签名密钥：--key <PEM 文件> 或 env ATTEST_SIGNING_KEY（PEM 字面量）。
私钥只存 secret（INV-04）；公钥仓内公开（pipeline/attestation/keys/）——
验证锚点机械（INV-01/AC-8f）：attest_verify.py 复算 digest+验签，零 LLM。

绑定 evidence/ 判定记录（AC-8e）：调用侧把 attestation digest 作
inputs_digest 经 archive 仓 write_evidence.py 追加（payload 内联
bundle 摘要三件套——4KB 内，INV-06）。

用法：
  attest_pack.py --artifact <file> --repo <owner/name> --commit <sha> \
      --card <owner/repo#n> --tenant <t> [--key <pem>] --out <dir>
退出码：0=成功 | 1=参数/输入非法 | 2=基础设施错误（签名失败等）
"""
import base64
import datetime
import hashlib
import json
import os
import subprocess
import sys
import tarfile

BUNDLE_TYPE = "attest-pack/v0"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sbom_from_artifact(artifact: str, repo: str, commit: str) -> dict:
    """文件清单 SBOM：tar 内逐成员内容 sha256（与 gzip 字节级非确定性解耦——
    成员内容 digest 才是稳定锚，tar.gz 整体 digest 只作 subject）。"""
    files = []
    with tarfile.open(artifact, "r:*") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            f = tf.extractfile(m)
            files.append({
                "path": m.name,
                "sha256": hashlib.sha256(f.read()).hexdigest(),
                "size": m.size,
            })
    files.sort(key=lambda x: x["path"])
    return {
        "sbom_version": "0",
        "format": "file-manifest",
        "artifact": {
            "name": os.path.basename(artifact),
            "sha256": sha256_file(artifact),
            "size": os.path.getsize(artifact),
        },
        "source": {"repo": repo, "commit": commit},
        "files": files,
        "generated_at": now_utc(),
    }


def load_key(args_key: str) -> str:
    pem = os.environ.get("ATTEST_SIGNING_KEY", "")
    if args_key:
        with open(args_key, encoding="utf-8") as f:
            pem = f.read()
    if not pem.strip():
        print("FATAL 签名私钥缺失（--key 或 ATTEST_SIGNING_KEY；INV-04：私钥只存 secret）",
              file=sys.stderr)
        sys.exit(2)
    return pem


def rs256_sign(pem: str, message: bytes) -> str:
    """openssl 子进程签名（同 gh-app-token.sh 先例——不引 python crypto 依赖）。"""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as tf:
        tf.write(pem.encode())
        key_path = tf.name
    try:
        p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_path],
                           input=message, capture_output=True)
    finally:
        os.unlink(key_path)
    if p.returncode != 0:
        print(f"FATAL 签名失败: {p.stderr.decode()[:200]}", file=sys.stderr)
        sys.exit(2)
    return base64.b64encode(p.stdout).decode()


def main() -> int:
    def opt(name: str) -> str:
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else ""

    artifact = opt("--artifact")
    repo = opt("--repo")
    commit = opt("--commit")
    card = opt("--card")
    tenant = opt("--tenant")
    out = opt("--out")
    if not all([artifact, repo, commit, card, tenant, out]):
        print(__doc__)
        return 1
    if not os.path.isfile(artifact):
        print(f"FATAL 产物不存在: {artifact}", file=sys.stderr)
        return 2

    pem = load_key(opt("--key"))
    sbom = sbom_from_artifact(artifact, repo, commit)
    sbom_digest = hashlib.sha256(
        json.dumps(sbom, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")).hexdigest()

    statement = {
        "_type": BUNDLE_TYPE,
        "subject": {"name": sbom["artifact"]["name"], "digest": {"sha256": sbom["artifact"]["sha256"]}},
        "materials": {"sbom_sha256": sbom_digest, "repo": repo, "git_commit": commit},
        "predicate": {"card": card, "tenant": tenant, "tool": "attest_pack.py", "generated_at": now_utc()},
    }
    message = json.dumps(statement, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    attestation = dict(statement)
    attestation["signature"] = {"alg": "RS256", "sig": rs256_sign(pem, message)}

    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "sbom.json"), "w", encoding="utf-8") as f:
        json.dump(sbom, f, ensure_ascii=False, indent=1)
        f.write("\n")
    with open(os.path.join(out, "attestation.json"), "w", encoding="utf-8") as f:
        json.dump(attestation, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"OK    证据包落盘 {out}/（artifact={sbom['artifact']['sha256'][:12]} "
          f"sbom={sbom_digest[:12]} files={len(sbom['files'])}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
