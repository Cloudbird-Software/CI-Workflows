# W5-C2 端到端实跑 — 验收证据清单

> 卡片：`.github#287` · IR：`Cloudbird-Software/.github#263` · Spec v5
> AC 目标：全程时间线 + check run 链接链 + 一条真实 `verdict=insufficient` 阻断记录 + 人类签收抽检记录（AC-20）。

## 0. 当前执行模式声明

**本地机械模拟**（`e2e-runner.py`），原因见 `e2e-test-plan.md` §0.2：
- 验证者 APP 未创建（`verifier_app.id=null`）→ holdout 身份面无法落位；
- cloudbrid-agent App 无 `workflows` 权限 → `adversary.yml`/`adversary-gate.yml`/`conductor.yml` 无法上游推送；
- LLM endpoint 凭据不可用于本 runner → 用 W5-C1 golden set `native` 模式（确定性、免 LLM）+ 故意极差 spec fixture 触发 `insufficient`。

待 §5 前置条件齐备后，用**同一套 fixture** 触发真实 GitHub Actions 全流程并回填 §3 证据。

## 1. 本地模拟证据（已完成）

### 1.1 状态机模拟结果（7/7 场景通过）

```
$ python e2e-runner.py
[PASS] S1: 故意极差 spec 触发真实 insufficient (Veto → needs-human)  final_state=needs-human
[PASS] S2: 修复后 spec 审计 → survived → wave-planned                final_state=wave-planned
[PASS] S3: 完整闭环: Veto → 修复 → survived → wave-planned → 认领 → 合并  final_state=done
[PASS] S4: 负向对照: 无 Veto 全程不算数 (AC-2)                       final_state=ir-signed
[PASS] S5: suite 未就绪 → T5 拒转 (AC-12)                           final_state=spec
[PASS] S6: needs-human 不可直跳 wave-planned (AC-12)                final_state=needs-human
[PASS] S7: 跨卡三元组 → T6 拒转 (AC-12)                             final_state=redteam
全部 7 个场景通过 ✓
```

### 1.2 关键验证点证据

| 验证点 | 场景 | 结果 | 证据位置 |
|--------|------|------|----------|
| AC-4: 故意极差 spec 触发真实 insufficient | S1 | ✅ | `fixtures/e2e/deliberately-bad-spec/adversary-scores.json` (全零聚合分) |
| AC-14: Veto → needs-human 且无法进 wave-planned | S1, S6 | ✅ | S1 Phase A + S6 显式断言 |
| AC-12: T5 suite-ready 谓词（确定性重断言） | S2, S5 | ✅ | S5 反向：缺 suite/ → DENIED-suite-not-ready |
| AC-12: T6 三元组 survived 谓词（本卡本 specVersion） | S2, S3, S7 | ✅ | S7 反向：跨卡记录不计入 → DENIED-triplet |
| AC-20: Veto 理由 ↔ 修复 diff 机械闭环 | S3 | ✅ | `verify_veto_fix_loop()` 5/5 criteria 核对通过 |
| AC-2: 无 Veto 全程不算数（负向硬谓词） | S4 | ✅ | 显式标记「不算数」 |
| AC-3: PR 绑定卡测试通过 | S3 | ✅ | `_run_suite(fixed-spec)` 4/4 真实断言通过 |
| AC-8: golden known-bad 样本构造独立于判定脚本 | 全局 | ✅ | `adversary-scores.json` 纯数值合成，与 W5-C1 样本同构 |

## 2. 真实 GitHub Actions 全流程证据（待 §5 前置回填）

> 以下为真实执行时的证据模板；本地模拟阶段以模拟证据占位。

### 2.1 全程时间线（issue #287 事件序列）

| 时间 (UTC) | 事件 | 状态转移 | check run / 证据链接 |
|------------|------|----------|----------------------|
| (待回填) | 父意图签署 `label:state:id-signed` | ir-draft → spec | conductor run: (link) |
| (待回填) | spec PR 打开（含 suite/ + adversary check） | — | spec PR #: (link) |
| (待回填) | adversary 审计故意极差 spec → `insufficient` | spec → needs-human | adversary run: (link) |
| (待回填) | 修复 PR（回应 Veto 理由）合入 | needs-human → redteam | fix PR #: (link) |
| (待回填) | adversary 审计修复后 spec → `survived` | redteam → wave-planned | adversary run: (link) |
| (待回填) | /claim 认领 | wave-planned → ready → in-progress | (link) |
| (待回填) | 实现 PR（绑定卡测试 + holdout）合入 | in-progress → done | impl PR #: (link) |

### 2.2 Check Run 链接链

- [ ] conductor run（T1 ir-signed→spec）: (待回填)
- [ ] adversary run #1（insufficient / Veto）: (待回填)
- [ ] adversary run #2（survived）: (待回填)
- [ ] holdout 校验 check: (待回填)
- [ ] 卡绑定测试 check: (待回填)
- [ ] expected-skip check（纯开发路径 PR）: (待回填)

### 2.3 真实 insufficient 阻断记录（AC-20 负向事件硬谓词）

> **必须存在至少一条**真实 `verdict=insufficient` 阻断，否则全程不算数（AC-2）。

- [ ] adversary run 产出 `verdict=insufficient`：(待回填 run link)
- [ ] check run conclusion = `failure`（`check_run_writeback.py`）: (待回填)
- [ ] Veto 理由逐 criterion：(待回填，引用 `adversary-report.json`)
- [ ] 修复 diff ↔ Veto 理由机械核对通过：(待回填)

### 2.4 Veto 理由 ↔ 修复 diff 闭环证据

- [ ] `verify_veto_fix_loop()` 5/5 criteria 机械核对日志：(待回填)
- [ ] 修复后 spec 审计 `verdict=survived`：(待回填)

## 3. 人类签收抽检记录（DECISION-05 第三层）

| 抽检项 | 抽检方式 | 结果 | 抽检人 / 时间 |
|--------|----------|------|---------------|
| 时间线完整性 | 字符串级核对事件序列 vs check run | (待签收) | (待回填) |
| insufficient 阻断真实性 | 独立读取 adversary run 日志，确认非 verifier 自报 | (待签收) | (待回填) |
| Veto↔修复闭环 | 独立重跑 `verify_veto_fix_loop()` | (待签收) | (待回填) |
| check run 链接链 | 逐链接打开确认 conclusion 与报告一致 | (待签收) | (待回填) |

本地模拟阶段抽检占位：
- ✅ 状态机逻辑抽检通过（7/7 场景，含 S4 负向硬谓词、S6 needs-human 不可直跳）
- ⏳ 真实 GitHub Actions 抽检：待 §5 前置齐备后执行

## 4. 本地模拟阶段自检清单

- [x] `e2e-test-plan.md` 覆盖 AC-20/AC-4/AC-14/AC-12/AC-2/AC-3/AC-8
- [x] `e2e-runner.py` 七场景矩阵（含反向断言 S4-S7）全部通过
- [x] 故意极差 spec fixture 构造独立于判定脚本（纯数值合成）
- [x] Veto 理由 ↔ 修复 diff 机械闭环（`veto-fix-loop.json` + `verify_veto_fix_loop()`）
- [x] golden known-bad 样本与 W5-C1 `golden-known-bad-01` 同构
- [x] suite 测试含真实 AC 语义断言（`test_ac_traceability.py`）
- [x] 子代理协作文件未冲突（本卡仅新增 `w5-c2/` 目录 + CI-Workflows PR 新分支）

## 5. 真正端到端实跑所需权限 / 前置（阻塞项）

| # | 前置条件 | 状态 | 所需动作 |
|---|----------|------|----------|
| 1 | cloudbrid-agent App 获得 `workflows` 权限 | ❌ 未就绪 | org admin 授予 workflows 写权 |
| 2 | 推送 `adversary.yml` / `adversary-gate.yml` / `conductor.yml` | ❌ 待权限 | 人类维护员用有 workflows 权限的 PAT/App 推送 |
| 3 | `main-protection.json` 新增 `adversary` required check（PR #309） | 🔜 open | 待 adversary-gate 正常运行后合并 |
| 4 | `org-required-workflows.json` 新增 `adversary-gate.yml`（PR #310） | 🔜 open | 同上 |
| 5 | 验证者 APP 创建并登记到 `expected-state.json` | ❌ 未就绪 | 创建 verifier App + 登记 ID |
| 6 | LLM provider endpoint + `LLM_API_KEY` org secret | ⚠️ 未配置 | 注入 org secret |

> 部署顺序约束（PR #309/#310 描述已警告）：① 推送 workflow 文件 → ② 确认 adversary-gate 正常运行 → ③ 合并 PR #309/#310。逆序将导致全组织 PR 被阻断。
