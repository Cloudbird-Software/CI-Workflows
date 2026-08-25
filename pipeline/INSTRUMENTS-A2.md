# quality-instruments —— 五件质量仪器（IR-0004 AC-3/4/5/6/7，rev6）

Cloudbird-Software CI-Workflows `pipeline/testing/`：仪器+机械判定；LLM/弱模型只在
生成/触发侧（本模块不含 LLM 调用）。Python 3.11 标准库 only，第三方工具只探测调用
不硬依赖。所有文本文件 UTF-8 + LF。

## 1. fuzz —— schema 感知种子生成 + 语料台账 + 崩溃去重（AC-3）

```bash
python pipeline/testing/fuzz/seedgen.py --schema fuzz/schema.json --out-dir corpus/
python pipeline/testing/fuzz/corpus.py    add --corpus-dir corpus/ --dir corpus/ --generator seedgen
python pipeline/testing/fuzz/corpus.py    growth --corpus-dir corpus/ --out growth.jsonl
python pipeline/testing/fuzz/dedup.py     --dir crashes/ --out fingerprints.json
```

- seedgen：15 类内置边界生成器（空/极小/极大/越界/Unicode/类型混淆/深嵌套 32/64
  层/数值极值/null/超长串/缺失必填/多余字段/枚举边界/空白串），纯机械无随机；
- corpus：内容寻址（sha256）入库天然去重，ledger.jsonl append-only，growth 出
  按日累计曲线 JSONL；
- dedup：指纹 = sha256(异常类型 + basename:func 栈帧序列)，默认丢行号（重编译
  不裂变），`--strict-lines` 严格模式。

## 2. metamorphic —— 蜕变关系 catalog + 可执行检查（AC-4）

```bash
python pipeline/testing/metamorphic/relations.py --catalog catalog.yaml --case case.json
python pipeline/testing/metamorphic/relations.py --catalog catalog.yaml --list
```

catalog.yaml 收录 15 条（3 implemented / 10 candidate / 2 rejected，字段
id/relation/verify/applies_to/status）。implemented 三条为可执行检查：
MR-001 输入重排→结果集等价、MR-002 重试→幂等、MR-003 单元拆分→总量守恒。
反例判 fail/error → 退出码 1。

## 3. symbolic —— 符号执行试点评估器（AC-5）

```bash
python pipeline/testing/symbolic/pilot.py --target module.py --out-dir reports/ [--force-proxy]
```

探测 pynguin（`python -m pynguin --version`，只探测）；不可用回落 AST 静态近似
（分支数/循环深度/可达路径上界），指标显式 `proxy=true`。输出三段式报告
（证据/指标/结论，markdown+JSON）：路径覆盖估计、求解超时率、单位时间发现数；
结论仅 adopt|reject 二值，reject 必带 revisit_when，报告附「复算命令」行
（ADR-0085 复算锚点）。

## 4. sast —— 分诊台账 + 全量 sweep（AC-6）

```bash
python pipeline/testing/sast/ledger.py init   --ledger ledger.yaml
python pipeline/testing/sast/ledger.py append --ledger ledger.yaml --fingerprint F \
    --repo owner/name --rule py/x --severity error --disposition fixed \
    --resolved-sha <40hex> [--adr ADR-xxxx | --reason 文本] [--date YYYY-MM-DD]
python pipeline/testing/sast/ledger.py verify --ledger ledger.yaml
python pipeline/testing/sast/sweep.py --alerts codeql-alerts.json --ledger ledger.yaml
```

- 台账 append-only：每条含 chain_sha = sha256(前链 + canonical_json(本条))，
      任何篡改/删除/重排使 verify 退出码 2（自实现，不 import 仓外）；
- disposition 规则：fixed→resolved_sha(40hex)；waived→adr；false_positive→reason；
- sweep：比对告警与台账，未处置清单 + 退出码 1（需开 issue）；台账坏链退出码 2；
  兼容 CodeQL REST alerts 形状（只取 open）。演示 fixtures 见 sast/fixtures/。

## 5. formal —— 形式化条件触发（AC-7）

```bash
python pipeline/testing/formal/trigger.py --meta card-meta.yaml [--out verdict.json]
```

checklist.yaml：4 正适用（数学核心/状态机/密码原语/高风险稳定小块）+ 4 反适用
（胶水/无半形式化起点/演进契约/无界输入），每项 id + source(元数据字段) + rule
（机械算子）+ semantic_fields（human/LLM 填充位，只留痕）。
final：risk_level 缺失 → needs_risk_level（退出码 3，fail-closed）＞正命中
applicable ＞ 反命中 not_applicable。

## 6. CI 接入（reusable workflow）

```yaml
quality:
  uses: Cloudbird-Software/CI-Workflows/.github/workflows/quality-instruments.yml@<ref>
  with: {instrument: fuzz, target_repo: Cloudbird-Software/api-gateway}
```

instrument ∈ fuzz|metamorphic|symbolic|sast-sweep|formal-check；actions 只用钉 SHA
checkout/setup-python，permissions: contents: read，timeout 20min；每周一 06:43 UTC
cron 对全 org 跑 sast-sweep（无 security-events 授权时回落 fixture 演示 +
--no-enforce）。

## 7. 离线自测

```bash
python -m unittest discover -s tests -v      # 42 用例（fixtures 自带，无网络）
python -m compileall -q pipeline tests       # py_compile 全过
python - <<'PY'                              # YAML 解析自检（含 workflow）
from pathlib import Path
from pipeline.testing import _yamlmini as y
for p in Path(".").rglob("*.yaml"): y.load(p)
for p in Path(".github/workflows").glob("*.yml"): y.load(p)
print("yaml ok")
PY
```

退出码约定：relations 0/1（fail）；sweep 0/1（未处置）/2（坏链）；ledger verify
0/2；trigger 0/3（needs_risk_level）；seedgen/corpus/dedup/pilot 0/2。
