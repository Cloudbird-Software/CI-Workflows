#!/usr/bin/env python3
"""dashboard-update.py —— 管家账本 dashboard issue 刷新（宪法 §12 投影二 / ADR-0055 决策 8）

v2（W5-C4 .github#227，ADR-0073）：北极星对同屏互锁 + 四类指标全量。body 三区：
- 人类一屏区（**北极星对置顶**——AC-1"同屏实时"）：零接触合并数 × 质量护栏；
  护栏任一 red → 合并数显示归零+原因标注（呈现层归零，raw 保留 JSON——非数据删除）
- 状态一览：在制卡/板链接/SLI（#98 口径兼容）
- 机器可读区：`<!-- dashboard-json -->` 后 fenced JSON（v1 键全保留 + north_star/metrics）
指标计算=governance/metrics.py（纯库，阈值真源 policy/metrics.yaml）；
本脚本只做采集（GitHub API / drill 台账 / arbiter 误放行台账 / metering 归账）与呈现。

失效语义（两层，ADR-0073 决策 7）：
- 核心面（卡扫描/merged PR/账本 issue 写）API 失败 → exit 2 fail-closed（v1 不变）
- 辅助指标源（arbiter 台账/billing/metering/产品指标文件）失败 → 该指标 pending
  +原因可见，不冒充 0 也不拖垮整轮刷新（缺数据≠劣化，但盲区必须上屏）
驱动：butler-ledger.yml 每 15min（唤醒矩阵行 2，无需改 workflow——最小侵入）。

v1 SLI 口径（诚实标注，#98 T2 分母陷阱：零分母→null+N/A，不除零不出 100%）：
- automerge_rate：近 7 天 merged PR 中 merged_by==cloudbrid-agent[bot] 占比
 （proxy：App 身份执行合并；timeline 级 auto-merge 事件归 W5-C3）
- escape_rate（v2 起有数）：(非演习 [auto-revert] PR + post-merge P0)/merged（sli-report 同口径）
- stuck_prs：open PR 停留 >24h 数（跨 active 仓求和）
- 其余三项（human_touch_per_pr / false_red_rate / entropy_delta）：置 null + pending W5-C3
"""
import base64
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover
    print("FATAL 缺少 PyYAML（CI 预装；本地 pip install pyyaml）", file=sys.stderr)
    raise SystemExit(2)

ORG = "Cloudbird-Software"
HOME_REPO = ".github"
ISSUE_TITLE = "管家账本 dashboard（factory-floor）"
LABEL = {"name": "dashboard", "color": "F9D0C4",
         "description": "管家账本投影二（宪法 §12，机器可读 JSON+一屏摘要）"}
GH_API = "https://api.github.com"
DRY_RUN = "--dry-run" in sys.argv or os.environ.get("DASHBOARD_DRY_RUN") == "1"
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GOVERNANCE_TOKEN") or ""
DIR = os.path.dirname(os.path.abspath(__file__))
NOW = _dt.datetime.now(_dt.timezone.utc)
TRIGGER = os.environ.get("BUTLER_TRIGGER") or "manual"
APP_BOT = "cloudbrid-agent[bot]"
JSON_MARK = "<!-- dashboard-json -->"
FENCE = "`" * 8  # 长于任何合理用户输入的 fence（标题可含 ```——防截断机器可读区）

sys.path.insert(0, DIR)  # W5-C4 计算库同目录（阈值真源 policy/metrics.yaml，ADR-0073）
try:
    import metrics as metrics_lib
except ImportError:  # pragma: no cover
    print("FATAL 缺少 governance/metrics.py（W5-C4 计算库——同 PR 落盘）", file=sys.stderr)
    raise SystemExit(2)
METRICS_POLICY = metrics_lib.load_policy(os.path.join(DIR, "policy", "metrics.yaml"))


def _safe_text(s):
    """剥离用户可控文本里可破坏 fence / 伪造区标记的字面量（标题进 body 的必经清洗）。"""
    return (str(s or "").replace("`", "'")
            .replace("<!--", "<! --").replace("-->", "-- >"))


class Infra(Exception):
    pass


def _req(url, body=None, method=None):  # 状态码判定归调用方（send/显式检查）——不设 ok_codes 形参以免“声明了却不用”
    headers = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "dashboard-update",
               "Accept": "application/vnd.github+json"}
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw}
    except Exception as e:
        raise Infra(f"请求失败 {url}: {e}") from e


def get(path):
    st, payload = _req(f"{GH_API}{path}")
    if st != 200:
        raise Infra(f"GET {path} HTTP {st}: {str(payload.get('message'))[:120]}")
    return payload


def send(method, path, body, ok_codes=(200, 201)):
    st, payload = _req(f"{GH_API}{path}", body, method)
    if st not in ok_codes:
        raise Infra(f"{method} {path} HTTP {st}: {str(payload.get('message'))[:160]}")
    return payload


def active_repos():
    try:
        with open(os.path.join(DIR, "REPOS.yaml"), encoding="utf-8") as f:
            repos = yaml.safe_load(f)["repos"]
    except Exception as e:
        raise Infra(f"REPOS.yaml 读取失败: {e}") from e
    names = [r["name"] for r in repos if r.get("status") == "active"]
    if not names:
        raise Infra("REPOS.yaml 无 active 仓")
    return names


def _iso(s):
    return _dt.datetime.fromisoformat((s or NOW.isoformat()).replace("Z", "+00:00"))


def scan_cards(repos):
    """与 board-sync.py 同判据的独立轻量扫描（自包含；真相源=issue label）。"""
    cards = []
    for repo in repos:
        page = 1
        while True:
            batch = get(f"/repos/{ORG}/{repo}/issues?state=open&per_page=100&page={page}")
            for it in batch:
                if "pull_request" in it:
                    continue
                sl = sorted(l["name"] for l in it.get("labels", [])
                            if str(l.get("name", "")).startswith("state:"))
                if not sl:
                    continue
                if len(sl) > 1:  # 真相源唯一性被破坏（宪法 §12）——排序取首保证两投影一致
                    print(f"WARN multi-state {repo}#{it['number']}: {sl}"
                          f"——多 state 标签并存，本轮取 {sl[0]}，请修标签")
                cards.append({"repo": repo, "number": it["number"], "title": _safe_text(it["title"]),
                              "state": _safe_text(sl[0][len("state:"):]),
                              "assignee": (it.get("assignees") or [{}])[0].get("login", ""),
                              "url": it["html_url"], "updated_at": it.get("updated_at") or "",
                              "days_idle": max(0, (NOW - _iso(it.get("updated_at"))).days)})
            if len(batch) < 100:
                break
            page += 1
    return cards


Q_MERGED_PRS = """query($o:String!,$r:String!,$cur:String){
  repository(owner:$o,name:$r){
    pullRequests(states:MERGED, first:100, after:$cur,
                 orderBy:{field:UPDATED_AT,direction:DESC}){
      pageInfo{ hasNextPage endCursor }
      nodes{ mergedAt updatedAt title body mergedBy{ login } } } } }"""


def merged_prs(repos, days=14):
    """窗口内 merged PR 节点（GraphQL 批量——REST 列表端点无 mergedBy，15min
    节奏下逐 PR detail 是配额浪费，ADR-0055 决策 8）。v2 取 14 天：北极星逃逸
    护栏需要当前窗+上一窗双窗事件（sustained 判定事件时戳直算，ADR-0073 决策 1）。
    """
    since = NOW - _dt.timedelta(days=days)
    nodes = []
    for repo in repos:
        cur = None
        while True:
            body = {"query": Q_MERGED_PRS,
                    "variables": {"o": ORG, "r": repo, "cur": cur}}
            st, payload = _req(f"{GH_API}/graphql", body, "POST")
            if st != 200 or payload.get("errors"):
                raise Infra(f"GraphQL merged PRs {repo} HTTP {st}: "
                            + json.dumps(payload.get("errors", payload), ensure_ascii=False)[:200])
            conn = payload["data"]["repository"]["pullRequests"]
            page_min_updated = min((_iso(n["updatedAt"]) for n in conn["nodes"]),
                                   default=_dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc))
            nodes.extend(n for n in conn["nodes"]
                         if n.get("mergedAt") and _iso(n["mergedAt"]) >= since)
            # 按 UPDATED_AT 倒序翻页：页内最小 updatedAt 已出窗即止——后续页
            # updatedAt 更旧，而 mergedAt<=updatedAt，不可能再有窗口内合并
            if page_min_updated < since or not conn["pageInfo"]["hasNextPage"]:
                break
            cur = conn["pageInfo"]["endCursor"]
    return nodes


def sli_automerge(repos):
    """近 7 天 merged PR 中 App 身份合并占比（proxy；零分母→null N/A，#98 T2）。"""
    since = NOW - _dt.timedelta(days=7)
    merged = [n for n in merged_prs(repos) if _iso(n["mergedAt"]) >= since]
    auto = sum(1 for n in merged if (n.get("mergedBy") or {}).get("login") == APP_BOT)
    if not merged:
        return None, 0
    return round(auto / len(merged), 4), len(merged)


# @w5c4-pure-begin —— 纯函数区（governance/tests/test-metrics-wiring.sh 按标记对
# 提取本块离线单测——不复制实现，防"测试测影子"；标记对缺失=测试红）
DRILL_MARKS = ("演练", "演习", "[drill]")  # 演习数据约定标记（sli-report"演练"+ADR-0069"演习"双词兼容）


def _ts(s):
    """ISO→datetime（失败 None）；本块自包含（不依赖模块级 _iso——提取测试可独立运行）。"""
    try:
        return _dt.datetime.fromisoformat(str(s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_drill_text(*texts):
    joined = " ".join(str(t or "") for t in texts)
    return any(m in joined for m in DRILL_MARKS)


def partition_escapes(prs, p0s, now):
    """双窗逃逸事件：current=[now-7d,now)，previous=[now-14d,now-7d)。

    prs=merged PR 节点（[auto-revert] 标题约定）；p0s=post-merge 冒烟 P0 issue。
    演习数据（title/body 含约定标记）从分子排除且 drills_excluded 计数可见——
    过滤不可见=作弊通道（sli-report 先例）。reverts_current 供回滚率护栏（分子
    只算 revert，不含 P0）。
    """
    w = _dt.timedelta(days=7)
    cur = prev = drills = reverts_cur = 0
    for p in prs or []:
        if "[auto-revert]" not in str(p.get("title") or ""):
            continue
        if _is_drill_text(p.get("title"), p.get("body")):
            drills += 1
            continue
        ts = _ts(p.get("mergedAt"))
        if ts and now - 2 * w < ts <= now:
            if ts > now - w:
                cur += 1
                reverts_cur += 1
            else:
                prev += 1
    for i in p0s or []:
        if _is_drill_text(i.get("title"), i.get("body")):
            drills += 1
            continue
        ts = _ts(i.get("created_at"))
        if ts and now - 2 * w < ts <= now:
            if ts > now - w:
                cur += 1
            else:
                prev += 1
    return {"current": cur, "previous": prev, "reverts_current": reverts_cur,
            "drills_excluded": drills}


def drill_redrate_lines(lines):
    """drill history.jsonl 行→红率输入（kind=seed-drill，red/denom=red+green——
    与 drill.py redrate 同口径，no-surface 与 failclose 演习不入分母；畸形行计入 bad 可见）。"""
    red = denom = bad = 0
    for ln in lines or []:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            bad += 1
            continue
        if rec.get("kind") != "seed-drill":
            continue
        v = rec.get("verdict")
        if v in ("red", "green"):
            denom += 1
            if v == "red":
                red += 1
    return {"red": red, "denom": denom, "bad_lines": bad}


def false_decision_parse(text, now, window_days):
    """arbiter 误放行台账文本→（窗内 false-allow 数, 窗内 false-deny 数, 全部行列表）。
    `#` 注释行跳过（台账文件头约定）；date 出窗不计。"""
    allow = deny = 0
    lines = []
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        lines.append(rec)
        ts = _ts(rec.get("date"))
        if ts and (now - ts).days <= window_days:
            if rec.get("kind") == "false-allow":
                allow += 1
            elif rec.get("kind") == "false-deny":
                deny += 1
    return allow, deny, lines


def sign_durations(timelines):
    """type:intent issue 的 timeline 事件列表→（签署耗时秒列表, 在途 draft 数）。

    耗时=首个 labeled state:ir-signed 时刻 − 首个 labeled state:ir-draft 时刻
    （宪法 §7：签署耗时如实计入判断预算）。无 draft 事件的已签 IR 不可算→跳过
    （不造 0）；有 draft 无 signed→在途。"""
    durations, in_flight = [], 0
    for events in timelines or []:
        draft = signed = None
        for ev in events or []:
            if ev.get("event") != "labeled":
                continue
            name = (ev.get("label") or {}).get("name")
            ts = _ts(ev.get("created_at"))
            if not ts:
                continue
            if name == "state:ir-draft" and draft is None:
                draft = ts
            elif name == "state:ir-signed" and signed is None and draft is not None:
                signed = ts
        if draft and signed and signed > draft:
            durations.append(round((signed - draft).total_seconds()))
        elif draft and not signed:
            in_flight += 1
    return durations, in_flight


def dwell_hours(events, now, label="state:needs-human"):
    """timeline 事件→进入 label 态至今停留小时数（取最近一次 labeled 时刻——
    反复进出取当前段）。无该事件→None。"""
    latest = None
    for ev in events or []:
        if ev.get("event") != "labeled":
            continue
        if (ev.get("label") or {}).get("name") != label:
            continue
        ts = _ts(ev.get("created_at"))
        if ts and (latest is None or ts > latest):
            latest = ts
    if latest is None or latest > now:
        return None
    return round((now - latest).total_seconds() / 3600, 2)


def user_metric_from(content_text):
    """产品仓 user-result.yaml 文本→指标 dict（缺 metric_key/value=不完整→None）。
    本地 import yaml——提取测试独立运行不依赖模块级导入。"""
    import yaml
    try:
        d = yaml.safe_load(content_text)
    except Exception:
        return None
    if isinstance(d, dict) and d.get("metric_key") and "value" in d:
        return d
    return None
# @w5c4-pure-end


def sli_stuck(repos):
    """open PR 停留 >24h 数。"""
    cutoff = NOW - _dt.timedelta(hours=24)
    stuck = 0
    for repo in repos:
        page = 1  # 分页拉全量（>100 open PR 单页漏计——与 scan_cards 同教训）
        while True:
            prs = get(f"/repos/{ORG}/{repo}/pulls?state=open&per_page=100&page={page}")
            stuck += sum(1 for pr in prs if _iso(pr.get("created_at")) < cutoff)
            if len(prs) < 100:
                break
            page += 1
    return stuck


# ---------- v2 辅助指标源采集（失败→pending 盲区，不拖垮核心面——ADR-0073 决策 7） ----------

def _raw_content(repo, path):
    """仓文件原文（base64 解码）；失败→None（调用方落 pending）。"""
    st, payload = _req(f"{GH_API}/repos/{repo}/contents/{path}")
    if st != 200 or not payload.get("content"):
        return None
    try:
        return base64.b64decode(payload["content"]).decode("utf-8")
    except Exception:
        return None


def collect_escape(repos):
    """逃逸双窗（[auto-revert] PR + post-merge P0）。任一源失败→None（护栏 pending）。"""
    try:
        prs = merged_prs(repos)
        since = (NOW - _dt.timedelta(days=14)).strftime("%Y-%m-%d")
        q = urllib.parse.quote(f'org:{ORG} "post-merge 冒烟失败" created:>={since}')
        st, payload = _req(f"{GH_API}/search/issues?q={q}&per_page=100")
        if st != 200:
            print(f"WARN escape: P0 搜索失败 HTTP {st}——逃逸护栏 pending（盲区上屏）")
            return None
        return partition_escapes(prs, payload.get("items") or [], NOW)
    except Infra as e:
        print(f"WARN escape: 采集失败 {e}——逃逸护栏 pending（盲区上屏）")
        return None


def collect_drill():
    """演习红率（本地台账——butler-ledger checkout 自带，零 API）。文件缺失→None。"""
    path = os.path.join(DIR, "drill", "history.jsonl")
    if not os.path.exists(path):
        return None, None
    with open(path, encoding="utf-8") as f:
        agg = drill_redrate_lines(f.readlines())
    records = None
    if agg["denom"] or agg["bad_lines"]:
        records = []  # security 组同口径透传（seed-drill 重放，避免二次读文件）
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if rec.get("kind") == "seed-drill":
                    records.append(rec)
    return {"red": agg["red"], "denom": agg["denom"]}, records


def collect_false_decisions():
    """arbiter 误放行台账（ADR-0054 §7 落盘形态）。失败→(None, []) 护栏 pending。"""
    text = _raw_content(f"{ORG}/arbiter", "tests/false_decision_ledger.jsonl")
    if text is None:
        print("WARN false-decisions: arbiter 台账不可读——误放行护栏 pending（盲区上屏）")
        return None, []
    win = METRICS_POLICY["security"]["false_decision_window_days"]
    allow, deny, lines = false_decision_parse(text, NOW, win)
    return allow, lines


def _timeline(repo, number):
    """issue timeline 事件（分页）。失败→[]（该样本跳过，不造 0）。"""
    events, page = [], 1
    while True:
        batch = get(f"/repos/{ORG}/{repo}/issues/{number}/timeline?per_page=100&page={page}")
        events.extend(batch)
        if len(batch) < 100:
            return events
        page += 1


def collect_attention(cards):
    """签署耗时（type:intent timeline 差）+ needs-human 停留 + 当月 IR 数。"""
    durations, in_flight, ir_month = [], 0, 0
    intents, page = [], 1
    while True:
        batch = get(f"/repos/{ORG}/{HOME_REPO}/issues?labels=type:intent&state=all&per_page=100&page={page}")
        intents.extend(i for i in batch if "pull_request" not in i)
        if len(batch) < 100:
            break
        page += 1
    timelines = []
    for it in intents:
        c = _iso(it.get("created_at"))
        if c and c.year == NOW.year and c.month == NOW.month:
            ir_month += 1
        try:
            timelines.append(_timeline(HOME_REPO, it["number"]))
        except Infra as e:
            print(f"WARN attention: #{it['number']} timeline 失败 {e}——样本跳过")
    try:
        durations, in_flight = sign_durations(timelines)
    except Exception as e:  # 纯函数不该炸——防御面：注意力组降 pending
        print(f"WARN attention: 签署统计失败 {e}")
    dwell = []
    for c in cards:
        if c["state"] != "needs-human":
            continue
        try:
            h = dwell_hours(_timeline(c["repo"], c["number"]), NOW)
            if h is not None:
                dwell.append(h)
        except Infra as e:
            print(f"WARN attention: {c['repo']}#{c['number']} timeline 失败 {e}——样本跳过")
    return durations, in_flight, dwell, ir_month


def _billing_minutes():
    """当月 Actions 分钟（billing usage——cost-check 同端点）。失败→None。"""
    st, payload = _req(f"{GH_API}/orgs/{ORG}/settings/billing/usage?year={NOW.year}&month={NOW.month}")
    if st != 200:
        return None
    try:
        return int(sum(i["quantity"] for i in payload["usageItems"]
                       if i.get("product") == "actions" and i.get("unitType") == "Minutes"))
    except Exception:
        return None


def _metering_config():
    try:
        with open(os.path.join(DIR, "policy", "automation-limits.yaml"), encoding="utf-8") as f:
            m = yaml.safe_load(f)["cost"]["llm_tokens"]["metering"]
        return m["repo"], m["branch"], m["code_path"]
    except Exception:
        return None, None, None


def collect_cost(prev):
    """成本快照（Actions 分钟 + metering 归账 token）。TTL 内复用上一快照——
    15min 节奏每轮拉 tarball 是配额浪费（policy cost.snapshot_ttl_minutes）。"""
    ttl = METRICS_POLICY["cost"]["snapshot_ttl_minutes"]
    prev_ts = _ts(prev.get("cost_snapshot_ts"))
    prev_min, prev_tok = prev.get("actions_minutes_month"), prev.get("llm_tokens_month")
    if prev_ts and isinstance(prev_min, int) and isinstance(prev_tok, int):
        age = (NOW - prev_ts).total_seconds() / 60
        if 0 <= age < ttl:
            return prev_min, prev_tok, prev.get("cost_snapshot_ts")
    minutes = _billing_minutes()
    tokens = _metering_tokens()
    if minutes is None or tokens is None:
        print("WARN cost: billing/metering 采集失败——成本指标部分 pending（盲区上屏）")
        return minutes, tokens, (prev.get("cost_snapshot_ts") if minutes is None and tokens is None else NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return minutes, tokens, NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


def _metering_tokens():
    """CI-Workflows metering 归账（ADR-0062：先验链后归账；rc=2 无账本→0，
    rc=3 链断→None 不可信不入账，与 cost-check llm_channel 契约一致）。"""
    repo, branch, code = _metering_config()
    if not repo:
        print("WARN cost: automation-limits.yaml metering 定位缺失")
        return None
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        for fn in ("metering.py", "record.schema.json"):
            data = _raw_content(repo, f"{code}/{fn}")
            if data is None:
                print(f"WARN cost: 归账引擎 {fn} 拉取失败")
                return None
            with open(os.path.join(td, fn), "w", encoding="utf-8", newline="\n") as f:
                f.write(data)
        led = os.path.join(td, "ledger")
        os.makedirs(led)
        url = f"{GH_API}/repos/{repo}/tarball/{branch}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}",
                                                   "User-Agent": "dashboard-update"})
        try:
            import io
            import tarfile
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
                want = [m for m in tf.getmembers() if m.name.endswith(".jsonl")
                        and f"/{code}/records-" in f"/{m.name}"]
                for m in want:
                    m.name = os.path.basename(m.name)
                    tf.extract(m, led)
        except Exception as e:
            print(f"WARN cost: metering 账本拉取失败 {e}——token 指标 pending")
            return None
        if not os.path.isdir(td):
            return None
        since = NOW.strftime("%Y-%m-01")
        try:
            r = subprocess.run([sys.executable, os.path.join(td, "metering.py"), "aggregate",
                                "--dir", led, "--since", since, "--json"],
                               capture_output=True, text=True, timeout=180)
        except Exception as e:
            print(f"WARN cost: metering 归账执行失败 {e}")
            return None
        if r.returncode == 2:
            return 0  # 账本分支已建但无周片=零用量（ZERO 契约）
        if r.returncode != 0:
            print(f"WARN cost: metering 验链/归账失败 rc={r.returncode}——不可信不入账")
            return None
        try:
            return int(json.loads(r.stdout)["totals"]["total_tokens"])
        except Exception:
            return None


def collect_user_metrics():
    """各产品仓用户结果指标（读取位=policy user_results.read_path；缺失→None）。"""
    out = {}
    for p in METRICS_POLICY["user_results"]["products"]:
        text = _raw_content(f"{ORG}/{p['repo']}", METRICS_POLICY["user_results"]["read_path"])
        out[p["repo"]] = user_metric_from(text) if text else None
    return out


def build_payload(repos, cards, purl="", prev=None):
    """v2：v1 键全保留（cards/sli/sli_pending/sli_meta——agent 兼容）+ north_star/metrics。

    prev=上一轮 issue body 的 JSON（成本快照 TTL 复用 + 逃逸 sustained 无状态化——
    事件时戳直算双窗，ADR-0073 决策 1）。
    """
    prev = prev or {}
    rate, denom = sli_automerge(repos)
    zero_touch = sum(1 for n in merged_prs(repos, days=7)
                     if (n.get("mergedBy") or {}).get("login") == APP_BOT)
    esc = collect_escape(repos)
    drill_agg, drill_records = collect_drill()
    allow, fd_lines = collect_false_decisions()
    durations, in_flight, dwell, ir_month = collect_attention(cards)
    minutes, tokens, snap_ts = collect_cost(prev.get("metrics", {}).get("cost", {}))
    data = {
        "now": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zero_touch_merges_7d": zero_touch,
        "escape_rate_sustained": esc,
        "revert_rate": (None if esc is None else
                        {"num": esc["reverts_current"], "denom": denom}),
        "drill_red_rate": drill_agg,
        "false_allow": allow,
        "sign_durations_seconds": durations,
        "sign_in_flight": in_flight,
        "needs_human_dwell_hours": dwell,
        "false_decision_lines": fd_lines,
        "drill_records": drill_records,
        "actions_minutes_month": minutes,
        "llm_tokens_month": tokens,
        "ir_count_month": ir_month,
        "cost_snapshot_ts": snap_ts,
        "user_metric_files": collect_user_metrics(),
    }
    v2 = metrics_lib.build_payload(data, METRICS_POLICY)
    # SLI 块（#98 口径）：escape_rate v2 起有数（同北极星逃逸护栏分子，sli-report 口径）
    esc_rate = None
    if esc is not None and denom:
        esc_rate = round(esc["current"] / denom, 4)
    sli = {"automerge_rate": rate, "human_touch_per_pr": None, "escape_rate": esc_rate,
           "stuck_prs": sli_stuck(repos), "false_red_rate": None, "entropy_delta": None}
    pending = {"human_touch_per_pr": "W5-C3", "false_red_rate": "W5-C3", "entropy_delta": "W5-C3"}
    if rate is None:
        pending["automerge_rate"] = "N/A（近 7 天零 merged PR——分母陷阱 #98 T2，不造数）"
    if esc is None:
        pending["escape_rate"] = "N/A（逃逸采集失败——北极星护栏同步 pending，盲区已上屏）"
    elif esc_rate is None:
        pending["escape_rate"] = "N/A（近 7 天零 merged PR——分母陷阱 #98 T2，不造数）"
    payload = {
        "generated_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema": "dashboard-json v2（ADR-0055 v1 键兼容 + ADR-0073 north_star/metrics）",
        "project": {"title": "factory-floor", "url": purl},
        "cards": cards,
        "sli": sli,
        "sli_pending": pending,
        "sli_meta": {
            "automerge_rate": f"近7天 merged PR 中 merged_by=={APP_BOT} 占比（proxy，W5-C3 换 timeline 事件）",
            "automerge_denominator_7d": denom,
            "escape_rate": "（非演习 [auto-revert]+post-merge P0）/近7天 merged（ADR-0059 口径，v2 实算）",
            "stuck_prs": "open PR 停留>24h（active 仓求和）",
        },
        "north_star": v2["north_star"],
        "metrics": v2["metrics"],
    }
    return payload


def render_body(payload):
    """正文顶部=北极星对（AC-1 同屏）→ 状态一览 → 机器可读 JSON（宪法 §8 人 30 秒读懂）。"""
    cards = payload["cards"]
    by_state = {}
    for c in cards:
        by_state.setdefault(c["state"], []).append(c)
    state_lines = "\n".join(
        f"- {s}: {len(v)} 张（" + " ".join(f"[{c['repo']}#{c['number']}]({c['url']})" for c in v[:8])
        + ("…" if len(v) > 8 else "") + "）" for s, v in sorted(by_state.items())) or "- （队列空）"
    sli, meta = payload["sli"], payload["sli_meta"]
    rate_txt = f"{sli['automerge_rate']*100:.0f}%（分母 {meta['automerge_denominator_7d']}）" \
        if sli["automerge_rate"] is not None else "N/A（零分母）"
    human = f"""# 管家账本 dashboard（factory-floor 投影二，宪法 §12 / ADR-0055+0073）

{metrics_lib.render_brief({"north_star": payload["north_star"], "metrics": payload["metrics"]})}
## 状态一览

- 在制卡：**{len(cards)}** 张（active 仓 open+state:*）
{state_lines}
- factory-floor 板：{payload["project"]["url"] or "（board-sync 首轮后回填链接）"}
- SLI（#98 口径）：自动合并率 {rate_txt} · 逃逸率 {sli['escape_rate'] if sli['escape_rate'] is not None else 'N/A'} · 卡死 PR（>24h）{sli['stuck_prs']}
- 待补（W5-C3）：人类触碰/PR · 假红率 · 熵增——见 sli_pending 与 metrics 各 pending 字段
- 刷新节奏：butler-ledger 每 15min（唤醒矩阵行 2）；手动：workflow_dispatch board-sync

## 机器可读区（agent 一次读取全局；历史留痕=本 issue 编辑历史）

{JSON_MARK}
{FENCE}json
{json.dumps(payload, ensure_ascii=False, indent=2)}
{FENCE}
"""
    return human


def find_issue():
    """幂等查找账本 issue（ensure_issue 的查找半——main 需先读旧 body 取成本快照）。

    查找范围 state=all（含已关闭：账本被人工关闭后复用之）；同名无 `dashboard`
    label 的 issue 不接管（防 body 覆盖写进无关 issue，ADR-0055）。
    """
    page = 1
    while True:
        batch = get(f"/repos/{ORG}/{HOME_REPO}/issues?state=all&per_page=100&page={page}")
        found = next((i for i in batch
                      if "pull_request" not in i and i["title"] == ISSUE_TITLE
                      and LABEL["name"] in [l.get("name") for l in i.get("labels", [])]), None)
        if found or len(batch) < 100:
            return found
        page += 1


def ensure_issue(body):
    """幂等找到/创建账本 issue；返回 (number, created)。"""
    found = find_issue()
    if found:
        return found["number"], False
    # 幂等建 label（201=新建，422=已存在；其余=真故障——fail-closed 不静默）
    st, payload = _req(f"{GH_API}/repos/{ORG}/{HOME_REPO}/labels",
                       {"name": LABEL["name"], "color": LABEL["color"],
                        "description": LABEL["description"]}, "POST")
    if st not in (201, 422):
        raise Infra(f"POST labels HTTP {st}: {str(payload.get('message'))[:160]}")
    if DRY_RUN:
        print(f"[dry-run] 将创建 dashboard 账本 issue「{ISSUE_TITLE}」")
        return None, True
    issue = send("POST", f"/repos/{ORG}/{HOME_REPO}/issues",
                 {"title": ISSUE_TITLE, "body": body, "labels": [LABEL["name"]]})
    return issue["number"], True


def project_url():
    """只读取 factory-floor 项目链接（board-sync 已建；失败不阻塞账本——置空）。

    projectsV2 游标翻页（与 board-sync.ensure_project 同判据）：org 项目 >100 时
    目标不在首页，不翻页会把“存在”误判为“不存在”。
    """
    try:
        cur = None
        while True:
            st, payload = _req(f"{GH_API}/graphql", {
                "query": "query($o:String!,$cur:String){ organization(login:$o){"
                         " projectsV2(first:100, after:$cur){ nodes{ title url }"
                         " pageInfo{ hasNextPage endCursor } } } }",
                "variables": {"o": ORG, "cur": cur}}, "POST")
            if st != 200 or payload.get("errors"):
                break
            conn = payload["data"]["organization"]["projectsV2"]
            for p in conn["nodes"]:
                if p["title"] == "factory-floor":
                    return p["url"]
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cur = conn["pageInfo"]["endCursor"]
    except Exception:
        pass
    return ""


def _stable(body):
    """剥离每轮必变的时戳再比对（generated_at 精度到秒；snapshot_age_minutes 每 15min
    递增——不剥离则“内容相同跳过写”永不生效，每 15min 一条无意义编辑淹没 issue 历史；
    snapshot_ts 每小时快照刷新会真变更——保留，那是实质内容变化）。"""
    for key in ("generated_at", "snapshot_age_minutes"):
        body = re.sub(rf'"{key}":\s*"[^"]*"', f'"{key}":"-"', body)
        body = re.sub(rf'"{key}":\s*[0-9.]+', f'"{key}":0', body)
    return body.strip()


def _prev_payload(body_text):
    """旧 issue body → 上轮 JSON（成本快照复用源）。解析失败→{}（快照自然过期）。"""
    m = re.search(re.escape(JSON_MARK) + r".*?```+\s*json\s*\n(.*?)\n```+", body_text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {}


def main():
    if not TOKEN:
        print("FATAL 需要环境变量 GH_TOKEN=GOVERNANCE_TOKEN", file=sys.stderr)
        return 2
    stats = {"cards": 0, "issue": None, "created": 0, "edited": 0, "unchanged": 0,
             "guards_red": 0, "zeroed": 0}
    try:
        repos = active_repos()
        cards = scan_cards(repos)
        stats["cards"] = len(cards)
        prev, cur = {}, None
        found = find_issue()
        if found:
            cur = get(f"/repos/{ORG}/{HOME_REPO}/issues/{found['number']}")
            prev = _prev_payload(cur.get("body") or "")
        payload = build_payload(repos, cards, project_url(), prev)
        ns = payload["north_star"]
        stats["guards_red"] = len(ns["zero_touch_merges_7d"]["zeroed_reasons"])
        stats["zeroed"] = 1 if ns["interlocked_zeroed"] else 0
        body = render_body(payload)
        num, created = ensure_issue(body)
        stats["created"] = 1 if created else 0
        stats["issue"] = num
        if num is None:  # dry-run 新建路径
            print(f"AUDIT | butler=dashboard-update | trigger={TRIGGER} | outcome=ok | "
                  f"dry-run=1 | actions={json.dumps(stats, ensure_ascii=False)}")
            return 0
        if not created:
            if cur is None:  # find_issue 未命中但 ensure_issue 命中（并发创建）——重取
                cur = get(f"/repos/{ORG}/{HOME_REPO}/issues/{num}")
            if _stable(cur.get("body") or "") == _stable(body):
                stats["unchanged"] = 1
            elif DRY_RUN:
                print(f"[dry-run] 将编辑 issue #{num} body（{len(body)} 字节）")
                stats["edited"] = 1
            else:
                send("PATCH", f"/repos/{ORG}/{HOME_REPO}/issues/{num}", {"body": body})
                stats["edited"] = 1
    except Infra as e:
        print(f"AUDIT | butler=dashboard-update | trigger={TRIGGER} | outcome=infra-fail | "
              f"actions={json.dumps(stats, ensure_ascii=False)} | error={e}", flush=True)
        print(f"FATAL {e}", file=sys.stderr)
        return 2
    print(f"AUDIT | butler=dashboard-update | trigger={TRIGGER} | outcome=ok | "
          f"dry-run={1 if DRY_RUN else 0} | "
          f"actions={json.dumps(stats, ensure_ascii=False)}")
    print(f"issue: https://github.com/{ORG}/{HOME_REPO}/issues/{stats['issue']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
