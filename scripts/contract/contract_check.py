#!/usr/bin/env python3
"""contract_check.py —— 契约兼容性检测门引擎（P2-4，.github#89，ADR-0038）

三种检测面（按 policy 声明的 kind 分派）：
  - openapi    → oasdiff breaking --fail-on WARN（退出码 0=兼容 / 1=breaking / 其他=工具错误→红）
  - jsonschema → 内置结构化 breaking 分类器（type 改变 / required 收紧 / 属性删除 /
                 enum 收窄 / additionalProperties:false 新增 / 数值边界收紧）
  - proto      → 不接受（组织无 proto；声明即报错，fail-closed。首个 proto 仓落地时
                 修订 ADR-0038 实装 buf breaking）

DB migration：alembic（op.* 调用 + op.execute 内嵌 SQL）与裸 SQL 双前端同一分类核心。
destructive 判定须给出 文件:行号；destructive 变更须 (a) PR 引用真实存在的 ADR 且
(b) 回滚脚本含逆操作（alembic downgrade()，逆映射见 INVERSE_KINDS；裸 SQL 无
downgrade 节——回滚脚本要求仅适用 alembic，ADR-0038 决策 4）才绿。

fail-closed 原则：policy 拉取失败（非 404）→ 红；声明路径找不到文件 → 红（T6 失明
防护）；分类器无法判定的 DDL → 红；oasdiff 工具错误 → 红；JSON 解析失败 → 红。

自测：--selftest 以临时 git 仓跑全套卡内测试项（T1-T7），任一断言不符 → 非零退出。
仅依赖 Python 标准库 + PyYAML。
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:  # pragma: no cover
    print("::error::缺少 PyYAML（workflow 侧已 pip install pyyaml==6.0.3）", file=sys.stderr)
    raise

POLICY_REPO_API = "repos/Cloudbird-Software/.github/contents/governance/policy/contracts.yaml"
ADR_DIR_API = "repos/Cloudbird-Software/agent-registry/contents/decisions"
ADR_RE = re.compile(r"\bADR-[0-9]{4}\b")
HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------- finding -----

class Finding:
    def __init__(self, kind: str, reason: str, file: str = "", line: int = 0):
        self.kind, self.reason, self.file, self.line = kind, reason, file, line

    def __repr__(self):
        where = f"{self.file}:{self.line}" if self.file else "(policy)"
        return f"[{self.kind}] {where} {self.reason}"

def err(f: Finding):
    if f.file and f.line:
        print(f"::error file={f.file},line={f.line}::{f.kind}: {f.reason}")
    elif f.file:
        print(f"::error file={f.file}::{f.kind}: {f.reason}")
    else:
        print(f"::error::{f.kind}: {f.reason}")

# ------------------------------------------------------------- policy ------

ALLOWED_KINDS = {"openapi", "jsonschema"}          # proto 显式拒绝（ADR-0038 决策 2）
ALLOWED_TOOLS = {"alembic", "sql"}

def _bundled_text() -> str:
    with open(os.path.join(HERE, "policy-bundled.yaml"), encoding="utf-8") as fh:
        return fh.read()

def load_policy_text(mode: str, path: str | None):
    """→ (text, source)。org 模式 404 → bundled 回退（bootstrap，ADR-0038 决策 6）；
    其他错误抛异常（fail-closed）。"""
    if mode == "path":
        with open(path, encoding="utf-8") as fh:
            return fh.read(), f"file:{path}"
    if mode == "bundled":
        return _bundled_text(), "bundled"
    tok = os.environ.get("GITHUB_TOKEN", "")
    env = dict(os.environ, **{"GH_TOKEN": tok}) if tok else dict(os.environ)
    try:
        out = subprocess.run(["gh", "api", POLICY_REPO_API], capture_output=True,
                             text=True, env=env, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise RuntimeError(f"org policy 拉取失败（fail-closed）: {e}") from e
    if out.returncode == 0:
        try:
            return base64.b64decode(json.loads(out.stdout)["content"]).decode(), "org(.github main)"
        except Exception as e:
            raise RuntimeError(f"org policy 响应解析失败（fail-closed）: {e}") from e
    if "Not Found" in (out.stderr or ""):
        print("::warning::org policy governance/policy/contracts.yaml 未合入（404）——"
              "使用引擎内置 bootstrap 快照（以 .github 仓 org policy 为准，ADR-0038 决策 6）")
        return _bundled_text(), "bundled(bootstrap)"
    raise RuntimeError(f"org policy 拉取失败 rc={out.returncode}（fail-closed）: "
                       f"{(out.stderr or '')[:200]}")

def validate_policy(pol: dict) -> list[Finding]:
    bad: list[Finding] = []
    repos = pol.get("repos")
    if not isinstance(repos, dict):
        return [Finding("POLICY_INVALID", "policy 缺少 repos 映射")]
    for repo, entry in repos.items():
        if not isinstance(entry, dict):
            bad.append(Finding("POLICY_INVALID", f"repos.{repo} 不是映射"))
            continue
        for c in entry.get("contracts") or []:
            kind = c.get("kind")
            if kind == "proto":
                bad.append(Finding("PROTO_UNSUPPORTED",
                    f"repos.{repo}: kind=proto 未实装（组织无 proto——ADR-0038 决策 2；"
                    "首个 proto 仓落地时修订 ADR 实装 buf breaking）"))
            elif kind not in ALLOWED_KINDS:
                bad.append(Finding("POLICY_INVALID", f"repos.{repo}.contracts kind={kind!r} 未知"))
            if not c.get("path"):
                bad.append(Finding("POLICY_INVALID", f"repos.{repo}.contracts 缺 path glob"))
        mig = entry.get("migrations")
        if mig:
            if mig.get("tool") not in ALLOWED_TOOLS:
                bad.append(Finding("POLICY_INVALID", f"repos.{repo}.migrations.tool 未知"))
            if not mig.get("dir"):
                bad.append(Finding("POLICY_INVALID", f"repos.{repo}.migrations 缺 dir"))
    return bad

# --------------------------------------------------------- git plumbing ----

def git(*args, cwd, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.strip()[:300]}")
    return r.stdout

def ls_files(root) -> list[str]:
    return [l for l in git("ls-files", cwd=root).splitlines() if l.strip()]

def glob_match(path: str, pattern: str) -> bool:
    # fnmatch 的 * 跨 '/'，故 specs/** 同时命中直接子文件与深层文件
    return fnmatch.fnmatch(path, pattern.rstrip("/"))

def decl_matches(path: str, decls: list[dict]) -> list[dict]:
    return [d for d in decls if glob_match(path, d["path"])]

def show(root, sha, path: str) -> str | None:
    r = subprocess.run(["git", "show", f"{sha}:{path}"], capture_output=True, text=True, cwd=root)
    return r.stdout if r.returncode == 0 else None

def diff_name_status(root, base, head) -> list[tuple[str, str, str | None]]:
    """[(status, path, previous_path)]"""
    out = git("diff", "--name-status", "--find-renames", base, head, cwd=root)
    res = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) == 2:
            res.append((parts[0], parts[1], None))
        else:                                       # R100 old new / 其余带分值状态
            res.append((parts[0], parts[-1], parts[-2]))
    return res

# --------------------------------------------------------- SQL DDL 分类器 ---

NEUTRAL_SQL = re.compile(
    r"^\s*(insert|update|delete|select|grant|revoke|set|comment|analyze|vacuum|lock|"
    r"begin|commit|rollback|with|do|call|explain)\b", re.I)
DD = r"[A-Za-z0-9_\"'`\[\]]+"

def split_statements(text: str) -> list[tuple[str, int, int]]:
    """按 ';' 切语句（感知 -- 与 /* */ 注释、引号、$$ 美元引用），返回 (stmt, 起行, 末行)，
    行号 1 基。"""
    stmts, i, n, start, line_start, line = [], 0, len(text), 0, 0, 1
    in_s = in_d = in_lc = in_bs = in_dollar = False
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            in_lc = False
            i += 1
            continue
        if in_lc:
            i += 1
            continue
        if in_bs:
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                in_bs = False
                i += 2
            else:
                i += 1
            continue
        if in_dollar:
            if text.startswith("$$", i):
                in_dollar = False
                i += 2
            else:
                i += 1
            continue
        if in_s:
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_s = False
            i += 1
            continue
        if in_d:
            if c == '"':
                in_d = False
            i += 1
            continue
        if c == "-" and i + 1 < n and text[i + 1] == "-":
            in_lc = True
            i += 2
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            in_bs = True
            i += 2
            continue
        if c == "'":
            in_s = True
            i += 1
            continue
        if c == '"':
            in_d = True
            i += 1
            continue
        if c == "$" and text.startswith("$$", i):
            in_dollar = True
            i += 2
            continue
        if c == ";":
            stmts.append((text[start:i], line_start + 1, line))
            start, line_start = i + 1, line
            i += 1
            continue
        i += 1
    if text[start:].strip():
        stmts.append((text[start:], line_start + 1, line))
    return stmts

def classify_sql_statement(s: str) -> tuple[str, str]:
    """单条 SQL → (kind, label)。label ∈ destructive|additive|neutral。"""
    t = re.sub(r"\s+", " ", s).strip()
    low = t.lower()
    if not low or NEUTRAL_SQL.match(low):
        return ("", "neutral")
    # ---- destructive（ADR-0038 决策 3 清单）----
    if re.search(r"\bdrop\s+table\b", low):
        return ("DROP_TABLE", "destructive")
    if re.search(r"\bdrop\s+(index|view|schema|database|function|procedure|trigger|type|domain)\b", low):
        return ("DROP_OBJECT", "destructive")
    if re.search(r"\bdrop\s+constraint\b|\bdrop\s+(primary|unique|foreign)\b", low):
        return ("DROP_CONSTRAINT", "destructive")
    if re.search(rf"\balter\s+table\s+(only\s+)?{DD}\s+drop\s+(column\s+)?{DD}", low) \
            or re.search(rf"^\s*alter\s+table\s+(only\s+)?{DD}\s+drop\s+(column\s+)?{DD}", low):
        return ("DROP_COLUMN", "destructive")
    if re.search(r"\btruncate\b", low):
        return ("TRUNCATE", "destructive")
    if re.search(r"\brename\s+to\b|\brename\s+(table|column)\b", low) \
            or re.search(rf"\balter\s+table\s+{DD}\s+rename\b", low):
        return ("RENAME", "destructive")
    if re.search(rf"\balter\s+(column\s+)?{DD}\s+(type|set\s+data\s+type)\b", low) \
            or re.search(rf"\balter\s+table\s+{DD}\s+alter\s+{DD}\s+type\b", low) \
            or re.search(rf"\bmodify\s+(column\s+)?{DD}\s+[a-z]", low) \
            or re.search(rf"\bchange\s+(column\s+)?{DD}\s+{DD}\s+[a-z]", low):
        return ("ALTER_TYPE", "destructive")
    if re.search(r"\bset\s+not\s+null\b", low):
        return ("SET_NOT_NULL", "destructive")
    if re.search(r"\bdrop\s+default\b", low):
        return ("DROP_DEFAULT", "destructive")
    add_m = re.search(rf"\badd\s+(column\s+)?{DD}", low)
    if add_m and re.search(rf"\badd\s+(column\s+)?{DD}.*\bnot\s+null\b", low):
        if not re.search(r"\bdefault\b", low):
            return ("ADD_NOTNULL_NO_DEFAULT", "destructive")
        return ("ADD_COLUMN", "additive")          # NOT NULL + DEFAULT（PG11+ 快加列）
    if re.search(r"\badd\s+constraint\b", low):
        if re.search(r"\bnot\s+valid\b", low):
            return ("ADD_CONSTRAINT_NOT_VALID", "additive")
        return ("ADD_CONSTRAINT", "destructive")   # 立即校验存量数据，从严
    # ---- additive ----
    if re.search(r"\bcreate\s+(or\s+replace\s+)?(table|index|view|extension|function|"
                 r"procedure|trigger|type|materialized)\b", low):
        return ("CREATE_OBJECT", "additive")
    if re.search(rf"\balter\s+type\s+{DD}\s+add\s+value\b", low):
        return ("ENUM_ADD_VALUE", "additive")
    if add_m:
        return ("ADD_COLUMN", "additive")
    # DDL 动词在场但无规则命中 → fail-closed
    if re.match(r"\s*(alter|drop|create|truncate|rename)\b", low):
        return ("UNCLASSIFIED_DDL", "destructive")
    return ("", "neutral")

def classify_sql_text(text: str) -> list[tuple[str, str, int]]:
    out = []
    for s, l0, _l1 in split_statements(text):
        k, lab = classify_sql_statement(s)
        if k and lab != "neutral":
            out.append((k, lab, l0))
    return out

# -------------------------------------------------------- alembic 分类器 ----

OP_CALL = re.compile(r"\bop\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")

def _extract_call(text: str, start: int) -> tuple[str, int]:
    """从左括号做配平（感知引号/三引号），返回 (调用文本, 结束偏移)。"""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c in "'\"":
            q = c
            triple = text.startswith(q * 3, i)
            i += 3 if triple else 1
            while i < n:
                if triple and text.startswith(q * 3, i):
                    i += 3
                    break
                if not triple and text[i] == q:
                    if i + 1 < n and text[i + 1] == q:
                        i += 2
                        continue
                    i += 1
                    break
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        i += 1
    return text[start:], n

def _str_literals(call: str) -> str:
    parts, i, n = [], 0, len(call)
    while i < n:
        if call[i] in "'\"":
            q = call[i]
            triple = call.startswith(q * 3, i)
            buf, i = [], i + (3 if triple else 1)
            while i < n:
                if triple and call.startswith(q * 3, i):
                    i += 3
                    break
                if not triple and call[i] == q:
                    if i + 1 < n and call[i + 1] == q:
                        buf.append(q)
                        i += 2
                        continue
                    i += 1
                    break
                if call[i] == "\\" and i + 1 < n:
                    buf.append(call[i + 1])
                    i += 2
                    continue
                buf.append(call[i])
                i += 1
            parts.append("".join(buf))
        else:
            i += 1
    return "\n".join(p for p in parts if p)

def _column_args(call: str) -> str | None:
    m = re.search(r"\bColumn\s*\(", call)
    if not m:
        return None
    inner, _ = _extract_call(call, m.end() - 1)
    return inner

def _alembic_section(text: str, name: str) -> str:
    m = re.search(rf"\bdef\s+{name}\s*\(", text)
    if not m:
        return ""
    rest = text[m.start():]
    nxt = re.search(r"\n(def\s+\w+|revision\s*[:=])", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest

def classify_alembic_text(text: str, section: str | None = None) -> list[tuple[str, str, int]]:
    """alembic 迁移（Python 源）→ [(kind, label, line)]；section 限定 upgrade/downgrade。"""
    scope = _alembic_section(text, section) if section else text
    if not scope:
        return []
    off = text.index(scope)
    out = []
    for m in OP_CALL.finditer(scope):
        line = text.count("\n", 0, off + m.start()) + 1
        name = m.group(1)
        call, _ = _extract_call(scope, m.end() - 1)
        k, lab = _classify_op(name, call, _column_args(call))
        if k and lab != "neutral":
            out.append((k, lab, line))
    return out

def _classify_op(name: str, call: str, inner_col: str | None):
    low = call.lower()
    if name == "drop_table":
        return ("DROP_TABLE", "destructive")
    if name in ("drop_column", "drop_columns"):
        return ("DROP_COLUMN", "destructive")
    if name in ("drop_constraint", "drop_index"):
        return ("DROP_CONSTRAINT", "destructive")
    if name == "rename_table":
        return ("RENAME", "destructive")
    if name == "alter_column":
        if re.search(r"\btype_\s*=|\busing\s*=", low):
            return ("ALTER_TYPE", "destructive")
        if re.search(r"\bnew_column_name\s*=", low):
            return ("RENAME", "destructive")
        if re.search(r"\bnullable\s*=\s*(false|0)\b", low):
            return ("SET_NOT_NULL", "destructive")
        if re.search(r"\bserver_default\s*=\s*none\b", low):
            return ("DROP_DEFAULT", "destructive")
        if re.search(r"\bnullable\s*=\s*(true|1)\b", low):
            return ("RELAX_NOTNULL", "additive")   # 放宽（downgrade 常见逆操作）
        return ("", "neutral")
    if name == "add_column":
        if inner_col is not None:
            c = inner_col.lower()
            notnull = re.search(r"\bnullable\s*=\s*(false|0)\b", c) \
                or not re.search(r"\bnullable\s*=", c)          # 缺省即 NOT NULL
            has_default = re.search(r"\bserver_default\s*=(?!\s*none)\s*\S", c) \
                or re.search(r"\bdefault\s*=", c)
            if notnull and not has_default:
                return ("ADD_NOTNULL_NO_DEFAULT", "destructive")
        return ("ADD_COLUMN", "additive")
    if name.startswith("create_"):
        return ("CREATE_OBJECT", "additive")
    if name == "execute":
        sql = _str_literals(call)
        if not sql.strip():
            return ("", "neutral")
        for k, lab, _ in classify_sql_text(sql):
            if lab == "destructive":
                return (f"EXEC:{k}", "destructive")
        return ("", "neutral")
    if re.search(r"drop|alter|rename|truncate", name):
        return ("UNCLASSIFIED_DDL", "destructive")   # fail-closed：未知危险 op
    return ("", "neutral")

# downgrade 逆操作映射（ADR-0038 决策 4）——kind 级
INVERSE_KINDS: dict[str, set[str]] = {
    "DROP_TABLE": {"CREATE_OBJECT"},
    "DROP_COLUMN": {"ADD_COLUMN", "CREATE_OBJECT"},
    "ALTER_TYPE": {"ALTER_TYPE", "ADD_COLUMN"},
    "SET_NOT_NULL": {"RELAX_NOTNULL", "ALTER_TYPE"},
    "ADD_NOTNULL_NO_DEFAULT": {"RELAX_NOTNULL", "ALTER_TYPE", "DROP_DEFAULT"},
    "RENAME": {"RENAME", "ALTER_TYPE", "ADD_COLUMN", "CREATE_OBJECT"},
}

def downgrade_has_inverse(destructive_kind: str, down_ops: list[tuple[str, str, int]]) -> bool:
    k = destructive_kind[5:] if destructive_kind.startswith("EXEC:") else destructive_kind
    if not down_ops:
        return False
    want = INVERSE_KINDS.get(k)
    if want is None:                                # TRUNCATE/UNCLASSIFIED 等：非空即认
        return len(down_ops) > 0
    have = {op_kind[5:] if op_kind.startswith("EXEC:") else op_kind for op_kind, _, _ in down_ops}
    return bool(want & have)

# ---------------------------------------------------- JSON Schema breaking --

META_KEYS = {"title", "description", "examples", "default", "$id", "$comment", "$schema",
             "definitions", "$defs", "deprecated", "readOnly", "writeOnly", "format"}
MIN_KEYS = {"minimum", "minLength", "minItems", "minProperties", "exclusiveMinimum"}
MAX_KEYS = {"maximum", "maxLength", "maxItems", "maxProperties", "exclusiveMaximum"}

def _deref(node, root, seen):
    if isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return None                              # 外部 ref：不可解析 → fail-closed
        if ref in seen:
            return node
        cur = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return _deref(cur, root, seen | {ref})
    return node

def schema_breaking(base, head, root_b, root_h, path="$") -> list[str]:
    base = _deref(base, root_b, set())
    head = _deref(head, root_h, set())
    if base is None or head is None:
        return [f"{path}: $ref 无法解析（外部引用或指针失效）——fail-closed 判 breaking"]
    if not isinstance(base, dict) or not isinstance(head, dict):
        if base != head:
            return [f"{path}: schema 节点形态改变 {base!r} → {head!r}"]
        return []
    out: list[str] = []
    bt, ht = base.get("type"), head.get("type")
    bset = set(bt) if isinstance(bt, list) else ({bt} if bt else None)
    hset = set(ht) if isinstance(ht, list) else ({ht} if ht else None)
    if bset and hset:
        if not bset <= hset:
            out.append(f"{path}.type: 类型收窄 {sorted(bset)} → {sorted(hset)}")
    elif bset is None and hset is not None:
        out.append(f"{path}.type: 新增 type 约束 {sorted(hset)}（原先开放）")
    be, he = base.get("enum"), head.get("enum")
    if isinstance(be, list):
        if isinstance(he, list):
            gone = [v for v in be if v not in he]
            if gone:
                out.append(f"{path}.enum: 收窄，丢失取值 {gone!r}")
        elif he is not None:
            out.append(f"{path}.enum: 形态改变")
    elif be is None and isinstance(he, list):
        out.append(f"{path}.enum: 新增 enum 约束（原先开放）")
    bc, hc = base.get("const"), head.get("const")
    if bc is not None and hc is not None and bc != hc:
        out.append(f"{path}.const: {bc!r} → {hc!r}")
    if bc is None and hc is not None:
        out.append(f"{path}.const: 新增 const 约束")
    br, hr = set(base.get("required") or []), set(head.get("required") or [])
    if br < hr:
        out.append(f"{path}.required: 新增必填 {sorted(hr - br)}")
    if base.get("additionalProperties") is not False and head.get("additionalProperties") is False:
        out.append(f"{path}.additionalProperties: 收紧为 false（关闭开放属性）")
    for k in MIN_KEYS | MAX_KEYS:
        bv, hv = base.get(k), head.get(k)
        if isinstance(bv, (int, float)) and isinstance(hv, (int, float)):
            if (k in MIN_KEYS and hv > bv) or (k in MAX_KEYS and hv < bv):
                out.append(f"{path}.{k}: 收紧 {bv} → {hv}")
        elif bv is None and isinstance(hv, (int, float)):
            out.append(f"{path}.{k}: 新增边界 {hv}")
    if "pattern" in head and head["pattern"] != base.get("pattern"):
        out.append(f"{path}.pattern: 新增/变更正则约束 {head['pattern']!r}")
    bp, hp = base.get("properties") or {}, head.get("properties") or {}
    for k in bp:
        if k not in hp:
            out.append(f"{path}.properties.{k}: 属性被删除（消费方依赖即断）")
    for k in bp.keys() & hp.keys():
        out += schema_breaking(bp[k], hp[k], root_b, root_h, f"{path}.properties.{k}")
    if "items" in base or "items" in head:
        if "items" not in base:
            out.append(f"{path}.items: 新增 items 约束（原先开放）")
        elif "items" in head:
            out += schema_breaking(base["items"], head["items"], root_b, root_h, f"{path}.items")
    for comb in ("allOf", "anyOf", "oneOf"):
        bl, hl = base.get(comb), head.get(comb)
        if isinstance(bl, list) and isinstance(hl, list):
            if len(bl) != len(hl):
                out.append(f"{path}.{comb}: 子 schema 数量 {len(bl)} → {len(hl)}"
                           "（不可结构化比对，fail-closed）")
            else:
                for i, (a, b) in enumerate(zip(bl, hl)):
                    out += schema_breaking(a, b, root_b, root_h, f"{path}.{comb}[{i}]")
        elif bl is None and isinstance(hl, list):
            out.append(f"{path}.{comb}: 新增组合约束（原先开放）")
    return out

# --------------------------------------------------------------- oasdiff ----

def oasdiff_check(bin_path: str, base_file: str, head_file: str,
                  fail_on: str = "WARN") -> tuple[str, list[str]]:
    """→ (verdict, detail)；verdict ∈ compatible|breaking|error（error=fail-closed 红）"""
    try:
        r = subprocess.run([bin_path, "breaking", "--fail-on", fail_on, base_file, head_file],
                           capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        return ("error", [f"oasdiff 执行失败: {e}"])
    detail = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
    if r.returncode == 0:
        return ("compatible", detail)
    if r.returncode == 1:
        return ("breaking", detail)
    return ("error", detail)

# ------------------------------------------------------------ ADR 引用 ------

def adr_refs(text: str) -> list[str]:
    return sorted(set(ADR_RE.findall(text or "")))

def adr_refs_valid(refs: list[str], verify_existence: bool) -> tuple[bool, list[str]]:
    if not refs:
        return (False, ["PR title/body 未引用任何 ADR-NNNN"])
    if not verify_existence:
        return (True, [f"ADR 引用: {' '.join(refs)}（存在性校验未启用）"])
    tok = os.environ.get("GITHUB_TOKEN", "")
    env = dict(os.environ, **{"GH_TOKEN": tok}) if tok else dict(os.environ)
    try:
        r = subprocess.run(["gh", "api", ADR_DIR_API, "--paginate", "--jq", ".[].name"],
                           capture_output=True, text=True, env=env, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        return (False, [f"agent-registry/decisions 清单拉取失败（fail-closed）: {e}"])
    if r.returncode != 0:
        return (False, [f"agent-registry/decisions 清单拉取失败（fail-closed）: "
                        f"{(r.stderr or '').strip()[:200]}"])
    names = r.stdout.split()
    missing = [x for x in refs if not any(n.startswith(f"{x}-") for n in names)]
    return ((not missing), [f"{x} 不存在于 agent-registry/decisions/（幽灵 ADR）" for x in missing])

# --------------------------------------------------------------- pr 模式 ----

def run_pr(repo: str, root: str, base: str, head: str, pr_title: str, pr_body: str,
           policy_mode: str, policy_path: str | None, verify_adr: bool,
           oasdiff_bin: str | None) -> int:
    findings: list[Finding] = []
    breaking_contracts: list[str] = []
    destructive: list[tuple[str, Finding]] = []

    try:
        text, src = load_policy_text(policy_mode, policy_path)
        pol = yaml.safe_load(text)
    except Exception as e:
        err(Finding("POLICY_FETCH_FAILED", str(e)))
        return 1
    findings += validate_policy(pol)
    print(f"contract-check: policy 来源 = {src}")

    # repo 键兼容全名（owner/name，GITHUB_REPOSITORY）与短名（policy 惯例）
    repos_pol = pol.get("repos") or {}
    entry = repos_pol.get(repo)
    if entry is None and "/" in repo:
        entry = repos_pol.get(repo.split("/", 1)[1])
    if entry is None:
        print(f"contract-check: N/A —— {repo} 未在 policy 中声明（policy: {src}；"
              "非静默 skip：job 真跑并显式记录，ADR-0038 决策 7）")
        return 0
    contracts = entry.get("contracts") or []
    migrations = entry.get("migrations")
    if not contracts and not migrations:
        print(f"contract-check: N/A —— {repo} 声明为无契约/迁移面（policy: {src}）")
        return 0
    breaking_requires_adr = bool(entry.get(
        "breaking_requires_adr", pol.get("defaults", {}).get("breaking_requires_adr", True)))

    files = ls_files(root)

    # ---- T6 失明防护：policy 声明与实际文件对账（每次运行都跑，与是否变更无关）----
    for d in contracts:
        if not [f for f in files if glob_match(f, d["path"])]:
            findings.append(Finding("DECLARED_PATH_MISSING",
                f"policy 声明的契约路径 {d['path']}（kind={d['kind']}）在 HEAD 无任何命中——"
                "契约文件被移走/改名后未更新 policy 声明 = 检测器失明（T6，ADR-0038 决策 5）"))
    mig_tool = None
    if migrations:
        mig_tool = migrations["tool"]
        pref = migrations["dir"].rstrip("/") + "/"
        if not [f for f in files if f.startswith(pref)]:
            findings.append(Finding("DECLARED_PATH_MISSING",
                f"policy 声明的迁移目录 {migrations['dir']}（tool={mig_tool}）"
                "在 HEAD 无任何文件——迁移目录被移走 = 检测器失明（T6）"))

    # ---- diff 分派 ----
    for status, path, prev in diff_name_status(root, base, head):
        hit_decls = decl_matches(path, contracts)
        is_migration = bool(migrations) and path.startswith(
            (migrations["dir"].rstrip("/") + "/"))
        if not hit_decls and not is_migration:
            continue
        if status.startswith("D"):
            findings.append(Finding("CONTRACT_REMOVED",
                f"声明路径下的文件被删除/移走: {path}"
                + (f"（原路径 {prev}；若为移动，须同步更新 policy 声明并引用 ADR）" if prev else ""),
                file=path))
            continue
        if hit_decls:
            kind = hit_decls[0]["kind"]
            base_txt = show(root, base, path)
            head_txt = show(root, head, path)
            if base_txt is None:
                print(f"contract-check: 新契约文件 {path}（{kind}）→ additive")
            elif kind == "openapi":
                if not oasdiff_bin:
                    findings.append(Finding("OASDIFF_MISSING",
                        f"{path}: openapi 检测需 OASDIFF_BIN（workflow 未提供）", file=path))
                else:
                    with tempfile.TemporaryDirectory() as td:
                        bf = os.path.join(td, "b.yaml")
                        hf = os.path.join(td, "h.yaml")
                        with open(bf, "w", encoding="utf-8") as fh:
                            fh.write(base_txt)
                        with open(hf, "w", encoding="utf-8") as fh:
                            fh.write(head_txt or "")
                        verdict, detail = oasdiff_check(
                            oasdiff_bin, bf, hf,
                            pol.get("defaults", {}).get("openapi_fail_on", "WARN"))
                    if verdict == "error":
                        findings.append(Finding("OASDIFF_ERROR",
                            f"{path}: oasdiff 工具错误（fail-closed）: "
                            + " | ".join(detail[:5]), file=path))
                    elif verdict == "breaking":
                        breaking_contracts.append(f"{path}: " + " | ".join(
                            l for l in detail if re.search(r"error|warning", l, re.I))[:600])
                    else:
                        print(f"contract-check: openapi 兼容变更 {path}")
            elif kind == "jsonschema":
                try:
                    bj, hj = json.loads(base_txt), json.loads(head_txt or "")
                except json.JSONDecodeError as e:
                    findings.append(Finding("SCHEMA_PARSE_ERROR",
                        f"{path}: JSON 解析失败（fail-closed）: {e}", file=path))
                else:
                    br = schema_breaking(bj, hj, bj, hj)
                    if br:
                        breaking_contracts.append(f"{path}: " + "; ".join(br)[:600])
                    else:
                        print(f"contract-check: jsonschema 兼容变更 {path}")
        if is_migration:
            content = show(root, head, path) or ""
            if mig_tool == "alembic" and path.endswith(".py"):
                rows = classify_alembic_text(content, "upgrade")
            else:
                rows = classify_sql_text(content)
            for kind, label, line in rows:
                if label == "destructive":
                    destructive.append((path, Finding(
                        kind, f"{mig_tool} 迁移含 destructive DDL [{kind}]（第 {line} 行起）",
                        file=path, line=line)))

    # ---- 判定 ----
    rc = 0
    for f in findings:
        err(f)
        rc = 1
    if breaking_contracts:
        if breaking_requires_adr:
            ok, msgs = adr_refs_valid(adr_refs(pr_title + "\n" + pr_body), verify_adr)
            if ok:
                print("::warning::契约 breaking 变更已携带 ADR 引用（policy 允许留痕放行）: "
                      + " ; ".join(breaking_contracts)[:800])
            else:
                for m in msgs:
                    err(Finding("BREAKING_NO_ADR", m))
                for b in breaking_contracts:
                    err(Finding("CONTRACT_BREAKING", b))
                rc = 1
        else:
            for b in breaking_contracts:
                err(Finding("CONTRACT_BREAKING", b))
            rc = 1
    if destructive:
        ok, msgs = adr_refs_valid(adr_refs(pr_title + "\n" + pr_body), verify_adr)
        for path, f in destructive:
            if not ok:
                err(Finding("DESTRUCTIVE_NO_ADR",
                    f"{f.reason}；且缺少完备手续: {'; '.join(msgs)}", file=f.file, line=f.line))
                rc = 1
                continue
            content = show(root, head, path) or ""
            if mig_tool == "alembic" and path.endswith(".py"):
                down = classify_alembic_text(content, "downgrade")
                if downgrade_has_inverse(f.kind, down):
                    print(f"::warning::{f.reason}；已附 ADR 引用 + downgrade 逆操作 → "
                          "按完备手续放行（ADR-0038 决策 4）")
                else:
                    err(Finding("DESTRUCTIVE_NO_ROLLBACK",
                        f"{f.reason}；但 downgrade() 无逆操作（逆映射见引擎 INVERSE_KINDS）",
                        file=f.file, line=f.line))
                    rc = 1
            else:
                # 裸 SQL 无 downgrade 节：回滚脚本要求仅适用 alembic（ADR-0038 决策 4）
                print(f"::warning::{f.reason}；裸 SQL 迁移无 downgrade 节——ADR 引用即完备手续"
                      "（回滚脚本要求仅适用 alembic，见 ADR-0038）")
    if rc == 0:
        print(f"contract-check: PASS（repo={repo}, contracts={len(contracts)}, "
              f"migrations={'yes' if migrations else 'no'}, policy={src}）")
    return rc

# -------------------------------------------------------------- selftest ----

FX = os.path.join(HERE, "tests")

def _git_config_identity():
    for k, v in (("user.name", "selftest"), ("user.email", "selftest@example.com")):
        subprocess.run(["git", "config", "--global", "--add", k, v], capture_output=True)

def _mkrepo(td: str, files: dict[str, str]) -> str:
    _git_config_identity()
    subprocess.run(["git", "init", "-q", "-b", "main", td], check=True, capture_output=True)
    for p, c in files.items():
        fp = os.path.join(td, p)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(c)
    subprocess.run(["git", "add", "-A"], cwd=td, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=td, check=True, capture_output=True)
    return git("rev-parse", "HEAD", cwd=td).strip()

def _commit(td: str, files: dict[str, str | None], msg: str) -> str:
    for p, c in files.items():
        if c is None:
            subprocess.run(["git", "rm", "-q", p], cwd=td, check=True, capture_output=True)
            continue
        fp = os.path.join(td, p)
        os.makedirs(os.path.dirname(fp) or td, exist_ok=True)
        with open(fp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(c)
    subprocess.run(["git", "add", "-A"], cwd=td, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=td, check=True, capture_output=True)
    return git("rev-parse", "HEAD", cwd=td).strip()

def _pr(pol: str, repo: str, td: str, base: str, head: str, title="", body="",
        oasdiff_bin=None) -> int:
    return run_pr(repo, td, base, head, title, body, "path", pol, False, oasdiff_bin)

POLICY_FX = """\
version: 1
defaults: {breaking_requires_adr: true, openapi_fail_on: WARN}
repos:
  fx-api:
    contracts:
      - {kind: openapi, path: "specs/openapi.yaml"}
  fx-schema:
    contracts:
      - {kind: jsonschema, path: "specs/contracts/**"}
  fx-db:
    migrations: {tool: alembic, dir: alembic/versions}
  fx-sqldb:
    migrations: {tool: sql, dir: migrations}
  fx-none: {}
"""

OPENAPI_BASE = """\
openapi: 3.0.0
info: {title: svc, version: "1.0"}
paths:
  /items:
    get:
      operationId: listItems
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                type: object
                properties:
                  name: {type: string}
                  tags: {type: array, items: {type: string}}
"""

OPENAPI_BREAKING = """\
openapi: 3.0.0
info: {title: svc, version: "1.1"}
paths:
  /items:
    get:
      operationId: listItems
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                type: object
                required: [name]
                properties:
                  name: {type: integer}
"""

OPENAPI_ENDPOINT_GONE = OPENAPI_BREAKING.replace("  /items:", "  /gone:")

OPENAPI_COMPAT = """\
openapi: 3.0.0
info: {title: svc, version: "1.1"}
paths:
  /items:
    get:
      operationId: listItems
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                type: object
                properties:
                  name: {type: string}
                  tags: {type: array, items: {type: string}}
                  priority: {type: integer}
  /items/{id}:
    get:
      operationId: getItem
      parameters: [{name: id, in: path, required: true, schema: {type: string}}]
      responses:
        "200": {description: ok}
"""

SCHEMA_BASE = json.dumps({
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}, indent=1)

SCHEMA_BREAKING = json.dumps({
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "integer"},
    },
}, indent=1)

SCHEMA_COMPAT = json.dumps({
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "priority": {"type": "integer"},
    },
}, indent=1)

ALEMBIC_ADDITIVE = '''\
"""additive"""
def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.add_column("items", sa.Column("priority", sa.Integer(), nullable=True))
    op.create_index("ix_items_priority", "items", ["priority"])

def downgrade() -> None:
    op.drop_index("ix_items_priority", table_name="items")
    op.drop_column("items", "priority")
    op.drop_table("audit_log")
'''

ALEMBIC_DESTRUCTIVE = '''\
"""drop legacy column"""
def upgrade() -> None:
    op.add_column("items", sa.Column("flag", sa.Boolean(), nullable=False))
    op.drop_column("items", "legacy")
    op.alter_column("items", "size", type_=sa.Integer())

def downgrade() -> None:
    pass
'''

ALEMBIC_DESTRUCTIVE_OK = '''\
"""drop legacy column"""
def upgrade() -> None:
    op.drop_column("items", "legacy")
    op.alter_column("items", "size", type_=sa.Integer())

def downgrade() -> None:
    op.add_column("items", sa.Column("legacy", sa.Text(), nullable=True))
    op.alter_column("items", "size", type_=sa.Text())
'''

def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8", newline="\n")
    f.write(text)
    f.close()
    return f.name

def selftest(oasdiff_bin: str | None) -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = ""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}"
              + (f" —— {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    # ---------------- T7: DDL 分类器（≥12 条预标注 fixture） ----------------
    with open(os.path.join(FX, "ddl_cases.yaml"), encoding="utf-8") as fh:
        cases = yaml.safe_load(fh)["cases"]
    check("T7a 分类器 fixture 数 ≥ 12", len(cases) >= 12, f"got {len(cases)}")
    dist = {"destructive": 0, "additive": 0, "boundary": 0}
    for c in cases:
        rows = (classify_alembic_text(c["content"], "upgrade")
                if c["format"] == "alembic" else classify_sql_text(c["content"]))
        exp_raw = c["expected"] if isinstance(c["expected"], list) else [c["expected"]]
        exp = [] if exp_raw == ["neutral"] else exp_raw   # neutral ⇔ 无任何非中性判定
        got = sorted({lab for _, lab, _ in rows})
        ok = got == sorted(set(exp))
        if ok and "destructive" in exp:
            want = sorted(set(c.get("kinds", [])))
            got_k = sorted({k for k, lab, _ in rows if lab == "destructive"})
            ok = got_k == want
            check(f"T7 {c['name']}", ok, f"kinds={got_k} 期望={want}")
        else:
            check(f"T7 {c['name']}", ok, f"labels={got} 期望={sorted(set(exp))}")
        for e in exp_raw:
            dist[e] = dist.get(e, 0) + 1
    check("T7b destructive/additive 各 ≥ 4（边界另计）",
          dist["destructive"] >= 4 and dist["additive"] >= 4, str(dist))

    # ---------------- T1/T2: OpenAPI breaking（oasdiff） ----------------
    if oasdiff_bin and os.path.exists(oasdiff_bin):
        v, d = oasdiff_check(oasdiff_bin, write_tmp(OPENAPI_BASE), write_tmp(OPENAPI_BREAKING))
        check("T1a OpenAPI 字段 string→integer = breaking", v == "breaking",
              f"{v} {d[:3]}")
        v, d = oasdiff_check(oasdiff_bin, write_tmp(OPENAPI_BASE), write_tmp(OPENAPI_ENDPOINT_GONE))
        check("T1b OpenAPI 删除 endpoint = breaking", v == "breaking", f"{v} {d[:3]}")
        v, d = oasdiff_check(oasdiff_bin, write_tmp(OPENAPI_BASE), write_tmp(OPENAPI_COMPAT))
        check("T2 OpenAPI 加可选字段/加 endpoint = compatible", v == "compatible",
              f"{v} {d[:3]}")
    else:
        print("[SKIP] T1/T2 oasdiff（未提供 OASDIFF_BIN——CI 内必跑，本地可选）")

    # ---------------- jsonschema 分类器 ----------------
    b, h = json.loads(SCHEMA_BASE), json.loads(SCHEMA_BREAKING)
    br = schema_breaking(b, h, b, h)
    check("SC1 jsonschema type 改变+required 收紧+属性删除 = breaking",
          any(".type" in x for x in br) and any("required" in x for x in br)
          and any("tags" in x for x in br), str(br))
    b, h = json.loads(SCHEMA_BASE), json.loads(SCHEMA_COMPAT)
    check("SC2 jsonschema 加可选属性 = compatible", schema_breaking(b, h, b, h) == [])

    # ---------------- git 集成：T3/T4/T5/T6 + N/A + 删除/移走防护 ----------------
    tmpdir = tempfile.mkdtemp()
    pol = os.path.join(tmpdir, "policy.yaml")
    with open(pol, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(POLICY_FX)

    # T5 additive migration
    td = tempfile.mkdtemp()
    base = _mkrepo(td, {"alembic/versions/0001_init.py": ALEMBIC_ADDITIVE})
    head = _commit(td, {"alembic/versions/0002_add.py":
                        ALEMBIC_ADDITIVE.replace('"""additive"""', '"""add 2"""')}, "add")
    check("T5 additive migration（建表/可空列/索引）= 绿", _pr(pol, "fx-db", td, base, head) == 0)

    # T3 destructive 无 ADR
    td = tempfile.mkdtemp()
    base = _mkrepo(td, {"alembic/versions/0001_init.py": ALEMBIC_ADDITIVE})
    head = _commit(td, {"alembic/versions/0002_drop.py": ALEMBIC_DESTRUCTIVE}, "drop")
    check("T3 destructive migration 无 ADR = 红", _pr(pol, "fx-db", td, base, head) == 1)

    rows = classify_alembic_text(ALEMBIC_DESTRUCTIVE, "upgrade")
    kinds = [k for k, lab, _ in rows if lab == "destructive"]
    check("T3b 错误信息可指明类别与行号",
          "ADD_NOTNULL_NO_DEFAULT" in kinds and "DROP_COLUMN" in kinds
          and "ALTER_TYPE" in kinds and all(l > 0 for _, _, l in rows), str(rows))

    # T4：ADR 是必要非充分——downgrade 空仍红
    check("T4a destructive + ADR 引用但 downgrade 空 = 红",
          _pr(pol, "fx-db", td, base, head, title="chore: 清理 legacy（ADR-0038）") == 1)
    td = tempfile.mkdtemp()
    base = _mkrepo(td, {"alembic/versions/0001_init.py": ALEMBIC_ADDITIVE})
    head = _commit(td, {"alembic/versions/0002_drop.py": ALEMBIC_DESTRUCTIVE_OK}, "drop ok")
    check("T4b ADR + downgrade 含逆操作（add_column/alter_column）= 绿",
          _pr(pol, "fx-db", td, base, head, title="chore: 清理 legacy（ADR-0038）") == 0)
    check("T4c 同迁移无 ADR 引用 = 红（ADR 是必要条件）", _pr(pol, "fx-db", td, base, head) == 1)

    # T6 失明防护
    td = tempfile.mkdtemp()
    base = _mkrepo(td, {"README.md": "x"})
    head = _commit(td, {"README.md": "y"}, "r")
    check("T6a policy 声明路径找不到文件 = 红", _pr(pol, "fx-api", td, base, head) == 1)

    td = tempfile.mkdtemp()
    base = _mkrepo(td, {"specs/openapi.yaml": OPENAPI_BASE})
    head = _commit(td, {"specs/openapi.yaml": None, "docs/api.yaml": OPENAPI_BASE}, "move")
    check("T6b 契约文件移出声明路径（未更新 policy）= 红",
          _pr(pol, "fx-api", td, base, head) == 1)

    # N/A 显式
    check("NA1 声明为空契约面的仓 = 显式 N/A 且绿", _pr(pol, "fx-none", td, base, head) == 0)
    check("NA2 不在 policy 的仓 = 显式 N/A 且绿", _pr(pol, "not-declared", td, base, head) == 0)

    # jsonschema 仓集成
    td = tempfile.mkdtemp()
    base = _mkrepo(td, {"specs/contracts/task-in.json": SCHEMA_BASE})
    head = _commit(td, {"specs/contracts/task-in.json": SCHEMA_BREAKING}, "brk")
    check("SC3 jsonschema breaking（无 ADR）= 红", _pr(pol, "fx-schema", td, base, head) == 1)
    check("SC4 jsonschema breaking + ADR 引用 = 留痕放行",
          _pr(pol, "fx-schema", td, base, head, title="fix: 契约收紧（ADR-0038）") == 0)
    head = _commit(td, {"specs/contracts/task-in.json": SCHEMA_COMPAT}, "compat")
    check("SC5 jsonschema 兼容变更 = 绿", _pr(pol, "fx-schema", td, base, head) == 0)

    # 裸 SQL 迁移
    td = tempfile.mkdtemp()
    base = _mkrepo(td, {"migrations/001_init.sql": "CREATE TABLE t (id int);\n"})
    head = _commit(td, {"migrations/002_drop.sql": "ALTER TABLE t DROP COLUMN c;\n"}, "drop")
    check("SQL1 裸 SQL destructive 无 ADR = 红", _pr(pol, "fx-sqldb", td, base, head) == 1)
    check("SQL2 裸 SQL destructive + ADR = 绿（回滚脚本要求仅适用 alembic）",
          _pr(pol, "fx-sqldb", td, base, head, title="chore: drop（ADR-0038）") == 0)

    # bundled policy 可加载
    txt, src = load_policy_text("bundled", None)
    bundled = yaml.safe_load(txt)
    check("BP bundled policy 可加载且含本组织仓声明",
          isinstance(bundled.get("repos"), dict) and src == "bundled")

    print("\n==== selftest summary:",
          "PASS" if not failures else f"FAIL({len(failures)}): {failures}", "====")
    return 0 if not failures else 1

# ------------------------------------------------------------------ main ----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["pr", "selftest"], required=True)
    ap.add_argument("--repo")
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--base-sha")
    ap.add_argument("--head-sha")
    ap.add_argument("--pr-title", default=os.environ.get("PR_TITLE", ""))
    ap.add_argument("--pr-body", default=os.environ.get("PR_BODY", ""))
    ap.add_argument("--policy", choices=["org", "bundled", "path"], default="org")
    ap.add_argument("--policy-path")
    ap.add_argument("--verify-adr-existence", action="store_true",
                    default=os.environ.get("CB_VERIFY_ADR_EXISTENCE", "") == "1")
    ap.add_argument("--oasdiff-bin", default=os.environ.get("OASDIFF_BIN"))
    a = ap.parse_args()
    if a.mode == "selftest":
        sys.exit(selftest(a.oasdiff_bin))
    if not a.repo:
        a.repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not a.head_sha:
        a.head_sha = git("rev-parse", "HEAD", cwd=a.root).strip()
    if not a.base_sha:
        a.base_sha = git("rev-parse", "HEAD^", cwd=a.root).strip()
    sys.exit(run_pr(a.repo, a.root, a.base_sha, a.head_sha, a.pr_title, a.pr_body,
                    a.policy, a.policy_path, a.verify_adr_existence, a.oasdiff_bin))

if __name__ == "__main__":
    main()
