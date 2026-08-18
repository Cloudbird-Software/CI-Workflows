# CI-Workflows

Cloudbird Software 组织的可复用工作流（唯一真相源）。业务仓通过 `uses: Cloudbird-Software/CI-Workflows/.github/workflows/<name>.yml@v1` 引用。

## 提供的工作流

| 工作流 | 用途 |
|---|---|
| `check.yml` | `make setup` + `make check`（lint + test），支持 node / python / go 运行时 |
| `hygiene.yml` | 大文件/凭据文件拦截 + gitleaks 全历史密钥扫描 + zizmor Actions 安全审计 |
| `dep-review.yml` | 依赖漏洞 + 许可证审查（拒绝 AGPL/GPL-3.0/SSPL） |
| `release.yml` | 构建 + SLSA 构建溯源 + GitHub Release 附件 |

## 版本策略

- 业务仓一律引用 `@v1` 大版本指针。
- 本仓发布：`git tag v1.0.0 && git push --tags`，然后 `git tag -f v1 v1.0.0 && git push -f origin v1` 移动指针。
- 破坏性变更递增大版本（v2、v3…），旧指针保留给存量仓库。

## 修改规则

改动本仓会影响**所有引用仓库**的 CI。修改前先在本仓 PR 验证，确认无误后再移动 `v1` 指针。
