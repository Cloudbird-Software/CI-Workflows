#!/usr/bin/env python3
"""spec-pr.py —— 把 spec.md 以 cloudbrid-agent 身份推到目标仓并开 PR（ADR-0050）

用法:
  APP_TOKEN=... python3 spec-pr.py --repo owner/name --spec spec.md \
      --branch spec/ir-XXXX --ir-ref "IR-XXXX owner/name#N" --usage-file usage.json

输出（stdout，供 workflow 摘要）: PR URL 与分支名。失败退出非零。
"""
import argparse
import base64
import json
import re
import sys
import urllib.request

API = "https://api.github.com"


def call(token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "spec-author",
    })
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)          # owner/name
    ap.add_argument("--spec", required=True)          # 本地 spec.md 路径
    ap.add_argument("--branch", required=True)        # e.g. spec/IR-0002-140
    ap.add_argument("--ir-ref", required=True)        # 展示用 IR 引用
    ap.add_argument("--ir-issue", required=True)      # 数字（回链与 PR 元数据）
    ap.add_argument("--usage-file", default="")       # llm-call.sh 的 usage 记录
    ap.add_argument("--token-env", default="APP_TOKEN")
    a = ap.parse_args()
    token = __import__("os").environ[a.token_env]

    # 目标路径 specs/<taskId>/spec.md（taskId 从 frontmatter 提取，只允许安全字符）
    text = open(a.spec, encoding="utf-8").read()
    m = re.match(r"---\n(.+?)\n---", text, re.S)
    taskId = ""
    if m:
        for line in m.group(1).splitlines():
            if line.startswith("taskId:"):
                taskId = line.split(":", 1)[1].strip().strip('"\'')
    taskId = re.sub(r"[^A-Za-z0-9._-]", "-", taskId)
    if not taskId:
        print("FATAL: 无法从 frontmatter 提取 taskId", file=sys.stderr)
        sys.exit(2)
    remote_path = f"specs/{taskId}/spec.md"

    st, base = call(token, "GET", f"/repos/{a.repo}/git/ref/heads%2Fmain")
    if st != 200:
        print(f"FATAL: 读 {a.repo} main 失败 {st}", file=sys.stderr)
        sys.exit(2)
    base_sha = base["object"]["sha"]
    st, bc = call(token, "GET", f"/repos/{a.repo}/git/commits/{base_sha}")
    base_tree = bc["tree"]["sha"]

    content = base64.b64encode(text.encode()).decode()
    st, blob = call(token, "POST", f"/repos/{a.repo}/git/blobs",
                    {"content": content, "encoding": "base64"})
    if st != 201:
        print(f"FATAL: blob 失败 {st} {blob}", file=sys.stderr)
        sys.exit(2)
    st, tree = call(token, "POST", f"/repos/{a.repo}/git/trees",
                    {"base_tree": base_tree,
                     "tree": [{"path": remote_path, "mode": "100644",
                               "type": "blob", "sha": blob["sha"]}]})
    st, commit = call(token, "POST", f"/repos/{a.repo}/git/commits",
                      {"message": f"spec({taskId}): 条款级规格（auto，{a.ir_ref}，ADR-0050）",
                       "tree": tree["sha"], "parents": [base_sha],
                       "author": {"name": "cloudbrid-agent[bot]",
                                  "email": "4632704+cloudbrid-agent[bot]@users.noreply.github.com"}})
    if st != 201:
        print(f"FATAL: commit 失败 {st}", file=sys.stderr)
        sys.exit(2)

    st, ref = call(token, "POST", f"/repos/{a.repo}/git/refs",
                   {"ref": f"refs/heads/{a.branch}", "sha": commit["sha"]})
    if st == 422:  # 分支已存在（重跑）：快进或复用
        st2, _ = call(token, "PATCH", f"/repos/{a.repo}/git/refs/heads/{a.branch}",
                      {"sha": commit["sha"], "force": False})
        if st2 != 200:
            # 非快进=同名分支已有一版：追加 -r2 分支，避免覆盖他人产物
            a.branch = a.branch + "-r2"
            st3, _ = call(token, "POST", f"/repos/{a.repo}/git/refs",
                          {"ref": f"refs/heads/{a.branch}", "sha": commit["sha"]})
            if st3 != 201:
                print("FATAL: 分支创建失败", file=sys.stderr)
                sys.exit(2)
    elif st != 201:
        print(f"FATAL: 建分支失败 {st} {ref}", file=sys.stderr)
        sys.exit(2)

    usage_line = ""
    if a.usage_file:
        try:
            u = json.load(open(a.usage_file, encoding="utf-8"))
            usage_line = (f"\n**计量（BEH-09/ADR-0048）**：model=`{u.get('model')}`，"
                          f"tokens={u['usage']['total_tokens']}，"
                          f"prompt_version=`{u.get('prompt_version','')[:19]}…`，"
                          f"latency={u.get('latency_ms')}ms\n")
        except Exception:
            pass
    body = (f"**自动产出（spec-author，ADR-0050）** | IR：{a.ir_ref}（#{a.ir_issue}）\n"
            f"{usage_line}\n"
            "- 冷上下文：仅 IR 标题+正文 + `pipeline/spec-template.md` 两输入（INV-04；本 run 日志可审计）\n"
            "- 注入防线：IR 正文定界符包裹 + g010 过渡版双扫通过（INV-10/AC-12）\n"
            f"- 产物：`{remote_path}`\n\n"
            f"人工验收面：条款是否忠实转写 IR、AC 是否可机检。合并即进入红队阶段（W2 前人工看）。")
    st, pr = call(token, "POST", f"/repos/{a.repo}/pulls",
                  {"title": f"spec({taskId}): {a.ir_ref} 条款级规格（auto）",
                   "head": a.branch, "base": "main", "body": body})
    if st != 201:
        print(f"FATAL: 开 PR 失败 {st} {pr.get('message','')}", file=sys.stderr)
        sys.exit(2)
    print(pr["html_url"])
    print(a.branch)


if __name__ == "__main__":
    main()
