# build-c — Cloudbird-Software oracle 工具链（IR-0004 rev6）

离线构建物：Python 3.11 标准库 only，无网络、无 git、无第三方依赖。全部文件 UTF-8 / LF。

## 接口哲学（PM 优先范式）
- **制作可选**：oracle 本身可以不制作——没有冻结样本、没有换代，都不是失败。
- **接口必须**：注册表 schema 与读写/校验接口永远存在、永远可用（AC-11）。
- **消费常在**：对拍器与 fan-out 消费者永远在场；输入缺席、为空、超时一律非零退出，
  绝不静默停摆、绝不静默放行（AC-12 / AC-13）。
- **fan-out 生产者可选，消费者常在**：products 目录为空是合法状态（`--empty-ok`），
  非空则逐条严校验 append-only 哈希链。
- **换代只追加，不修补**：历史 generations 不可改写；篡改（时间倒序 / sha 重复 /
  frozen_sha 与最新代不一致）在 validate 即被检出。

## AC 映射
| AC | 组件 | 职责 |
|----|------|------|
| AC-11 | `oracle-registry.schema.yaml`、`oracle/registry.py`、`oracle/cycle.py` | 契约+校验+幂等注册+退役+换代 |
| AC-12 | `oracle/diffbench.py` | 差分对拍：硬区 exit 2 / 软区警告 / 未运行 exit 1 |
| AC-13 | `fanout/consumer.py` | 红队燃料消费：JSONL 校验、链校验、攻击查询生成 |
| AC-19 | `drill/cnb-drill.py` | CNB 三接缝可脱离性月度干跑（static / functional） |

## 用法（在 build-c/ 下）
```bash
# 注册表：校验 / 注册（幂等，写前校验） / 退役（只允许 frozen→retired）
python oracle/registry.py --registry r.yaml validate
python oracle/registry.py --registry r.yaml register --name parser-core \
  --host-repo cloudbird/hero --target-surface parse --frozen-sha <40hex> \
  --cluster c1 --decorrelation-reason "独立实现+不同语料" \
  --hard-zone "case/hard/*" --soft-zone "case/soft/*"
python oracle/registry.py --registry r.yaml retire --frozen-sha <40hex>

# 换代：旧代 retired、新代追加 generations（只换代不修补）
python oracle/cycle.py --registry r.yaml --old <old40hex> --new <new40hex>

# 差分对拍（gate 侧）：按注册表硬/软区 glob 裁决，输出 {date, entries, verdicts} 台账
python oracle/diffbench.py --registry r.yaml --champion-out champ.jsonl \
  --oracle-out oracle.jsonl --zones --ledger diffbench-ledger.json

# fan-out 消费（红队/道闸燃料）：空目录须 --empty-ok 授权
python fanout/consumer.py --products-dir products/ --empty-ok \
  --expect-base-sha <40hex> --ledger consumer-ledger.json

# CNB 可脱离性干跑（AC-19 月度）：static 零副作用；functional 只输出 runbook
python drill/cnb-drill.py --mode static --repo-root <仓树>
python drill/cnb-drill.py --mode functional
```

## 退出码
| 工具 | 0 | 1 | 2 |
|------|---|---|---|
| registry.py | 合法 / 幂等命中 | 畸形注册表（含篡改检出）/ 非法迁移 | — |
| cycle.py | 换代完成 | 旧代非 frozen、新代非 candidate、形状非法 | — |
| diffbench.py | 等价或仅软区分歧 | 注册表非法 / 对拍未运行 / 输入缺失 / 超时 | 硬区分歧 |
| consumer.py | 消费完成 / 授权空 | 目录缺失 / 未授权空 / 断链 / 非法记录 / SHA 不符 | — |
| cnb-drill.py | static 绿 / functional 清单 | static 红（越界或缺 REMOVAL.md） | — |

## 输入格式
- 对拍输出：JSONL，每行 `{"case": "<id>", "output": <任意 JSON 值>}`；案例 id 按注册表
  `hard_zone` / `soft_zone` glob 归区（fnmatch，区分大小写）。非 JSONL 文本回退整文件比对。
- products：JSONL，必填 `card_id` `spec_hash` `base_sha` `type` `prev_hash`；
  `type ∈ {skeleton_divergence, assumption, eliminated_route, diff_divergence}`；
  哈希链：首行 `prev_hash` 为 64 个 `0`，其后每行等于上一行整记录的 SHA-256
  （规范化 JSON：`sort_keys=True, separators=(",", ":"), ensure_ascii=False`，UTF-8）。
  `eliminated_route` 会从 payload 的 `route/summary/points/routes` 字段机械生成
  “champion 是否覆盖：<路线要点>” 攻击查询文本。

## 自查（离线）
```bash
PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -t . -v   # 44 用例全绿
python -m py_compile oracle/*.py fanout/*.py drill/*.py tests/*.py    # 语法
python -c "from oracle.miniyaml import load_yaml; \
  d=load_yaml(open('oracle-registry.schema.yaml',encoding='utf-8').read()); \
  print(d['schema'], d['version'])"                                    # YAML 解析
```
