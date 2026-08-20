#!/usr/bin/env python3
"""parse-test-integrity-policy.py —— test-integrity policy 解析（ADR-0035 / .github #86，P2-1）。

输入：testing.yaml（.github governance/policy/testing.yaml 拉取到本地后的文件路径，
作为 argv[1]）。行为：
- 解析失败/YAML 不可读 → 红（fail-closed，ADR-0035：检测器读不到=红）；
- 无 test_integrity 节 → 检测器内置同值缺省（notice 后 exit 0）；
- 有节 → 把 patterns（test_file/assertion/suppression/non_source）与 TI-R4 severity
  写入 GITHUB_ENV（TI_TEST_FILE_RE / TI_ASSERT_RE / TI_SUPPRESS_RE / TI_META_PATH_RE /
  TI_R4_SEVERITY），覆盖 scripts/test-integrity.sh 的内置缺省。
"""
import os
import sys

try:
    import yaml
except ImportError:
    print("::error::TI-FC PyYAML 不可用——policy 不可解析（fail-closed）")
    sys.exit(1)

path = sys.argv[1] if len(sys.argv) > 1 else ""
if not path or not os.path.isfile(path):
    print("::error::TI-FC policy 文件不可读（fail-closed）")
    sys.exit(1)

try:
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
except Exception as e:  # noqa: BLE001 —— 任何解析失败一律红（fail-closed）
    print(f"::error::TI-FC policy YAML 解析失败：{e}（fail-closed）")
    sys.exit(1)

ti = doc.get("test_integrity")
if not isinstance(ti, dict) or not ti:
    print("::notice::policy 无 test_integrity 节——使用检测器内置缺省（与声明同值，ADR-0035）")
    sys.exit(0)


def emit(key: str, val) -> None:
    if not val:
        return
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:  # CI：写入 job 级 env
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"{key}={val}\n")
    else:  # 本地干跑：打印等价物（不写入任何文件）
        print(f"LOCAL-EMIT {key}={val}")


rules = {r.get("id"): r for r in (ti.get("rules") or []) if isinstance(r, dict)}
sev = (rules.get("TI-R4") or {}).get("severity")
if sev and str(sev) in ("red", "require_adr"):
    emit("TI_R4_SEVERITY", str(sev))
elif sev:
    print(f"::error::TI-FC TI-R4 severity 非法：{sev}（仅 red|require_adr）")
    sys.exit(1)

pats = ti.get("patterns") or {}
emit("TI_TEST_FILE_RE", pats.get("test_file"))
emit("TI_ASSERT_RE", pats.get("assertion"))
emit("TI_SUPPRESS_RE", pats.get("suppression"))
emit("TI_META_PATH_RE", pats.get("non_source"))
print("policy test_integrity 已载入（rules=%s）" % ",".join(sorted(rules)))
