#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dep-supply-chain-check.py —— 依赖供应链 policy 判定（P2-5 / ADR-0039）

模式：
  --policy P --validate-only          policy schema 校验（CI-Workflows hygiene 每 PR 跑）
  --policy P --print-allow-licenses   输出 allow-licenses 逗号串（dep-review.yml review job 消费）
  --policy P --base-sha S --head-sha S2 [--repo-root R]
                                       PR 判定：cwd（或 --repo-root）= 调用方仓 git
                                       checkout；PR title/body 经 env PR_TITLE/PR_BODY
  --self-test                          离线 fixtures（#90 T2/T3/T6 单元级证明，含阈值消费证明）

判定语义（policy/dependency-supply-chain.yaml 为单一真源，阈值全部来自 policy）：
  硬红     —— ADR 引用不豁免：包不存在、包龄不足、lockfile↔manifest 不一致
  需 ADR   —— PR title/body 引用 ADR-NNNN 即过（人审留痕）：低下载量、单维护者、
              install/构建期脚本
  fail-closed —— registry/policy 不可达即红，不降级
范围：仅「新增」依赖（manifest/lockfile 新条目）；既有依赖 minor/patch 更新不在
本域（languages.yaml dependency_policy 划界）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ────────────────────────────── policy 装载与校验 ──────────────────────────────

ECOSYSTEMS = ("npm", "pypi", "uv", "go")


class PolicyError(Exception):
    pass


class NetworkError(Exception):
    pass


def _re_ok(p):
    try:
        re.compile(p)
        return True
    except re.error:
        return False


def validate_policy(data):
    """policy schema 校验——缺键/类型错/正则不可编译一律拒绝（gate 可解析校验的执法点）。"""
    e = []

    def need(cond, msg):
        if not cond:
            e.append(msg)

    need(isinstance(data, dict), "policy 根必须是 mapping")
    if not isinstance(data, dict):
        raise PolicyError("; ".join(e))
    need(data.get("version") == 1, "version 必须为 1")
    lic = data.get("license")
    need(isinstance(lic, dict) and lic.get("mode") == "allowlist",
         "license.mode 必须为 allowlist（拒绝式白名单）")
    allow = lic.get("allow") if isinstance(lic, dict) else None
    need(isinstance(allow, list) and bool(allow)
         and all(isinstance(x, str) and x.strip() for x in allow),
         "license.allow 必须为非空字符串列表（SPDX id）")
    pm = data.get("package_maturity")
    need(isinstance(pm, dict), "package_maturity 节缺失")
    if isinstance(pm, dict):
        for k in ("min_age_days", "min_weekly_downloads"):
            v = pm.get(k)
            need(isinstance(v, int) and not isinstance(v, bool) and v >= 0,
                 f"package_maturity.{k} 必须为非负整数")
        need(isinstance(pm.get("single_maintainer_needs_adr"), bool),
             "package_maturity.single_maintainer_needs_adr 必须为 bool")
        for k in ("age_applies_to", "downloads_applies_to", "maintainer_applies_to"):
            v = pm.get(k)
            need(isinstance(v, list) and all(x in ECOSYSTEMS for x in v),
                 f"package_maturity.{k} 必须为生态列表（{'/'.join(ECOSYSTEMS)}）")
    ins = data.get("install_scripts")
    need(isinstance(ins, dict) and isinstance(ins.get("needs_adr"), bool),
         "install_scripts.needs_adr 必须为 bool")
    if isinstance(ins, dict):
        need(isinstance(ins.get("npm_script_keys"), list) and ins.get("npm_script_keys"),
             "install_scripts.npm_script_keys 必须为非空列表")
    lf = data.get("lockfile")
    need(isinstance(lf, dict), "lockfile 节缺失")
    if isinstance(lf, dict):
        need(isinstance(lf.get("manifest_lock_consistency"), bool)
             and isinstance(lf.get("frozen_install_required"), bool),
             "lockfile 布尔开关（manifest_lock_consistency/frozen_install_required）缺失")
        rm = lf.get("required_mode")
        need(isinstance(rm, dict) and set(rm) >= {"npm", "pypi", "uv", "go"},
             "lockfile.required_mode 须覆盖 npm/pypi/uv/go（frozen 模式矩阵可对账）")
    fp = data.get("first_party")
    need(isinstance(fp, dict)
         and all(isinstance(fp.get(k), list) for k in ("npm_scopes", "go_module_prefixes", "pypi_names")),
         "first_party 节（npm_scopes/go_module_prefixes/pypi_names 列表）缺失")
    pat = data.get("adr_reference_pattern")
    need(isinstance(pat, str) and _re_ok(pat), "adr_reference_pattern 必须为可编译正则")
    if e:
        raise PolicyError("; ".join(e))
    return data


def load_policy(path):
    import yaml  # 延迟导入：self-test 内联 policy 不需要 PyYAML
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return validate_policy(data)


# ────────────────────────────── lockfile/manifest 解析 ──────────────────────────────

def classify(path):
    """路径 → (生态, lock|manifest) | None"""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    if base == "package-lock.json":
        return ("npm", "lock")
    if base == "package.json":
        return ("npm", "manifest")
    if base.startswith("requirements") and base.endswith(".txt"):
        return ("pypi", "lock")
    if base == "uv.lock":
        return ("uv", "lock")
    if base == "pyproject.toml":
        return ("uv", "manifest")
    if base == "go.sum":
        return ("go", "lock")
    if base == "go.mod":
        return ("go", "manifest")
    return None


def parse_npm_lock(text):
    """package-lock.json → {name: version}（v2/v3 packages{} 优先，v1 dependencies{} 兜底）。"""
    out = {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"package-lock.json 解析失败：{exc}")
    pkgs = data.get("packages")
    if isinstance(pkgs, dict):
        for key, val in pkgs.items():
            if not key or not isinstance(val, dict):
                continue
            name = key.rsplit("node_modules/", 1)[-1]
            if name and val.get("version"):
                out[name] = str(val["version"])
    deps = data.get("dependencies")
    if isinstance(deps, dict):
        for name, val in deps.items():
            if isinstance(val, dict) and val.get("version"):
                out.setdefault(name, str(val["version"]))
    return out


def npm_lock_root_deps(text):
    """lock v2/v3 根包（packages[""]）的解析后直接依赖版本。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    root = (data.get("packages") or {}).get("")
    out = {}
    if isinstance(root, dict):
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            vals = root.get(key)
            if isinstance(vals, dict):
                for name, ver in vals.items():
                    out[name] = str(ver)
    return out


def npm_manifest_deps(text):
    """package.json → {name: spec}（dependencies/dev/optional 合并）。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out = {}
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        vals = data.get(key)
        if isinstance(vals, dict):
            for name, spec in vals.items():
                out[str(name)] = str(spec)
    return out


def _semver(v):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", str(v).strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def spec_satisfied(version, spec):
    """最小 semver 满足判定：^/~/精确/major-only；解析不了的 spec 只做存在性检查（返回 True）。"""
    spec = str(spec).strip()
    if (not spec or spec in ("*", "latest") or spec.startswith(("workspace:", "git", "file:", "http", "npm:", "node:", "catalog:"))
            or " - " in spec or any(op in spec for op in (">=", "<=", ">", "<", "||"))):
        return True
    caret = spec.startswith("^")
    tilde = spec.startswith("~")
    core = spec.lstrip("^~=>").lstrip("v")
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+][\w.\-+]*)?$", core)
    if not m:
        return True
    have = _semver(version)
    if have is None:
        return True
    maj = int(m.group(1))
    mino = int(m.group(2)) if m.group(2) is not None else None
    pat = int(m.group(3)) if m.group(3) is not None else None
    want = (maj, mino or 0, pat or 0)
    if mino is None:  # major-only（"2"）
        return have[0] == maj
    if pat is None:  # major.minor（"2.1"）
        return have[:2] == (maj, mino)
    if caret:
        return have[0] == maj and have >= want
    if tilde:
        return have[:2] == (maj, mino) and have >= want
    return have == want


def unify_pypi(name):
    """PEP 503 名字归一化。"""
    return re.sub(r"[-_.]+", "-", str(name).lower())


def parse_pypi_requirements(text):
    out = {}
    for raw in text.splitlines():
        line = raw.split(" # ", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(===|==)\s*([^\s;#]+)", line)
        if m:
            out[unify_pypi(m.group(1))] = m.group(4)
    return out


def parse_uv_lock(text):
    import tomllib
    data = tomllib.loads(text)
    return {unify_pypi(p["name"]): str(p.get("version", ""))
            for p in data.get("package", []) if isinstance(p, dict) and p.get("name")}


def parse_pyproject_deps(text):
    import tomllib
    data = tomllib.loads(text)
    proj = data.get("project", {})
    specs = list(proj.get("dependencies") or [])
    for group in (proj.get("optional-dependencies") or {}).values():
        specs.extend(group or [])
    names = []
    for d in specs:
        m = re.match(r"^[A-Za-z0-9._\-]+", str(d).strip())
        if m:
            names.append(unify_pypi(m.group(0)))
    return names


def parse_gosum(text):
    """go.sum → ({module: version}, {(module, version)} 普通/​go.mod 双行集合)"""
    latest, pairs, gomod_pairs = {}, set(), set()
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 2:
            continue
        mod, ver = parts[0], parts[1]
        if ver.endswith("/go.mod"):
            gomod_pairs.add((mod, ver[: -len("/go.mod")]))
        else:
            pairs.add((mod, ver))
            latest.setdefault(mod, ver)
    return latest, pairs, gomod_pairs


def parse_gomod(text):
    """go.mod → (requires: [(mod, ver)], local_replaces: {mod})（本地 replace 不查 registry）。"""
    requires, local_replace = [], set()
    block = None  # None | 'require' | 'replace' | 'exclude'
    for raw in text.splitlines():
        line = re.sub(r"//.*$", "", raw).strip()
        if not line:
            continue
        m = re.match(r"^(require|replace|exclude|retract)\s*\(\s*$", line)
        if m:
            block = m.group(1)
            continue
        if block and line == ")":
            block = None
            continue
        if block == "require":
            parts = line.split()
            if len(parts) >= 2:
                requires.append((parts[0], parts[1]))
        elif block == "replace":
            m2 = re.match(r"^(\S+)\s+(\S+)?\s*=>\s*(\S+)", line)
            if m2 and (m2.group(3).startswith("./") or m2.group(3).startswith("../")):
                local_replace.add(m2.group(1))
        elif not block:
            m2 = re.match(r"^require\s+(\S+)\s+(\S+)", line)
            if m2:
                requires.append((m2.group(1), m2.group(2)))
            m3 = re.match(r"^replace\s+(\S+)(?:\s+\S+)?\s*=>\s*(\S+)", line)
            if m3 and (m3.group(2).startswith("./") or m3.group(2).startswith("../")):
                local_replace.add(m3.group(1))
    return requires, local_replace


# ────────────────────────────── registry 适配层（self-test 可注入） ──────────────────────────────

FETCH_IMPL = None  # self-test 注入：url -> (status, obj)


def fetch_json(url):
    if FETCH_IMPL is not None:
        return FETCH_IMPL(url)
    req = urllib.request.Request(
        url, headers={"User-Agent": "cloudbird-dep-supply-chain-gate/1.0 (CI; +https://github.com/Cloudbird-Software)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return exc.code, None
        raise NetworkError(f"{url} -> HTTP {exc.code}")
    except Exception as exc:  # 超时/DNS/解析
        raise NetworkError(f"{url} -> {exc}")


def npm_packument(name):
    url = "https://registry.npmjs.org/" + (
        urllib.parse.quote(name, safe="") if name.startswith("@") else name)
    status, obj = fetch_json(url)
    if status in (404, 410) or obj is None:
        return None
    return obj


def npm_weekly_downloads(name):
    status, obj = fetch_json(
        "https://api.npmjs.org/downloads/point/last-week/" + urllib.parse.quote(name, safe=""))
    if status != 200 or not isinstance(obj, dict):
        return 0
    return int(obj.get("downloads") or 0)


def pypi_project(name):
    status, obj = fetch_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    if status in (404, 410) or obj is None:
        return None
    return obj


def pypi_weekly_downloads(name):
    status, obj = fetch_json(f"https://pypistats.org/api/packages/{urllib.parse.quote(name)}/recent")
    if status != 200 or not isinstance(obj, dict):
        return 0
    return int((obj.get("data") or {}).get("last_week") or 0)


def go_version_time(module, version):
    escaped = "/".join(urllib.parse.quote(seg, safe="") for seg in module.split("/"))
    status, obj = fetch_json(f"https://proxy.golang.org/{escaped}/@v/{urllib.parse.quote(version, safe='.')}.info")
    if status in (404, 410) or not isinstance(obj, dict):
        return None
    return obj.get("Time")


def parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # PyPI upload_time 等无时缀 → 按 UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ────────────────────────────── 判定核心 ──────────────────────────────

class Violation:
    def __init__(self, eco, name, kind, message, needs_adr=False):
        self.eco, self.name, self.kind, self.message = eco, name, kind, message
        self.needs_adr = needs_adr
        self.resolved = False

    def render(self):
        tag = "ADR豁免" if self.resolved else ("需ADR" if self.needs_adr else "硬红")
        return f"[{self.eco}] {self.name}: {self.kind}({tag}) — {self.message}"


def is_first_party(eco, name, policy):
    fp = policy["first_party"]
    if eco == "npm":
        return any(name.startswith(s + "/") for s in fp["npm_scopes"])
    if eco == "go":
        return any(name.startswith(pfx) for pfx in fp["go_module_prefixes"])
    if eco == "pypi":
        return unify_pypi(name) in {unify_pypi(x) for x in fp["pypi_names"]}
    return False


def check_new_package(eco, name, version, policy, now):
    viol = []
    if is_first_party(eco, name, policy):
        return viol  # 组织自有包（天然年轻零下载）跳过 maturity/脚本判据；许可证检查不豁免
    pm = policy["package_maturity"]
    ins = policy["install_scripts"]

    def age_days(created):
        dt = parse_iso(created)
        if dt is None:
            return None
        return (now - dt).days

    if eco == "npm":
        doc = npm_packument(name)
        if doc is None:
            viol.append(Violation(eco, name, "package_not_found",
                                  f"npm 包「{name}」不存在于 registry.npmjs.org（幻觉包/抢注的典型形态：包名无任何发布记录——先确认包名真实存在再引入）"))
            return viol
        created = (doc.get("time") or {}).get("created")
        days = age_days(created)
        if "npm" in pm["age_applies_to"]:
            if days is None:
                viol.append(Violation(eco, name, "age_unknown",
                                      f"npm 包「{name}」registry 元数据缺 time.created，包龄无法判定（fail-closed）"))
            elif days < pm["min_age_days"]:
                viol.append(Violation(eco, name, "age_below_min",
                                      f"npm 包「{name}」首次发布仅 {days} 天 < 阈值 {pm['min_age_days']} 天（新包=抢注/typosquat 高危窗口；policy 硬红，ADR 不豁免）"))
        if (pm["single_maintainer_needs_adr"] and "npm" in pm["maintainer_applies_to"]
                and len(doc.get("maintainers") or []) == 1):
            viol.append(Violation(eco, name, "single_maintainer",
                                  f"npm 包「{name}」维护者数=1 且无组织背书（bus factor/单点投毒）", needs_adr=True))
        if ins["needs_adr"]:
            ver_meta = (doc.get("versions") or {}).get(version) or {}
            hit = sorted(set((ver_meta.get("scripts") or {})) & set(ins["npm_script_keys"]))
            if hit:
                viol.append(Violation(eco, name, "install_scripts",
                                      f"npm 包「{name}@{version}」manifest 含安装期脚本 {hit}（安装即以依赖作者身份执行任意代码）", needs_adr=True))
        if "npm" in pm["downloads_applies_to"]:
            dl = npm_weekly_downloads(name)
            if dl < pm["min_weekly_downloads"]:
                viol.append(Violation(eco, name, "downloads_below_min",
                                      f"npm 包「{name}」周下载量 {dl} < 阈值 {pm['min_weekly_downloads']}（低流行度，幻觉/弃养风险）", needs_adr=True))
        return viol

    if eco == "pypi":
        doc = pypi_project(name)
        if doc is None:
            viol.append(Violation(eco, name, "package_not_found",
                                  f"PyPI 包「{name}」不存在于 pypi.org（幻觉包/抢注的典型形态：包名无任何发布记录——先确认包名真实存在再引入）"))
            return viol
        releases = doc.get("releases") or {}
        times = [f.get("upload_time_iso_8601") or f.get("upload_time")
                 for files in releases.values() for f in files if isinstance(f, dict)]
        times = sorted(t for t in (parse_iso(x) for x in times) if t)
        if "pypi" in pm["age_applies_to"]:
            if not times:
                viol.append(Violation(eco, name, "age_unknown",
                                      f"PyPI 包「{name}」无任何 release 上传时间，包龄无法判定（fail-closed）"))
            elif (now - times[0]).days < pm["min_age_days"]:
                viol.append(Violation(eco, name, "age_below_min",
                                      f"PyPI 包「{name}」首次发布仅 {(now - times[0]).days} 天 < 阈值 {pm['min_age_days']} 天（新包=抢注/typosquat 高危窗口；policy 硬红，ADR 不豁免）"))
        if ins["needs_adr"] and version:
            files = releases.get(version) or []
            if files and not any(f.get("packagetype") == "bdist_wheel" for f in files if isinstance(f, dict)):
                viol.append(Violation(eco, name, "install_scripts",
                                      f"PyPI 包「{name}=={version}」锁定版本仅有 sdist 无 wheel——安装即执行 setup.py（构建期任意代码）", needs_adr=True))
        if "pypi" in pm["downloads_applies_to"]:
            dl = pypi_weekly_downloads(name)
            if dl < pm["min_weekly_downloads"]:
                viol.append(Violation(eco, name, "downloads_below_min",
                                      f"PyPI 包「{name}」周下载量 {dl} < 阈值 {pm['min_weekly_downloads']}（低流行度，幻觉/弃养风险）", needs_adr=True))
        return viol

    if eco == "go":
        t = go_version_time(name, version)
        if t is None:
            viol.append(Violation(eco, name, "package_not_found",
                                  f"Go module「{name}@{version}」不存在于 proxy.golang.org（幻觉模块名/抢注的典型形态——先确认模块路径真实存在再引入）"))
            return viol
        dt = parse_iso(t)
        if "go" in pm["age_applies_to"]:
            if dt is None:
                viol.append(Violation(eco, name, "age_unknown",
                                      f"Go module「{name}」proxy 元数据缺发布时间，包龄无法判定（fail-closed）"))
            elif (now - dt).days < pm["min_age_days"]:
                viol.append(Violation(eco, name, "age_below_min",
                                      f"Go module「{name}」@{version} 发布仅 {(now - dt).days} 天 < 阈值 {pm['min_age_days']} 天（新模块=抢注高危窗口；policy 硬红，ADR 不豁免）"))
        return viol

    return viol


def check_lockfile_consistency(head_get, policy, changed):
    """静态一致性（名/精确版本级）：manifest ↔ lock 不一致即红。深度（哈希级）校验
    执法点在各仓安装命令（policy lockfile.required_mode，frozen/immutable）。"""
    viol = []
    if not policy["lockfile"]["manifest_lock_consistency"]:
        return viol
    touched = {classify(p)[0] for p in changed if classify(p)}

    def head_text(path):
        return head_get(path)

    if "npm" in touched:
        pkg = head_text("package.json")
        lock = head_text("package-lock.json")
        if pkg and lock:
            manifest = npm_manifest_deps(pkg)
            locked = npm_lock_root_deps(lock) or parse_npm_lock(lock)
            for name, spec in sorted(manifest.items()):
                if name not in locked:
                    viol.append(Violation("npm", name, "lockfile_mismatch",
                                          f"package.json 依赖「{name}」在 package-lock.json 无锁定条目（忘更新 lockfile？frozen 安装 npm ci 也会在此失败）"))
                elif not spec_satisfied(locked[name], spec):
                    viol.append(Violation("npm", name, "lockfile_mismatch",
                                          f"package.json 要求 {name}@{spec} 但 lockfile 锁定 {locked[name]}（手改 lockfile 或忘更新——frozen 安装 npm ci 也会在此失败）"))
    if "go" in touched:
        gomod = head_text("go.mod")
        gosum = head_text("go.sum")
        if gomod and gosum:
            requires, _local = parse_gomod(gomod)
            _, pairs, gomod_pairs = parse_gosum(gosum)
            known = pairs | gomod_pairs
            for mod, ver in sorted(set(requires)):
                if (mod, ver) not in known:
                    viol.append(Violation("go", mod, "lockfile_mismatch",
                                          f"go.mod require {mod}@{ver} 在 go.sum 无对应哈希行（手改 go.sum 或忘更新——go mod verify/build 也会在此失败）"))
    if "uv" in touched:
        pyproject = head_text("pyproject.toml")
        uvlock = head_text("uv.lock")
        if pyproject and uvlock:
            names = parse_pyproject_deps(pyproject)
            locked = set(parse_uv_lock(uvlock))
            for n in sorted(set(names) - locked):
                viol.append(Violation("uv", n, "lockfile_mismatch",
                                      f"pyproject.toml 依赖「{n}」在 uv.lock 无锁定条目（忘更新 lockfile？uv sync --locked 也会在此失败）"))
    return viol


def evaluate(changed, base_get, head_get, policy, pr_title, pr_body, now):
    """主判定：变更文件清单 + 两侧内容读取器 → 违规列表。"""
    # 1) 每生态 lockfile 的 base/head 依赖集
    eco_state = {}  # eco -> {"base": {...}, "head": {...}, "head_get": fn}
    for path in changed:
        cls = classify(path)
        if not cls:
            continue
        eco, kind = cls
        if kind != "lock":
            continue
        st = eco_state.setdefault(eco, {})
        base_txt = base_get(path)
        head_txt = head_get(path)
        if eco == "npm":
            st["base"] = parse_npm_lock(base_txt) if base_txt else {}
            st["head"] = parse_npm_lock(head_txt) if head_txt else {}
        elif eco == "pypi":
            st["base"] = parse_pypi_requirements(base_txt) if base_txt else {}
            st["head"] = parse_pypi_requirements(head_txt) if head_txt else {}
        elif eco == "uv":
            st["base"] = parse_uv_lock(base_txt) if base_txt else {}
            st["head"] = parse_uv_lock(head_txt) if head_txt else {}
        elif eco == "go":
            st["base"] = parse_gosum(base_txt)[0] if base_txt else {}
            st["head"] = parse_gosum(head_txt)[0] if head_txt else {}

    # 2) 新增包（仅新条目；既有 minor/patch 不在本域）→ registry 判定
    viol = []
    scanned = []
    for eco, st in sorted(eco_state.items()):
        new_names = sorted(set(st["head"]) - set(st["base"]))
        if not new_names:
            continue
        skip = set()
        if eco == "go":  # 本地 replace 的模块不查 registry（vendor/首方源码）
            gomod = head_get("go.mod")
            if gomod:
                _req, local = parse_gomod(gomod)
                skip = local
        for name in new_names:
            if name in skip:
                continue
            scanned.append((eco, name, st["head"][name]))
            viol.extend(check_new_package(eco, name, st["head"][name], policy, now))

    # 3) lockfile↔manifest 静态一致性（head 侧）
    viol.extend(check_lockfile_consistency(head_get, policy, changed))

    # 4) needs_adr 判据解析：PR title/body 引用 ADR-NNNN（存在性由各仓 adr-required/
    #    drift-check 后验，本门只认引用留痕）
    ctx = f"{pr_title or ''}\n{pr_body or ''}"
    adr_hit = re.search(policy["adr_reference_pattern"], ctx)
    for v in viol:
        if v.needs_adr and adr_hit:
            v.resolved = True
    return viol, scanned


# ────────────────────────────── git 侧读取（PR 模式） ──────────────────────────────

def run_git(repo_root, *args, binary=False):
    r = subprocess.run(["git", "-C", repo_root, *args], capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"::error::git {' '.join(args)} 失败：{r.stderr.decode('utf-8', 'replace')[:300]}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def main_pr(policy, repo_root, base_sha, head_sha, pr_title, pr_body):
    now = datetime.now(timezone.utc)
    # fail-closed：base 对象必须可得，否则"新增集"判定不完整
    run_git(repo_root, "cat-file", "-e", f"{base_sha}^{{commit}}")
    run_git(repo_root, "cat-file", "-e", f"{head_sha}^{{commit}}")
    mb = run_git(repo_root, "merge-base", base_sha, head_sha).strip()
    changed = [p for p in run_git(repo_root, "diff", "--name-only", "-z", mb, head_sha, binary=True).decode("utf-8").split("\0") if p]

    def get(sha):
        def _get(path):
            r = subprocess.run(["git", "-C", repo_root, "show", f"{sha}:{path}"], capture_output=True)
            if r.returncode != 0:
                return None
            return r.stdout.decode("utf-8", "replace")
        return _get

    try:
        viol, scanned = evaluate(changed, get(mb), get(head_sha), policy, pr_title, pr_body, now)
    except NetworkError as exc:
        print(f"::error::registry 不可达（fail-closed，不降级）：{exc}")
        return 1
    except PolicyError as exc:
        print(f"::error::policy/lockfile 解析失败（fail-closed）：{exc}")
        return 1

    print(f"dep-supply-chain: 变更文件 {len(changed)}，新增依赖 {len(scanned)} 个"
          + (f"：{', '.join(f'{e}:{n}@{v}' for e, n, v in scanned)}" if scanned else ""))
    if not viol:
        print("dep-supply-chain: OK（无违规；阈值=policy/dependency-supply-chain.yaml）")
        return 0
    rc = 0
    for v in viol:
        if v.resolved:
            print(f"::notice::{v.render()}")
        else:
            print(f"::error::{v.render()}")
            rc = 1
    return rc


# ────────────────────────────── self-test（#90 T2/T3/T6 单元级，离线） ──────────────────────────────

def _base_policy():
    return {
        "version": 1,
        "license": {"mode": "allowlist",
                    "allow": ["MIT", "Apache-2.0", "BSD-3-Clause", "ISC"]},
        "package_maturity": {"min_age_days": 90, "min_weekly_downloads": 100,
                             "single_maintainer_needs_adr": True,
                             "age_applies_to": ["npm", "pypi", "go"],
                             "downloads_applies_to": ["npm", "pypi"],
                             "maintainer_applies_to": ["npm"]},
        "install_scripts": {"needs_adr": True,
                            "npm_script_keys": ["preinstall", "install", "postinstall"]},
        "lockfile": {"manifest_lock_consistency": True, "frozen_install_required": True,
                     "required_mode": {"npm": "npm ci", "pypi": "pip install --require-hashes",
                                       "uv": "uv sync --locked", "go": "go mod verify"}},
        "first_party": {"npm_scopes": ["@cloudbird-software"],
                        "go_module_prefixes": ["github.com/Cloudbird-Software/"],
                        "pypi_names": []},
        "adr_reference_pattern": r"\bADR-[0-9]{4}\b",
    }


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _fixtures():
    """URL → (status, obj)；未收录的 URL 返回 404（=包不存在）。"""
    fx = {
        # T5 同构的成熟 npm 包：老、高下载、多维护者、无 install 脚本
        "https://registry.npmjs.org/left-pad-mature": {
            "time": {"created": "2015-03-01T00:00:00.000Z", "1.3.0": "2018-01-01T00:00:00.000Z"},
            "maintainers": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
            "versions": {"1.3.0": {"scripts": {"test": "node test.js"}}}},
        "https://api.npmjs.org/downloads/point/last-week/left-pad-mature": {"downloads": 5_000_000},
        # T2a：发布不足 90 天的新包（2026-07-15 → 36 天）
        "https://registry.npmjs.org/npm-fresh-slopsquat": {
            "time": {"created": "2026-07-15T00:00:00Z"},
            "maintainers": [{"name": "x"}, {"name": "y"}],
            "versions": {"0.1.0": {"scripts": {}}}},
        "https://api.npmjs.org/downloads/point/last-week/npm-fresh-slopsquat": {"downloads": 12},
        # T3：含 postinstall 的成熟包
        "https://registry.npmjs.org/scripty-mature": {
            "time": {"created": "2016-01-01T00:00:00Z"},
            "maintainers": [{"name": "a"}, {"name": "b"}],
            "versions": {"2.0.0": {"scripts": {"postinstall": "node install.js"}}}},
        "https://api.npmjs.org/downloads/point/last-week/scripty-mature": {"downloads": 900_000},
        # 低下载量（老包，仅触发 downloads 判据）
        "https://registry.npmjs.org/old-obscure": {
            "time": {"created": "2017-01-01T00:00:00Z"},
            "maintainers": [{"name": "a"}, {"name": "b"}],
            "versions": {"1.0.0": {"scripts": {}}}},
        "https://api.npmjs.org/downloads/point/last-week/old-obscure": {"downloads": 7},
        # 单维护者（老、高下载，仅触发 maintainer 判据）
        "https://registry.npmjs.org/lonely-maintainer": {
            "time": {"created": "2016-06-01T00:00:00Z"},
            "maintainers": [{"name": "solo"}],
            "versions": {"1.0.0": {"scripts": {}}}},
        "https://api.npmjs.org/downloads/point/last-week/lonely-maintainer": {"downloads": 400_000},
        # PyPI：成熟有 wheel
        "https://pypi.org/pypi/mature-py-pkg/json": {
            "releases": {"1.0": [{"upload_time": "2020-01-01T00:00:00", "packagetype": "bdist_wheel"}]}},
        "https://pypistats.org/api/packages/mature-py-pkg/recent": {"data": {"last_week": 250_000}},
        # PyPI：仅 sdist（安装即执行 setup.py）
        "https://pypi.org/pypi/sdist-only-pkg/json": {
            "releases": {"0.9": [{"upload_time": "2019-01-01T00:00:00", "packagetype": "sdist"}]}},
        "https://pypistats.org/api/packages/sdist-only-pkg/recent": {"data": {"last_week": 300_000}},
        # Go：成熟 / 新 / 不存在（最后一个不入表 → 404）
        "https://proxy.golang.org/github.com/mature/go-mod/@v/v1.2.3.info": {"Version": "v1.2.3", "Time": "2020-06-01T00:00:00Z"},
        "https://proxy.golang.org/github.com/fresh/go-mod/@v/v0.1.0.info": {"Version": "v0.1.0", "Time": "2026-08-01T00:00:00Z"},
    }
    return fx


def _install_fetch():
    fx = _fixtures()

    def impl(url):
        if url in fx:
            return 200, fx[url]
        return 404, None
    return impl


def _files():
    npm_lock_head = json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "app", "dependencies": {"left-pad-mature": "1.3.0"}},
            "node_modules/left-pad-mature": {"version": "1.3.0"},
        }})
    return {
        "package-lock.json": npm_lock_head,
        "package.json": json.dumps({"name": "app", "dependencies": {"left-pad-mature": "^1.3.0"}}),
        "go.sum": "github.com/mature/go-mod v1.2.3 h1:aaa=\ngithub.com/mature/go-mod v1.2.3/go.mod h1:bbb=\n",
        "go.mod": "module app\n\ngo 1.25\n\nrequire github.com/mature/go-mod v1.2.3\n",
        "uv.lock": '[project]\nname = "app"\n\n[[package]]\nname = "mature-py-pkg"\nversion = "1.0"\n',
        "pyproject.toml": '[project]\nname = "app"\ndependencies = ["mature-py-pkg>=1.0"]\n',
        "requirements.txt": "mature-py-pkg==1.0 --hash=sha256:deadbeef\n",
    }


def self_test():
    global FETCH_IMPL
    FETCH_IMPL = _install_fetch()
    files = _files()
    ok = [0]

    def run(name, cond, detail=""):
        if cond:
            ok[0] += 1
            print(f"  PASS {name}")
        else:
            print(f"  FAIL {name} {detail}")
            raise SystemExit(f"self-test 失败：{name} {detail}")

    def ev(changed, base_over=None, head_over=None, policy=None, title="", body=""):
        head = dict(files)
        head.update(head_over or {})
        base = dict(files)
        base.update(base_over or {})
        return evaluate(changed, lambda p: base.get(p), lambda p: head.get(p),
                        policy or _base_policy(), title, body, NOW)

    # ── policy 校验 ──
    validate_policy(_base_policy())
    run("policy: 合法 schema 通过", True)
    bad = _base_policy()
    bad["package_maturity"]["min_age_days"] = "ninety"
    try:
        validate_policy(bad)
        run("policy: 非法类型被拒", False)
    except PolicyError:
        run("policy: 非法类型被拒", True)
    bad2 = _base_policy()
    del bad2["license"]["allow"]
    try:
        validate_policy(bad2)
        run("policy: 缺 allow 列表被拒", False)
    except PolicyError:
        run("policy: 缺 allow 列表被拒", True)

    # ── T5 正向：成熟依赖绿 ──
    base_lock = json.dumps({"lockfileVersion": 3, "packages": {"": {"name": "app", "dependencies": {}}}})
    viol, scanned = ev(["package-lock.json", "package.json"],
                       base_over={"package-lock.json": base_lock, "package.json": json.dumps({"name": "app", "dependencies": {}})})
    run("T5: 成熟依赖（老/高下载/多维护者/无脚本）判绿", not [v for v in viol if not v.resolved] and len(scanned) == 1, str([v.render() for v in viol]))

    # ── T2a 负向：新包（<90 天）硬红 ──
    viol, _ = ev(["package-lock.json"], base_over={
        "package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {}}),
        "package.json": json.dumps({"name": "app", "dependencies": {}}),
    }, head_over={
        "package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {
            "node_modules/npm-fresh-slopsquat": {"version": "0.1.0"}}}),
        "package.json": json.dumps({"name": "app", "dependencies": {"npm-fresh-slopsquat": "^0.1.0"}}),
    }, title="feat: add dep (ADR-0039)")  # ADR 引用也不救 age
    age = [v for v in viol if v.kind == "age_below_min"]
    run("T2a: <90 天新包硬红（ADR 引用不豁免）", len(age) == 1 and not age[0].resolved, str([v.render() for v in viol]))

    # ── T2b 负向：不存在的包 → 报「不存在」而非误导性错误 ──
    viol, _ = ev(["package-lock.json"], base_over={
        "package-lock.json": json.dumps({"packages": {}}),
    }, head_over={
        "package-lock.json": json.dumps({"packages": {"node_modules/hallucinated-not-real-xyz": {"version": "9.9.9"}}}),
    })
    nf = [v for v in viol if v.kind == "package_not_found"]
    run("T2b: 不存在包名 → package_not_found 且信息含「不存在」",
        len(nf) == 1 and "不存在" in nf[0].message, str([v.render() for v in viol]))

    # ── T3 负向：postinstall 无 ADR 红，有 ADR 过 ──
    scripty_lock = json.dumps({"packages": {"node_modules/scripty-mature": {"version": "2.0.0"}}})
    viol, _ = ev(["package-lock.json"], base_over={"package-lock.json": json.dumps({"packages": {}}),
                                                   "package.json": json.dumps({"dependencies": {}})},
                 head_over={"package-lock.json": scripty_lock,
                            "package.json": json.dumps({"dependencies": {"scripty-mature": "^2.0.0"}})})
    sc = [v for v in viol if v.kind == "install_scripts"]
    run("T3: postinstall 无 ADR → 红", len(sc) == 1 and not sc[0].resolved, str([v.render() for v in viol]))
    viol, _ = ev(["package-lock.json"], base_over={"package-lock.json": json.dumps({"packages": {}}),
                                                   "package.json": json.dumps({"dependencies": {}})},
                 head_over={"package-lock.json": scripty_lock,
                            "package.json": json.dumps({"dependencies": {"scripty-mature": "^2.0.0"}})},
                 title="chore: add scripty (ADR-0039)")
    sc = [v for v in viol if v.kind == "install_scripts"]
    run("T3: postinstall + ADR 引用 → 过（留痕）", len(sc) == 1 and sc[0].resolved, str([v.render() for v in viol]))

    # ── 需 ADR 判据：低下载 / 单维护者 / PyPI sdist ──
    viol, _ = ev(["package-lock.json"], base_over={"package-lock.json": json.dumps({"packages": {}})},
                 head_over={"package-lock.json": json.dumps({"packages": {"node_modules/old-obscure": {"version": "1.0.0"}}})})
    run("downloads: 周下载 7<100 无 ADR → 红", any(v.kind == "downloads_below_min" and not v.resolved for v in viol), str([v.render() for v in viol]))
    viol, _ = ev(["package-lock.json"], base_over={"package-lock.json": json.dumps({"packages": {}})},
                 head_over={"package-lock.json": json.dumps({"packages": {"node_modules/old-obscure": {"version": "1.0.0"}}})},
                 body="rationale in ADR-0001")
    run("downloads: +ADR 引用 → 过", any(v.kind == "downloads_below_min" and v.resolved for v in viol), str([v.render() for v in viol]))
    viol, _ = ev(["package-lock.json"], base_over={"package-lock.json": json.dumps({"packages": {}})},
                 head_over={"package-lock.json": json.dumps({"packages": {"node_modules/lonely-maintainer": {"version": "1.0.0"}}})})
    run("maintainer: 单维护者 → 需 ADR 红", any(v.kind == "single_maintainer" and not v.resolved for v in viol), str([v.render() for v in viol]))
    viol, _ = ev(["requirements.txt"], base_over={"requirements.txt": ""},
                 head_over={"requirements.txt": "sdist-only-pkg==0.9 --hash=sha256:abcd\n"})
    run("PyPI: 仅 sdist（安装即 setup.py）→ 需 ADR 红", any(v.kind == "install_scripts" and not v.resolved for v in viol), str([v.render() for v in viol]))

    # ── Go ──
    viol, _ = ev(["go.sum"], base_over={"go.sum": ""}, head_over={
        "go.sum": "github.com/mature/go-mod v1.2.3 h1:aaa=\ngithub.com/mature/go-mod v1.2.3/go.mod h1:bbb=\n"})
    run("Go: 成熟 module 判绿", not [v for v in viol if not v.resolved], str([v.render() for v in viol]))
    viol, _ = ev(["go.sum"], base_over={"go.sum": ""}, head_over={
        "go.sum": "github.com/fresh/go-mod v0.1.0 h1:ccc=\n"})
    run("Go: <90 天 module 硬红", any(v.kind == "age_below_min" and not v.resolved for v in viol), str([v.render() for v in viol]))
    viol, _ = ev(["go.sum"], base_over={"go.sum": ""}, head_over={
        "go.sum": "github.com/hallucinated/go-path v1.0.0 h1:ddd=\n"})
    nf = [v for v in viol if v.kind == "package_not_found"]
    run("Go: 不存在 module → 「不存在」", len(nf) == 1 and "不存在" in nf[0].message, str([v.render() for v in viol]))

    # ── T6 阈值消费证明：同一成熟依赖，min_age_days=10000 → 红 ──
    viol, _ = ev(["package-lock.json"], base_over={
        "package-lock.json": json.dumps({"packages": {}}),
        "package.json": json.dumps({"dependencies": {}}),
    })
    run("T6 前置: 同一依赖在阈值 90 下判绿", not [v for v in viol if not v.resolved], str([v.render() for v in viol]))
    p10000 = _base_policy()
    p10000["package_maturity"]["min_age_days"] = 10000
    viol, _ = ev(["package-lock.json"], base_over={
        "package-lock.json": json.dumps({"packages": {}}),
        "package.json": json.dumps({"dependencies": {}}),
    }, policy=p10000)
    run("T6: 仅改 policy 阈值 10000 → 同一依赖变红（判定消费 policy 而非硬编码）",
        any(v.kind == "age_below_min" for v in viol), str([v.render() for v in viol]))

    # ── T4 静态一致性（深度哈希级在各仓安装步，见 policy required_mode） ──
    viol, _ = ev(["package.json", "package-lock.json"],
                 head_over={"package.json": json.dumps({"name": "app", "dependencies": {"left-pad-mature": "^2.0.0"}})})
    run("T4a: package.json ^2.0.0 vs lock 1.3.0 → lockfile_mismatch 红",
        any(v.kind == "lockfile_mismatch" for v in viol), str([v.render() for v in viol]))
    viol, _ = ev(["package.json", "package-lock.json"],
                 head_over={"package.json": json.dumps({"name": "app", "dependencies": {"ghost-dep": "^1.0.0"}})})
    run("T4b: package.json 有 lock 无 → lockfile_mismatch 红",
        any(v.kind == "lockfile_mismatch" for v in viol), str([v.render() for v in viol]))
    viol, _ = ev(["go.mod", "go.sum"], head_over={
        "go.mod": "module app\n\ngo 1.25\n\nrequire github.com/mature/go-mod v9.9.9\n"})
    run("T4c: go.mod require v9.9.9 无 go.sum 哈希行 → 红",
        any(v.kind == "lockfile_mismatch" for v in viol), str([v.render() for v in viol]))
    viol, _ = ev(["pyproject.toml", "uv.lock"], head_over={
        "pyproject.toml": '[project]\nname = "app"\ndependencies = ["mature-py-pkg>=1.0", "missing-uv-dep>=1"]\n'})
    run("T4d: pyproject 依赖 uv.lock 无 → 红",
        any(v.kind == "lockfile_mismatch" for v in viol), str([v.render() for v in viol]))

    # ── first-party 豁免（不查 registry：未入 fixtures 即 404，豁免=不触发 not_found） ──
    viol, _ = ev(["package-lock.json"],
                 base_over={"package-lock.json": json.dumps({"packages": {}}),
                            "package.json": json.dumps({"dependencies": {}})},
                 head_over={"package-lock.json": json.dumps({"packages": {"node_modules/@cloudbird-software/brand-new": {"version": "0.0.1"}}}),
                            "package.json": json.dumps({"dependencies": {"@cloudbird-software/brand-new": "^0.0.1"}})})
    run("first-party: @cloudbird-software/* 年轻零下载包跳过 maturity 判据",
        not [v for v in viol if not v.resolved], str([v.render() for v in viol]))

    print(f"self-test: {ok[0]} 项全部通过（阈值消费=T6 已证）")
    return 0


# ────────────────────────────── CLI ──────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--policy")
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--print-allow-licenses", action="store_true")
    ap.add_argument("--base-sha")
    ap.add_argument("--head-sha")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.policy:
        ap.error("需要 --policy（或 --self-test）")
    policy = load_policy(args.policy)
    if args.validate_only:
        print(f"policy OK: {args.policy}（license={len(policy['license']['allow'])} 项白名单，"
              f"min_age_days={policy['package_maturity']['min_age_days']}，"
              f"min_weekly_downloads={policy['package_maturity']['min_weekly_downloads']}）")
    if args.print_allow_licenses:
        print(",".join(policy["license"]["allow"]))
    if args.base_sha and args.head_sha:
        return main_pr(policy, args.repo_root, args.base_sha, args.head_sha,
                       os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
