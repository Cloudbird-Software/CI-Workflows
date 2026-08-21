#!/usr/bin/env bash
# scan-direct-sdk.sh —— 直连 SDK 静态扫描（W2-C3 .github#216，ADR-0062 决策 1，INV-06）
#
# 扫仓内代码无绕过计量 wrapper 的直连 LLM 调用（裸 openai/anthropic SDK import、
# client 实例化、curl/requests 直打 provider 端点等——模式表 scan-patterns.yaml
# 可配置、版本化）。命中即 exit 1（fail-closed）；模式表不可解析 = exit 2
# （检测器自身故障不算通过）。默认扫本仓全部；显式传路径扫指定子树（自测用）。
#
# 用法:
#   bash pipeline/metering/scan-direct-sdk.sh [--patterns <yaml>] [--root <dir>] [path...]
# 退出码: 0=无直连 | 1=命中直连（逐条 file:line 列出） | 2=配置/环境错误
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${METERING_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
fi
ROOT="$(git -C "$DIR" rev-parse --show-toplevel 2>/dev/null || echo "$DIR/../../..")"
PATTERNS="$DIR/scan-patterns.yaml"
PATHS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --patterns) PATTERNS="${2:?}"; shift 2 ;;
    --root)     ROOT="${2:?}"; shift 2 ;;
    *)          PATHS+=("$1"); shift ;;
  esac
done
[[ -f "$PATTERNS" ]] || { echo "FATAL: 模式表不存在：$PATTERNS" >&2; exit 2; }
[[ ${#PATHS[@]} -gt 0 ]] || PATHS=("$ROOT")

# 扫描主体在 python（regex 语义精确、glob/豁免/行内放行单点实现）；
# bash 入口只管参数与退出码。yaml 解析失败/regex 非法 → exit 2（fail-closed）。
"$PY" - "$PATTERNS" "${PATHS[@]}" <<'PYEOF'
import fnmatch, os, re, sys
try:
    import yaml
except ImportError:
    print("FATAL: PyYAML 不可用（runner 镜像预装；本地 pip install pyyaml）", file=sys.stderr)
    sys.exit(2)

patterns_path, paths = sys.argv[1], sys.argv[2:]
try:
    cfg = yaml.safe_load(open(patterns_path, encoding="utf-8"))
    pats = cfg["patterns"]
    exempt = [re.compile(p) for p in cfg.get("exempt_paths", [])]
    globs = tuple(cfg.get("scan_globs", ["*.py", "*.sh", "*.js", "*.ts"]))
    compiled = [(p["id"], p["description"], re.compile(p["regex"])) for p in pats]
except Exception as e:  # noqa: BLE001 —— 模式表任何缺陷=检测器故障，fail-closed
    print(f"FATAL: 模式表解析失败（{patterns_path}）: {e}", file=sys.stderr)
    sys.exit(2)

hits, allows = 0, 0
for root in paths:
    base = root if os.path.isdir(root) else (os.path.dirname(root) or ".")
    if os.path.isfile(root):
        walk = [(os.path.dirname(root) or ".", [], [os.path.basename(root)])]
    else:
        walk = os.walk(root)
    for dirpath, dirnames, filenames in walk:
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "node_modules")]
        for fn in filenames:
            if not any(fnmatch.fnmatch(fn, g) for g in globs):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, base).replace("\\", "/")
            if any(e.search(rel) for e in exempt):
                continue
            try:
                text = open(full, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                for pid, desc, rx in compiled:
                    if rx.search(line):
                        if f"metering-allow: {pid}" in line:
                            allows += 1  # 行内豁免：留痕放行（审计面可见，非静默）
                            print(f"ALLOW {rel}:{lineno}: [{pid}]（行内豁免标记）")
                            break
                        print(f"::error file={rel},line={lineno}:: [{pid}] {desc}")
                        print(f"   {rel}:{lineno}: {line.strip()[:160]}", file=sys.stderr)
                        hits += 1
if hits:
    print(f"FAIL 直连 SDK 命中 {hits} 处（INV-06：一切 LLM 调用须经 pipeline/metering/metering-wrapper.sh；"
          f"豁免/回流走 scan-patterns.yaml PR——ADR-0062）", file=sys.stderr)
    sys.exit(1)
print(f"OK 无绕过 wrapper 的直连 SDK 调用（模式 {len(compiled)} 条，行内豁免 {allows} 处留痕）")
PYEOF
