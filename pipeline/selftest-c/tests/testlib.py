"""测试公共库：路径注入、固定 SHA、条目工厂、注册表文本工厂。"""
import sys
from pathlib import Path

BUILD_C = Path(__file__).resolve().parents[1]
if str(BUILD_C) not in sys.path:
    sys.path.insert(0, str(BUILD_C))

from oracle.miniyaml import dump_yaml, load_yaml  # noqa: E402

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
BASE_SHA = "f" * 40
OTHER_BASE = "e" * 40
T1 = "2026-01-01T00:00:00Z"
T2 = "2026-02-01T00:00:00Z"
T3 = "2026-03-01T00:00:00Z"

RAW_REGISTRY_YAML = """entries:
  - name: parser-core
    host_repo: cloudbird/hero
    target_surface: parse
    frozen_sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    cluster: c1
    decorrelation_reason: 独立实现+不同语料
    hard_zone:
      - "case/hard/*"
    soft_zone:
      - "case/soft/*"
    status: frozen
    generations:
      - sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        frozen_at: "2026-01-01T00:00:00Z"
        note: initial freeze
  - name: parser-core
    host_repo: cloudbird/hero
    target_surface: parse
    frozen_sha: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    cluster: c1
    decorrelation_reason: 独立实现+不同语料
    hard_zone:
      - "case/hard/*"
    soft_zone:
      - "case/soft/*"
    status: candidate
    generations: []
"""


def make_entry(name="parser-core", sha=None, status="frozen",
               hard=("case/hard/*",), soft=("case/soft/*",)):
    sha = sha or SHA_A
    if status == "candidate":
        gens = []
    else:
        gens = [{"sha": sha, "frozen_at": T1, "note": "initial freeze"}]
    return {
        "name": name,
        "host_repo": "cloudbird/hero",
        "target_surface": "parse",
        "frozen_sha": sha,
        "cluster": "c1",
        "decorrelation_reason": "独立实现，训练语料与 champion 无共享",
        "hard_zone": list(hard),
        "soft_zone": list(soft),
        "status": status,
        "generations": gens,
    }


def registry_text(entries):
    return dump_yaml({"entries": entries})


def load_registry_file(path):
    return load_yaml(Path(path).read_text(encoding="utf-8"))
