#!/usr/bin/env bash
# install-ocr.sh —— vendored OCR 钉版安装（W2-C4 .github#217 / ADR-0063 决策 1）
#
# 供应链纪律（宪法 §2 security-response / ADR-0063）：
#   1) 版本钉死：显式 v1.9.9，绝不 latest 浮动；
#   2) 哈希锚定：sha256sum -c 校验通过才安装（校验失败=fail-closed 退出非零）；
#   3) telemetry 显式禁用：OCR 默认 telemetry off，但配置层 env 只能开不能强制关
#      （源码 internal/telemetry/config.go：OCR_ENABLE_TELEMETRY=="1" 仅单向开启），
#      故本安装器额外落 ~/.opencodereview/config.json {"telemetry":{"enabled":false}}
#      显式钉死关闭；出口兜底由 harden-runner egress 白名单阻断（workflow 层，
#      ADR-0063「无法关闭的出口流量经审计/阻断」——双层防线）。
#   4) SBOM：pipeline/ocr/sbom/ocr-v1.9.9.cdx.json（CycloneDX 1.5 手写清单，零依赖），
#      tests/test_pins.py 校验 SBOM↔本封装钉锚一致（防静默漂移）。
set -euo pipefail

# 钉锚与 action.yml inputs 同值（composite env 注入；独立运行时可用环境变量覆盖）
OCR_VERSION="${OCR_VERSION:-1.9.9}"
OCR_SHA256="${OCR_SHA256:-52f993c615a6b456cb1c36fc135fec6b8da19cb88da7f305bd2726c3d72f1cf0}"

BIN="${OCR_INSTALL_DIR:-/usr/local/bin}/ocr"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 官方 release 资产（github.com 直链；egress 白名单已含 github.com/objects.githubusercontent.com）
curl -sSfL "https://github.com/alibaba/open-code-review/releases/download/v${OCR_VERSION}/opencodereview-linux-amd64" \
  -o "$TMP/ocr-bin"

# 双锚定校验：sha256 不匹配即失败（fail-closed，不给降级路径）
printf '%s  %s\n' "$OCR_SHA256" "$TMP/ocr-bin" | sha256sum -c -

install -m 0755 "$TMP/ocr-bin" "$BIN"

# telemetry 显式禁用（见文件头说明；--api 无关：本地写盘，零网络）
mkdir -p "$HOME/.opencodereview"
printf '{"telemetry":{"enabled":false}}\n' > "$HOME/.opencodereview/config.json"

"$BIN" version
echo "vendored-ocr 安装完成：$BIN v${OCR_VERSION} sha256=${OCR_SHA256:0:16}… telemetry=disabled(显式)"
