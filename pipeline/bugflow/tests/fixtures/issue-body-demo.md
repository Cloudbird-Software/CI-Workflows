### 受影响的仓（owner/name）

Cloudbird-Software/CI-Workflows

### 症状签名（一句话 + 关键报错）

fixture 演示：case_bug 断言失败（demo 场景）

### 关键栈（可选）

AssertionError: demo fixture red

### 复现步骤

1. 运行 fixture repro 用例（base 状态）
2. 观察断言失败输出

### 期望结果

REPRO_OUTCOME: pass

### 实际结果

REPRO_OUTCOME: fail（退出码 1）

### 环境指纹（可选）

ubuntu-24.04 runner（上报面无机器键时，对账由场景 manifest 承担）

### 机器复现用例（可选）

bash pipeline/bugflow/tests/fixtures/scenario-reproduced/repro.sh
