# CI-Workflows

Cloudbird Software 组织的可复用工作流（唯一真相源）。业务仓通过 `uses: Cloudbird-Software/CI-Workflows/.github/workflows/<name>.yml@v1` 引用。

## 提供的工作流

| 工作流 | 用途 | timeout 上限 |
|---|---|---|
| `check.yml` | `make setup` + `make check`（lint + test），支持 node / python / go 运行时 | 15min |
| `hygiene.yml` | 大文件/凭据文件拦截 + gitleaks 全历史密钥扫描 + zizmor Actions 安全审计 | 10min |
| `dep-review.yml` | 依赖漏洞 + 许可证审查（拒绝 AGPL/GPL-3.0/SSPL） | 10min |
| `release.yml` | 构建 + SLSA 构建溯源 + GitHub Release 附件 | 20min |

所有 job 声明 `timeout-minutes` 熔断上限（testing.yaml "gate<5min" 原则的上界表达）：卡死的 job 不再无限占用 runner、不再阻塞合并。

## 权限模型（红队 #4 P1-3 审计结论）

每个 workflow 顶层 `permissions: {}`（零权限默认），job 级显式声明最小必要权限：

| workflow | 显式权限 | 理由 |
|---|---|---|
| check / hygiene | `contents: read` | checkout 只读 |
| dep-review | `contents: read` + `pull-requests: write` | 仅 `comment-summary-in-pr`（失败时摘要评论） |
| release | `contents/id-token/attestations: write` | 写 Release 附件 + SLSA 溯源签名；受 `production` environment 人工审批门（RL-1） |
| scorecard | 上传 SARIF 所需最小集 | 周扫公开仓安全评分 |

`can_approve_pull_request_reviews` 是 org 级 API-only 设置（expected-state.json#actions_policy 固定 `false`）——workflow YAML 的 `permissions:` 块**无法表达也无法覆盖**该设置；本仓无任何 workflow 具备批准 PR 的权限路径。`pull-requests: write` ≠ 审批权（仅评论/标签类写操作）。

## 已知风险与缓解（红队 #4 P1-4/P1-5 复核）

- **单点引用**：全部业务仓 gate 引用本仓 `@v1`——本仓 main 受 org ruleset 保护（BP-1/BP-2：PR+squash、gate required、owner-only review），误删/归档走 GitHub 90 天恢复窗口；不设镜像仓（双维护成本>收益，防线已由 ruleset+review 承担）。
- **`Cloudbird-Software/*` actions 白名单通配**：通配=信任组织内全部自有 action；本仓 workflow 变更属 C1 治理路径（GOVERNANCE flows.governance_change，owner-only review）。对高敏感业务仓，可改 pin commit sha 引用（`uses: Cloudbird-Software/CI-Workflows/.github/workflows/check.yml@<sha>`）换取不可变性、放弃自动跟随——按仓风险自选。
- **verifier 判卷（AR-9）状态**：注册层已声明（agent-registry standards/checks.yaml：`test-tree-freeze` active——test-author 冻结测试树）；产品仓侧的 `mechanism:verifier` 判卷 workflow 尚未实装，属 ADR-0010 二期（与 `pr-identity-path-matrix` 同批，见 checks.yaml planned 项）。首个产品仓接入时实装——当前无业务仓消费，提前实装无消费方可验证。
- **dependabot automerge（SC-3）**：判定逻辑在 template-service 仓（automerge workflow）；依赖审批 approver/SLA 已定义于 .github governance/policy/languages.yaml#dependency_policy（owner 审批，7 天 SLA）。

## 版本策略

- 业务仓一律引用 `@v1` 大版本指针。
- 本仓发布：`git tag v1.0.0 && git push --tags`，然后 `git tag -f v1 v1.0.0 && git push -f origin v1` 移动指针。
- 破坏性变更递增大版本（v2、v3…），旧指针保留给存量仓库。

## 修改规则

改动本仓会影响**所有引用仓库**的 CI。修改前先在本仓 PR 验证，确认无误后再移动 `v1` 指针。本仓变更属 C1 治理路径：PR 须引用 ADR（gate adr-required 检查）。
