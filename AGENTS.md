# AGENTS.md（索引型——只放不可推断的约束，细节按需读索引）

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
