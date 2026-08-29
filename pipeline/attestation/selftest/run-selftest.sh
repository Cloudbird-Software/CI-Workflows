#!/usr/bin/env bash
# run-selftest.sh —— 签名证据包 v0 自测（IR-0006 W4-R3 / #420 / AC-8e/8f）
#
# 零网络、零真实仓依赖：openssl 一次性密钥对 + fixture 产物（tar.gz）。
# 正向：pack→verify 全链绿（digest 复算/SBOM 重算/RS256 验签/锚点）。
# 负向：产物篡改/SBOM 篡改/attestation 篡改/错公钥/缺 bundle——逐项必红
# （fail-closed：验证器漏检任何一类=测试红）。
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # pipeline/attestation
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  PASS $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $1"; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# ---- fixture：密钥对 + 产物 tar.gz（三文件，内容含区分度） ----
openssl genrsa -out "$TMP/sk.pem" 2048 2>/dev/null
openssl rsa -in "$TMP/sk.pem" -pubout -out "$TMP/pub.pem" 2>/dev/null
mkdir -p "$TMP/src/gov" "$TMP/src/docs"
echo "gate-engine-v1"   > "$TMP/src/gov/engine.sh"
echo "policy-v1"        > "$TMP/src/gov/policy.yaml"
echo "boundary-v1"      > "$TMP/src/docs/boundary.md"
tar -czf "$TMP/artifact.tar.gz" -C "$TMP" src

run_pack()   { python3 "$DIR/attest_pack.py"   "$@"; }
run_verify() { python3 "$DIR/attest_verify.py" "$@"; }

# T1 正向全链
run_pack --artifact "$TMP/artifact.tar.gz" --repo "Cloudbird-Software/.github" \
  --commit "abc123def4567890abc123def4567890abc123de" --card "Cloudbird-Software/.github#420" \
  --tenant cloudbird-internal --key "$TMP/sk.pem" --out "$TMP/bundle" >/dev/null 2>&1
[[ -s "$TMP/bundle/attestation.json" && -s "$TMP/bundle/sbom.json" ]] \
  && ok "T1 pack 产出双件（attestation+sbom）" || bad "T1 pack 产出缺失"
run_verify --bundle "$TMP/bundle" --artifact "$TMP/artifact.tar.gz" --pubkey "$TMP/pub.pem" \
  --expect-commit "abc123def4567890abc123def4567890abc123de" --expect-card "Cloudbird-Software/.github#420" >/dev/null 2>&1 \
  && ok "T1 verify 全链绿（digest/SBOM/验签/锚点）" || bad "T1 verify 红"

# T2 产物篡改（一字节）
cp "$TMP/artifact.tar.gz" "$TMP/artifact.bad.tar.gz"
printf 'x' >> "$TMP/artifact.bad.tar.gz"
run_verify --bundle "$TMP/bundle" --artifact "$TMP/artifact.bad.tar.gz" --pubkey "$TMP/pub.pem" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "T2 产物篡改 → 红（subject digest 不符）" || bad "T2 产物篡改漏检"

# T3 SBOM 篡改（清单行改 digest）
cp -r "$TMP/bundle" "$TMP/bundle-sbom"
python3 - "$TMP/bundle-sbom/sbom.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["files"][0]["sha256"] = "0" * 64
json.dump(d, open(p, "w"), ensure_ascii=False, indent=1)
PY
run_verify --bundle "$TMP/bundle-sbom" --artifact "$TMP/artifact.tar.gz" --pubkey "$TMP/pub.pem" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "T3 SBOM 篡改 → 红（清单与产物漂移检出）" || bad "T3 SBOM 篡改漏检"

# T4 attestation 篡改（subject digest 换——签名必坏）
cp -r "$TMP/bundle" "$TMP/bundle-att"
python3 - "$TMP/bundle-att/attestation.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["subject"]["digest"]["sha256"] = "f" * 64
json.dump(d, open(p, "w"), ensure_ascii=False, indent=1)
PY
run_verify --bundle "$TMP/bundle-att" --artifact "$TMP/artifact.tar.gz" --pubkey "$TMP/pub.pem" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "T4 attestation 篡改 → 红（digest 不符+验签必坏双锚）" || bad "T4 attestation 篡改漏检"

# T5 错公钥（第二把钥匙验签）
openssl genrsa -out "$TMP/sk2.pem" 2048 2>/dev/null
openssl rsa -in "$TMP/sk2.pem" -pubout -out "$TMP/pub2.pem" 2>/dev/null
run_verify --bundle "$TMP/bundle" --artifact "$TMP/artifact.tar.gz" --pubkey "$TMP/pub2.pem" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "T5 错公钥 → 红（RS256 验签拒）" || bad "T5 错公钥漏检"

# T6 缺件（bundle 目录空）→ exit 2 infra；--expect-commit 不符 → 红
mkdir -p "$TMP/bundle-empty"
run_verify --bundle "$TMP/bundle-empty" --artifact "$TMP/artifact.tar.gz" --pubkey "$TMP/pub.pem" >/dev/null 2>&1
[[ $? -eq 2 ]] && ok "T6 缺 bundle → exit 2（fail-closed infra）" || bad "T6 缺件退出码错（$?）"
run_verify --bundle "$TMP/bundle" --artifact "$TMP/artifact.tar.gz" --pubkey "$TMP/pub.pem" \
  --expect-commit "0000000000000000000000000000000000000000" >/dev/null 2>&1
[[ $? -eq 1 ]] && ok "T6b commit 锚不符 → 红（回溯链执法）" || bad "T6b commit 锚漏检"

# T7 回溯内容级验证（--content-only）：重打包 tar（字节变、内容同）→ 绿；
# 内容变一字节 → 红（内容锚不放过）
tar -cf "$TMP/artifact-repack.tar" -C "$TMP" src && gzip -n -9 -c "$TMP/artifact-repack.tar" > "$TMP/artifact-repack.tar.gz"
run_verify --bundle "$TMP/bundle" --artifact "$TMP/artifact-repack.tar.gz" --pubkey "$TMP/pub.pem" --content-only >/dev/null 2>&1 \
  && ok "T7 重打包（字节变内容同）+ --content-only → 绿（内容级锚）" || bad "T7 重打包误红"
mkdir -p "$TMP/src2" && cp -r "$TMP/src/." "$TMP/src2/" && echo "tampered" >> "$TMP/src2/gov/engine.sh"
tar -czf "$TMP/artifact2.tar.gz" -C "$TMP" src2
run_verify --bundle "$TMP/bundle" --artifact "$TMP/artifact2.tar.gz" --pubkey "$TMP/pub.pem" --content-only >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "T7b 内容篡改 → 红（--content-only 不放过内容漂移）" || bad "T7b 内容篡改漏检"

echo "attestation-selftest: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
