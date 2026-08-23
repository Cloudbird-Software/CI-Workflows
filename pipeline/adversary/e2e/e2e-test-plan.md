# W5-C2 端到端实跑测试计划

> 卡片：`.github#287` — W5-C2: 端到端实跑（真实卡全程 + 一次真实 Veto + 人类签收抽检）
> Spec：`specs/ISSUE-263/spec.md` v5 · IR 父意图：`Cloudbird-Software/.github#263`
> 前置：W4-C3（adverse check 纳入 required checks）、W5-C1（golden set 标定 + holdout 注册）已完成。

## 0. 范围与约束

### 0.1 目标
一张真实卡走通完整生命周期：

```
ir-signed → spec → redteam → (真实 Veto → 修复 → survived) → wave-planned → 认领 → PR 绑定卡测试 → 合并
```

并满足 AC-20 的**负向事件硬谓词**：全程必须发生至少一次真实 `verdict=insufficient` 阻断；无 Veto 的「全程」不算数。

### 0.2 当前硬性限制（决定本计划以本地模拟为主）

| 限制 | 影响 | 应对 |
|------|------|------|
| 验证者 APP 尚未创建（`expected-state.json` 中 `verifier_app.id=null`） | holdout 注册/校验的身份面无法真正在 GitHub 上落位 | 代码先实现（W2-C4 已做），运行时降级留痕；本地模拟身份面 |
| cloudbrid-agent App 无 `workflows` 权限 | `adversary.yml` / `adversary-gate.yml` / `conductor.yml` 无法上游推送 | 端到端实跑**无法真正触发 GitHub Actions 全流程**；本地模拟状态机 + 判定逻辑 |
| LLM 凭据 / endpoint 不可用于本 runner | 无法真正在 CI 内运行 adversary 红队 | 用 W5-C1 golden set 的 `native` 模式（确定性、免 LLM）+ 故意极差 spec fixture 触发 insufficient |

**结论**：本计划产出一套**本地可执行的端到端模拟**（`e2e-runner.py`），机械验证状态流转、guard、deterministic predicates、Veto→修复→survived 闭环；并产出**验证证据清单模板**（`verification-checklist.md`），待 workflows 权限 + 验证者 APP 到位后，用同一套 fixture 触发真实 GitHub Actions 全流程并回填证据。

### 0.3 验收规范映射

| AC | 本计划覆盖方式 |
|----|----------------|
| AC-20 | 全程时间线 + check run 链接链 + 一条真实 insufficient 阻断 + 人类抽检记录 |
| AC-4 | 故意极差 spec 触发真实 insufficient 的端到端记录 |
| AC-14 | Veto 后 state=needs-human 且无法进入 wave-planned；修复后经 survived 才放行 |
| AC-12 | T5/T6 转移：suite-ready 谓词 + 三元组 survived 记录；needs-human 不可直跳 wave-planned |
| AC-2 | 无真实 Veto 的「全程」不算数（负向事件硬谓词）——本计划显式构造 Veto 并断言其发生 |
| AC-8 | 故意极差 spec 基于 W5-C1 golden known-bad 样本构造（独立于判定脚本） |

---

## 1. 端到端阶段与验证点

### 阶段 0：准备（test fixture 就位）
- **验证点 0.1**：`transitions.yaml` 可解析，状态集与转移表完整。
- **验证点 0.2**：故意极差 spec fixture（`fixtures/e2e/deliberately-bad-spec/`）存在且含 `spec.md` + `suite/`（含非空可解析测试文件）+ `run-suite.sh`。
- **验证点 0.3**：故意极差 spec 的审计评分输入（`criteria_scores`）全部低于阈值 → 预期 verdict=`insufficient`。
- **验证点 0.4**：修复后 spec（`fixtures/e2e/fixed-spec/`）的评分输入全部达标 → 预期 verdict=`survived`。
- **预期结果**：fixture 加载通过，Veto 触发条件与修复条件均满足。

### 阶段 1：ir-signed → spec（T1）
- **验证点 1.1**：初始态 `ir-draft`（无 state 标签）或 `ir-signed`。
- **验证点 1.2**：事件 `label:state:ir-signed`，sender_role ∈ {owner, agent}，`type:intent` ∈ label_set。
- **验证点 1.3**：guard 通过 → 状态转移至 `spec`，action=`invoke:spec-author`。
- **验证点 1.4**：重复投递同一事件（幂等）→ 当前态已变，无匹配转移 = no-op。
- **预期结果**：状态 = `spec`，触发 spec-author。

### 阶段 2：spec → redteam（T5）
- **验证点 2.1**：事件 `label:state:redteam`，sender_role ∈ {owner, agent}。
- **验证点 2.2**：guard 通过后，conductor **重断言 suite-ready 谓词**（确定性：suite/ 存在 + 非空测试文件 + `ast.parse` 可解析）。
- **验证点 2.3**：suite-ready 满足 → 转移至 `redteam`。
- **验证点 2.4（反向）**：若 suite/ 不存在或测试文件为空/不可解析 → T5 拒转（fail-closed），状态停留 `spec`。
- **预期结果**：状态 = `redteam`。

### 阶段 3：redteam → Veto（真实 insufficient）
- **验证点 3.1**：adversary 审计故意极差 spec → 产出 `verdict=insufficient`。
- **验证点 3.2**：insufficient 触发 check run 写回（`check_run_writeback.py`，conclusion=failure）。
- **验证点 3.3**：Veto 强制力（AC-14）：state 变为 `needs-human`，**无法进入 `wave-planned`**。
- **验证点 3.4**：Veto 理由（哪条 criterion 未过、阈值多少）写入报告，供机械核对。
- **预期结果**：状态 = `needs-human`，存在一条真实 insufficient 阻断记录。

### 阶段 4：修复 → survived
- **验证点 4.1**：人类/owner 用修复后 spec（`fixed-spec/`）替换故意极差 spec。
- **验证点 4.2**：修复 diff 与 Veto 理由**机械闭环**：Veto 理由中引用的 criterion 在修复后 spec 中均有对应改进（字符串级匹配）。
- **验证点 4.3**：重新审计 → `verdict=survived`。
- **验证点 4.4**：survived 落盘为该卡本次生命周期内的审计记录（三元组：card_id + specVersion + audit_run_id）。
- **预期结果**：survived 审计记录存在，修复 diff ↔ Veto 理由核对通过。

### 阶段 5：redteam → wave-planned（T6）
- **验证点 5.1**：事件 `label:state:wave-planned`，sender_role == agent，`adversary:survived` ∈ label_set。
- **验证点 5.2**：guard 通过后，conductor **重断言三元组 survived 谓词**（card_id + specVersion + audit_run_id 存在且匹配）。
- **验证点 5.3**：三元组满足 → 转移至 `wave-planned`。
- **验证点 5.4（反向）**：跨卡 / 历史 / 其它 specVersion 记录不计入（fail-closed）。
- **验证点 5.5（反向）**：`needs-human` 不可直跳 `wave-planned`（无转移定义 = 永拒，显式留痕断言）。
- **预期结果**：状态 = `wave-planned`。

### 阶段 6：认领（T3）
- **验证点 6.1**：事件 `comment:/claim`，sender_role ∈ {agent, owner, member, collaborator}。
- **验证点 6.2**：arbiter 前置裁决 allow。
- **验证点 6.3**：状态 `wave-planned` → `ready` → `in-progress`（认领）。
- **预期结果**：状态 = `in-progress`，assignee 已设置。

### 阶段 7：PR 绑定卡测试 → 合并
- **验证点 7.1**：实现 PR 引用卡 ID（`Card: Cloudbird-Software/.github#287`）。
- **验证点 7.2**：PR 触发卡对应测试集 + 已注册 holdout 测试（AC-3 / AC-17）。
- **验证点 7.3**：测试通过 → 合并放行。
- **验证点 7.4（反向）**：缺测试 / 空测试集 / holdout hash 不匹配 → 三类互异失败源的红记录。
- **预期结果**：PR 合并成功，check run 全绿。

---

## 2. 本地模拟执行矩阵（e2e-runner.py）

| 场景 | 输入 | 预期 verdict | 预期终态 | 覆盖 AC |
|------|------|--------------|----------|---------|
| S1 主通路（故意极差 spec） | bad-spec fixture | insufficient → needs-human | needs-human（阻断） | AC-4, AC-14, AC-20 |
| S2 修复后重审 | fixed-spec fixture | survived | wave-planned | AC-12, AC-20 |
| S3 完整闭环 | bad → Veto → fix → survived → wave-planned → claim → merge | insufficient 然后 survived | done | AC-20 全流程 |
| S4 无 Veto 全程（负向对照） | 直接给 survived | survived | wave-planned | AC-2（不算数） |
| S5 suite 未就绪 | 缺 suite/ | 拒转 | spec（停留） | AC-12 T5 |
| S6 needs-human 直跳 wave-planned | 跳过修复 | 永拒 | needs-human | AC-12 显式断言 |
| S7 跨卡三元组 | 用其它卡 survived 记录 | 拒转 | redteam（停留） | AC-12 T6 |

---

## 3. 真实 GitHub Actions 全流程前置清单

以下条件满足后，用**同一套 fixture** 触发真实全流程并回填 `verification-checklist.md`：

- [ ] cloudbrid-agent App 获得 `workflows` 权限（推送 `adversary.yml` / `adversary-gate.yml` / `conductor.yml`）
- [ ] 验证者 APP 创建并登记到 `expected-state.json`（`verifier_app.id` 非 null）
- [ ] `main-protection.json` 新增 `adversary` required check 已合并（PR #309）
- [ ] `org-required-workflows.json` 新增 `adversary-gate.yml` 已合并（PR #310）
- [ ] LLM provider endpoint 可达且 `LLM_API_KEY` 已注入 org secret
- [ ] 部署顺序：① workflow 文件就位 → ② adversary-gate 正常运行 → ③ 合并 PR #309/#310

---

## 4. 人类签收抽检（DECISION-05 第三层）

- 抽检对象：端到端证据链（时间线 + check run 链接链 + insufficient 阻断记录 + 修复 diff ↔ Veto 理由核对）。
- 抽检方式：独立于执行者的维护员对证据链做字符串级机械核对 + 主观判断。
- 落档位置：`verification-checklist.md` 的「人类签收」节（真实执行时回填）。
- 本地模拟阶段：以「模拟抽检记录」占位，注明「待真实执行后替换」。
