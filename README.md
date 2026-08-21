# CI-Workflows

Cloudbird Software 组织的可复用工作流（唯一真相源）。业务仓通过 `uses: Cloudbird-Software/CI-Workflows/.github/workflows/<name>.yml@v1` 引用。

## 提供的工作流

可复用 workflow（业务仓 `uses:` 引用）：

| 工作流 | 用途 | timeout 上限 |
|---|---|---|
| `check.yml` | `make setup` + `make check`（lint + test，flaky-retry ≤N 入账），支持 node / python / go 运行时；含 `suppression-budget` 门（P2-2，ADR-0036）：抑制标记净增 ≤3/PR + 合入树总量棘轮 + ADR 逃生门（PR 事件判定，非 PR 事件 n/a-success） | 15min |
| `hygiene.yml` | 大文件/凭据文件拦截 + gitleaks 全历史密钥扫描 + zizmor Actions 安全审计 | 10min |
| `dep-review.yml` | 依赖漏洞 + 许可证审查（拒绝 AGPL/GPL-3.0/SSPL） | 10min |
| `contract.yml` | 契约兼容性检测门：OpenAPI（oasdiff）/ JSON Schema breaking + DB migration destructive DDL 分类（P2-4，ADR-0038） | 10min |
| `diff-coverage.yml` | diff coverage 门槛（ADR-0037）：本次 PR 变更行覆盖率 ≥ policy 阈值（非全局覆盖率） | 5min |
| `release.yml` | 构建 + SLSA 构建溯源 + GitHub Release 附件 | 20min |
| `test-integrity.yml` | P2-1 测试篡改检测（ADR-0035/.github #86）：TI-R1 测试文件删除 / TI-R2 断言净下降 / TI-R3 新增抑制标记 / TI-R4 期望值改写 → 红；PR 引用 ADR 可豁免（计数入账） | 5min |

本仓自有 workflow（不可复用，仅本仓 CI）：

| 工作流 | 用途 | timeout 上限 |
|---|---|---|
| `ci.yml` | 本仓 PR/push 门禁（hygiene + T8 fixture 自检 + gate 聚合） | 10min / 5min |
| `scorecard.yml` | 每周一 05:30 OSSF Scorecard 安全评分 + SARIF 上传 | 10min |

所有 job 声明 `timeout-minutes` 熔断上限（testing.yaml "gate<5min" 原则的上界表达）：卡死的 job 不再无限占用 runner、不再阻塞合并。

## 契约兼容性检测门（contract.yml，P2-4 / ADR-0038）

业务仓 `ci.yml` 挂 `contract` job 并加入 gate `needs`（无事件条件，PR/push 两面都真跑）：

```yaml
  contract:
    uses: Cloudbird-Software/CI-Workflows/.github/workflows/contract.yml@<sha> # v1
    with:
      ciw-ref: <sha>   # 与 uses: 钉扎同 SHA（被调用侧无法自取自身 ref，两处一致）
  gate:
    needs: [..., contract]
```

- **policy SoT**：`.github` 仓 `governance/policy/contracts.yaml`（各仓契约 kind+glob、迁移目录与工具、ADR/豁免要求）。org policy 未合入时回退引擎内置 bootstrap 快照（`scripts/contract/policy-bundled.yaml`，大声 WARN；`scripts/` 已纳入本仓 adr-required C1 路径）。
- **检测面**：OpenAPI → `oasdiff breaking --fail-on WARN`（1.29.1 + tarball sha256 双锚定）；JSON Schema → 内置结构化 breaking 分类器；DB migration → alembic（`op.*` + `op.execute` 内嵌 SQL）/裸 SQL 双前端 DDL 分类器。proto 未实装——policy 声明即报错（组织无 proto，ADR-0038）。
- **destructive 手续**：destructive DDL（DROP/ALTER TYPE/无默认值 NOT NULL 加列等，清单见引擎）须 PR 引用真实存在的 ADR + alembic `downgrade()` 含逆操作才放行。
- **失明防护**：policy 声明路径在 HEAD 必须命中文件，否则红（防契约文件被移走后检测器失明，卡内 T6）。
- **N/A 显式**：未声明契约面的仓真跑并输出 N/A（非 skipped，ADR-0032 范式）。
- **自测**：本仓 `ci.yml` 的 `contract-selftest` job 跑 `contract_check.py --mode selftest`（卡内 T1-T7 全套，fixture 在 `scripts/contract/tests/`）。

## 权限模型（红队 #4 P1-3 审计结论）

可复用 workflow（check / hygiene / dep-review / release）顶层 `permissions: {}`（零权限默认），job 级显式声明最小必要权限。本仓自有 workflow 例外：`ci.yml` 顶层 `contents: read`（job 级继承后按需收敛），`scorecard.yml` 顶层 `read-all`（Scorecard 评分需仓库元数据）+ job 级收敛为 `security-events/id-token: write` + `contents/actions: read`：

| workflow | 实际权限 | 理由 |
|---|---|---|
| check / hygiene | 顶层 `{}`，job 级 `contents: read` | checkout 只读 |
| dep-review | 顶层 `{}`，job 级 `contents: read` + `pull-requests: write` | `comment-summary-in-pr` 失败摘要评论 + 评审写操作（见下） |
| diff-coverage | 顶层 `{}`，job 级 `contents: read` | checkout caller 全历史求 merge-base + GITHUB_TOKEN 读公开仓 .github 的 policy（ADR-0020/0021 先例）；不注入 org secret |
| release | 顶层 `{}`，job 级 `contents/id-token/attestations: write` | 写 Release 附件 + SLSA 溯源签名；受 `production` environment 人工审批门（RL-1） |
| ci.yml | 顶层 `contents: read` | 本仓 checkout 只读 |
| scorecard.yml | 顶层 `read-all`，job 级 `security-events/id-token: write` + `contents/actions: read` | 评分需元数据读 + SARIF 上传 |

**`pull-requests: write` 的完整语义（评审勘误）**：该权限按 GitHub 文档覆盖 PR 评审写操作——含提交 `APPROVE` / `REQUEST_CHANGES` 事件，并非仅评论/标签。当前拦截 Actions 身份批准 PR 的是 org 级 API-only 设置 `can_approve_pull_request_reviews=false`（expected-state.json#actions_policy 固定）——workflow YAML 的 `permissions:` 块**无法表达也无法覆盖**该设置。即：令牌权限本身含审批路径（记录在案、纳入 `pull-requests: write` 发放审计），实际能否落地审批由 org 设置封堵，且 org 设置变更会被每日 drift-check 检出。

## diff coverage 门槛（ADR-0037，P2-3）

**口径**：本次 PR 变更行（`git diff -M -U0 $(merge-base) HEAD` 的新增行，含修改行，删除行不计）中被测试执行到的比例 ≥ 阈值——**非全局覆盖率**（全局口径按 .github `governance/policy/testing.yaml` X-01 继续拒绝；大 PR 稀释挡不住"顺手加 200 行无测试代码"，.github#88 T3 稀释攻击负向测试）。

**阈值/豁免真源**：`.github` 仓 `governance/policy/testing.yaml` 的 `diff_coverage:` 段（缺省 80%，边界语义 **≥80.0 绿 / 79.9 红**）。按仓覆盖须登记 `repo_overrides`，caller input 显式声明的阈值与登记不符即红。豁免清单（文档/配置/生成代码）同在 policy——C1 路径（ADR+owner review）保护，**业务仓 PR 作者无权扩大豁免**；豁免变更走 ADR。

**fail-closed**：非豁免变更行存在但覆盖率数据缺失/不可解析、policy 拉取失败、阈值与登记不符——一律红（"没测过"≠"测了没覆盖"）。

**门禁三要素防削弱**（#81 §3.3）：workflow 本体（caller 钉 ref 引用）、执法工具（从 workflow 同 ref checkout 本仓 `scripts/diff-coverage.py`，不取 caller 仓内副本）、阈值与豁免（读 .github main policy）——均来自被审 PR 改不动的位置。工具自带 `--self-test`（#88 T6 预标注 fixture，`scripts/diff-coverage-fixtures/` 四组：lcov 等值边界 / istanbul 稀释攻击 / go 低于阈值+豁免 / cobertura+按仓覆盖），**每次执法运行前置执行**。

**业务仓接入**（随 P2-1/P2-2 批次，caller 侧两步）：

1. `make test` 产出四格式之一（工具 `--format auto` 自动嗅探）：
   - node：vitest `--coverage`（`coverage/lcov.info` 现成）；
   - python：Makefile 补 `--cov-report=xml`（`coverage.xml`，Cobertura）；
   - go：`go test ./... -coverprofile=coverage.out`；
   覆盖率文件经 `check.yml` 既有 `Upload reports` 工件（`reports-<runs-on>`，含 `coverage/`）透传。
2. `ci.yml` 增 job 并入 gate needs 链：

   ```yaml
   diff-coverage:
     uses: Cloudbird-Software/CI-Workflows/.github/workflows/diff-coverage.yml@<与 check.yml 相同的钉住 ref>
     with:
       coverage-artifact: reports-ubuntu-latest   # 与 check.yml 的 runs-on 对应
       # coverage-format: lcov | istanbul | cobertura | go | auto（缺省 auto 嗅探）
       # threshold: "90"   # 按仓覆盖——须先在 .github policy repo_overrides 登记，否则红

   gate:
     needs: [hygiene, check, diff-coverage, deps, deps-audit, adr-required]
     # push 事件按 ADR-0032（required-check-chains.md 规则 2）在 EXPECTED_SKIP
     # 登记 diff-coverage（该 job 仅 PR 事件执法——事件互补结构性跳过）。
   ```

## test-integrity 测试篡改检测门（ADR-0035 / .github #86，P2-1）

业务仓 caller workflow 把本门纳入 gate 的 needs 链（hygiene/check 同款钉 ref）：

```yaml
  test-integrity:
    uses: Cloudbird-Software/CI-Workflows/.github/workflows/test-integrity.yml@<钉住 ref>
  gate:
    if: always()
    needs: [hygiene, check, test-integrity, ...]
```

- 规则/阈值声明在 `Cloudbird-Software/.github` 的 `governance/policy/testing.yaml#test_integrity`
  （inputs `policy-repo`/`policy-ref` 可覆盖）；拉取失败即红（fail-closed），缺节用检测器
  内置同值缺省。
- **非 PR 事件（push）本门为 n/a-success**（无 base...head 的 PR diff 语义），调用方 gate
  无需为它登记 ADR-0032 的 EXPECTED_SKIP（与 diff-coverage 的 job 级 if + EXPECTED_SKIP
  登记方案二选一，本门选 n/a-success 以减少 caller 配置面）。
- 命中规则的 PR 想放行：title/body 引用 ADR-NNNN（须存在于 agent-registry/decisions，防
  幽灵 ADR）→ 豁免但计数入账（job log `TI-COUNT escape_hatch_waived=` + step summary）。
- 执法工具从本 workflow 同 ref checkout（`github.workflow_ref` 解析，不取 caller 仓内
  副本）；每次执法前前置跑 T8 fixture 自检（15 case 预标注全比对，`scripts/
  test-integrity-fixtures/`）。检测器/模式变更也经本仓 ci.yml 的
  `test-integrity-selftest` job 在本仓 PR 上先行回归。

## 已知风险与缓解（红队 #4 P1-4/P1-5 复核）

- **单点引用**：全部业务仓 gate 引用本仓 `@v1`——本仓 main 受 org ruleset 保护（BP-1/BP-2：PR+squash、gate required、owner-only review），误删/归档走 GitHub 90 天恢复窗口；不设镜像仓（双维护成本>收益，防线已由 ruleset+review 承担）。
- **`Cloudbird-Software/*` actions 白名单通配**：通配=信任组织内全部自有 action；本仓 workflow 变更属 C1 治理路径（GOVERNANCE flows.governance_change，owner-only review）。对高敏感业务仓，可改 pin commit sha 引用（`uses: Cloudbird-Software/CI-Workflows/.github/workflows/check.yml@<sha>`）换取不可变性、放弃自动跟随——按仓风险自选。
- **verifier 判卷（AR-9）状态**：注册层已声明（agent-registry standards/checks.yaml：`test-tree-freeze` active——test-author 冻结测试树）；产品仓侧的 `mechanism:verifier` 判卷 workflow 尚未实装，属 ADR-0010 二期（与 `pr-identity-path-matrix` 同批，见 checks.yaml planned 项）。首个产品仓接入时实装——当前无业务仓消费，提前实装无消费方可验证。
- **dependabot automerge（SC-3）**：判定逻辑在 template-service 仓（automerge workflow）；依赖审批 approver/SLA 已定义于 .github governance/policy/languages.yaml#dependency_policy（owner 审批，7 天 SLA）。

## 版本策略

- 业务仓一律引用 `@v1` 大版本指针。
- 发布流程（红队 #6-A 加固，ADR-0016 决策 3）：
  1. 合并变更 PR（gate + owner review）；
  2. 打具体版本 tag：`git tag v1.X.Y <审阅过的合并SHA> && git push origin v1.X.Y`；
  3. 移动指针：`git tag -f v1 v1.X.Y && git push -f origin v1`；
  4. 复核不变式：`git ls-remote origin refs/tags/v1 refs/tags/v1.X.Y` 两行指向**同一 commit** 才算发布完成。
- **可检测不变式**：`v1` 恒指向最高的 `v1.x.y` tag 的 commit。`.github` 治理仓 drift-check §11 每日校验此不变式——admin 经 release-tags ruleset bypass 强移 `v1`（或 `v1` 与最高 `v1.x.y` 脱钩）= 24h 内漂移报警，指针投毒不再是无痕通道。
- 破坏性变更递增大版本（v2、v3…），旧指针保留给存量仓库；更高大版本指针遵循同一不变式（`vN` == 最高 `vN.x.y`）。

## 修改规则

改动本仓会影响**所有引用仓库**的 CI。修改前先在本仓 PR 验证，确认无误后再移动 `v1` 指针。本仓变更属 C1 治理路径：PR 须引用 ADR（gate adr-required 检查）。

