# pipeline/dsl + pipeline/entropy —— 验收 DSL 编译与骨架方差仪器

Cloudbird-Software/CI-Workflows pipeline/ 新模块（IR-0004 AC-8/AC-9 rev6）。
Python 3.11 标准库 only，零网络零第三方依赖；产物 UTF-8 + LF、确定性输出。

## 1. 验收 DSL 编译器（AC-8 / IFACE-04）

```bash
# 编译：spec.md frontmatter 的 acceptanceCriteria → pytest 骨架
python pipeline/dsl/compile.py --spec specs/IR-0007/spec.md \
    --out specs/IR-0007/suite/generated/test_ir_0007.py
# 校验（gate 用）：重算 spec-hash / 逐 AC hash / 应然内容，任一不符=exit 1
python pipeline/dsl/verify.py --generated specs/IR-0007/suite/generated/test_ir_0007.py \
    --spec specs/IR-0007/spec.md
# 只校验不重生成（compile --check 转发 verify）
python pipeline/dsl/compile.py --spec ... --out ... --check
```

- 生成物每 AC 一个 `test_<ac_id 小写下划线>` 函数：docstring 载 given/when/then
  原文，占位断言 `assert False, "TODO: 由实施卡填充——…"`（实施卡填充=合法改写起点）。
- 头部含 `spec-hash`（spec.md 全文字节 sha256）、逐 `ac-hash`、`regenerate` 命令行。
- 确定性渲染：生成物是 (spec 全文字节, AC 序列, --spec/--out 参数串) 的纯函数
  ——verify 以同形路径重渲染"应然内容"逐字节比对，手改正文但 hash 头不动也红。
- 红处置（verify 输出必含）：手改生成测试：改 spec 后重新编译，或删除本文件由
  实施卡重写。改判据必须回 spec 层。
- 编译/校验以同形路径调用（组织约定：仓根相对路径），否则 regenerate 行致假红。

## 2. 骨架解读方差仪器（AC-9 rev6，PM 可选用——工具非流程）

```bash
python pipeline/entropy/divergence.py --dir skeletons/ --out out/ \
    --card-id CARD-07 --spec-hash <sha256> --base-sha <sha> [--threshold 0.85]
```

- 输入：目录内 N 份骨架 md，每份四节 `## 路线陈述 / ## 接口签名 / ## 测试草案 /
  ## 假设清单`（fixture：tests/fixtures/skeletons/ 三份示例）。
- 输出 `out/divergence-report.json`：
  - `pairs`：两两字符 3-gram Jaccard 相似度，参数版本化留痕
    `{algo: "3gram-jaccard", version: "v1", threshold: 0.85}`；超阈值=疑似串通/拷贝
    （防人为放大交集，AC-9 独立性校验）。
  - `convergence_pct`：趋同度=100×|测试草案交集|/|并集|。
  - `contract_divergences` / `route_divergences`：关键词启发式归类（接口签名/测试
    草案节分歧=契约，路线陈述节=路线），每条判断附证据片段与命中关键词。
  - `assumptions_union`（spec 缺口显式清单）、`test_intersection`（验收标准候选）、
    `test_union_minus_intersection`（红队输入燃料）。
- 输出 `out/fanout-products.jsonl`（AC-13 / IFACE-03 燃料目录契约）：
  `{type: skeleton_divergence|assumption, card_id, spec_hash, base_sha, …}`，
  base_sha 动态传入，消费侧机械核对；目录 append-only 由消费侧校验。

## 3. gate 接线（.github/workflows/spec-dsl-gate.yml）

可复用 workflow（workflow_call）：PR 变更命中 specs/<IR>/ 时——frontmatter 可
解析+DSL 可编译（干跑不落仓）；存在 suite/generated/ 则逐文件 verify.py 校验。
caller 须钉 ref 引用并以 `detector-ref` 同值输入钉执法工具来源（fail-closed）。

## 4. AC 对应

| 组件 | 条款 |
| --- | --- |
| compile.py | AC-8（DSL→测试骨架+spec hash 溯源头）、IFACE-04 |
| verify.py | AC-8（手改生成测试触发 CI 红；改 spec 重编译转绿） |
| spec-dsl-gate.yml | AC-8（specs 路径关卡；实施后全 PR 生效） |
| divergence.py | AC-9 rev6（仪器化：交集/并集/独立性/趋同度+燃料） |
| fanout-products.jsonl | AC-13 / IFACE-03（生产者可选、消费者常在） |

## 5. 边界：g060 / T-13（ADR-0035）

- hash 溯源（本模块）管辖 **DSL 编译生成物**：手改即红，改判据必须回 spec 层。
- T-13 test-integrity 管辖 **一般测试文件四形态**（删除/断言净下降/抑制标记/
  期望值改写，TI-R4 require_adr）。
- 同一文件双命中时**从严者生效**（本对齐为 AC-8 承接声明，不修改 T-13 语义）。
- 与 g010/spec-check.py 并存分工：spec-check 管 frontmatter 结构与注入双扫，
  本模块管 DSL 可编译与 hash 溯源，重叠面从严者生效、不重复建设。

## 6. 自测

```bash
python -m unittest discover -s tests -v   # 28 用例（DSL 17 + entropy 11）全绿
```
