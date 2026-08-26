# AGENTS.md（索引型——只放不可推断的约束，细节按需读索引）

<!-- entry-protocol v2 -->

### 入口协议（陌生 agent 从这里开始——宪法 §11 / ADR-0055/0095）

0. **按意图定角色**（指引=.github 仓 `docs/agent/ROLE-*.md`，ADR-0095）：开新意图→ROLE-IR · 把已签署 IR 写成 spec→ROLE-SPEC · 实现卡片→ROLE-IMPLEMENT · 验收/人类让你处理 issues→ROLE-ACCEPT
1. 取 ghcb（钉 SHA，禁浮动 main）：`curl -fsS -o ghcb https://raw.githubusercontent.com/Cloudbird-Software/.github/f72d9520706c8fca974d92456f65cae5c1412bb7/scripts/ghcb && chmod +x ghcb`（凭据用你自己的：`gh auth login` 或 `export GH_TOKEN=<PAT>`；`-f` 必带——404 时 curl 无 -f 仍退出 0，会把错误页当脚本落盘）
2. 找活：`bash ghcb next [owner/repo]` → 列 state:ready 卡（卡 issue 是唯一工作凭证，无卡不开工）
3. 认领：`bash ghcb claim <n> [owner/repo]` → 评论 /claim——conductor 转介 arbiter 原子 CAS 租约，先到先得；败者换下一张（`bash ghcb status <n>` 看持有者）
4. 开工：`make card-test CARD=<n>`（读卡 AC、测试先行）→ `make gates-pr`（本地复现 CI 关卡）
5. 提 PR：body 必带一行卡元数据 `Card: <owner>/<repo>#<n>`（`bash ghcb card-meta <n>` 生成；缺失=后续关卡 exit 3）
6. front-desk 命令（卡 issue 评论，conductor 转介 arbiter 处理）：/claim 认领 · /release 释放租约 · /retry 隔离回流

<!-- /entry-protocol -->

## 角色路由（按你的意图选路——ADR-0095；指引文件在 .github 治理仓 docs/agent/）

- 开 IR：feature 意图=本仓 issue（issue 即 IR，无需 PR）；治理意图=.github 仓 → [ROLE-IR.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-IR.md)
- IR→spec：spec PR 必带测试设计逐类讨论（差分/属性/模糊…）+ holdout；**spec agent 不得直接实现** → [ROLE-SPEC.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-SPEC.md)
- 实现卡片（PM 职责）：弱模型优先（子 agent / CNB 池）· fan-out=工具非流程 · 边做边推 PR · 3 次熔断自己接手 → [ROLE-IMPLEMENT.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-IMPLEMENT.md)
- 验收 / 人类让你处理 issues：卡/IR 完成度检查 · bug 复现三值判定 → [ROLE-ACCEPT.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-ACCEPT.md)

## 命令

- `make gates-pr` 提交前必跑：bash -n + py_compile + yaml 解析 + test-integrity/suppression 自测——ci.yml 的本地可等价部分，CI 关卡语义仍以 `.github/workflows/ci.yml` 为准
- `make card-test CARD=<n>` 读工作卡 AC（测试先行）

## 硬规则（违反 = PR 打回）

1. 认证：一切 push/PR 用 cloudbrid-agent App 令牌，禁个人 PAT。获取（脚本 pin 到已审阅提交，升级先比对 .github main 再换 SHA——禁 `curl|bash` 浮动 main 指针，ADR-0021）：
   `GH_TOKEN=$(REPO=CI-Workflows bash <(curl -sS https://raw.githubusercontent.com/Cloudbird-Software/.github/f72d9520706c8fca974d92456f65cae5c1412bb7/scripts/gh-app-token.sh))`
2. 不改 `.github/workflows/**`（App 无 Workflows 权限，人类专属）；本仓是全组织 CI 门禁唯一实现，改门禁语义属治理变更——走 C1 runbook（.github 仓 docs/pm/PLAYBOOK.md）并在 PR body 引用 ADR-NNNN
3. 新依赖先报"名称/用途/许可证/标准库可否替代"等人批；禁 AGPL/GPL-3.0/SSPL
4. 密钥、客户名、连接串不进仓库；一个 PR 一件事，diff < 400 行；bug 修复先写复现失败测试
5. workflow inputs/outputs 属对外契约：变更必须在 PR body 写明并给出业务仓同步方案；提交信息用 Conventional Commits

## 索引（用到再读，不要全读）

| 场景 | 读这个 |
| --- | --- |
| 业务仓怎么挂 CI / 有哪些可复用工作流 | [README.md](README.md) 工作流总表（`uses: Cloudbird-Software/CI-Workflows/.github/workflows/<name>.yml@v1`，ref 钉 SHA） |
| 改某条管道（metering / patrol / trust-gate / holdout-unseal / …） | `pipeline/<名>/` 内规格与脚本 |
| 改策略（依赖供给链 / 抑制预算） | `policy/*.yaml` |
| 改门禁脚本（test-integrity / suppression-budget / diff-coverage / …） | `scripts/`（自测 fixture 就近放 `scripts/*-fixtures/`） |
| 选语言 / 选库 / 测试政策 / 治理总清单 | 组织 [.github 仓 governance/](https://github.com/Cloudbird-Software/.github/tree/main/governance) |
