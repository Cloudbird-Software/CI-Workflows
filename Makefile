# CI-Workflows Makefile（#366 项 5 / ADR-0055 决策 11 落点）
# 入口协议块第 4 步（make card-test / make gates-pr）在 CI-Workflows 的兑现面——
# 与治理仓 .github 同款诚实薄封装：真实执行 ci.yml 的本地可等价部分
# （bash -n / py_compile / yaml 解析 / 快速自测），CI 关卡语义仍以
# .github/workflows/ci.yml 为准，不伪装已运行 CI。
CARD ?=
REPO ?= Cloudbird-Software/CI-Workflows

.PHONY: card-test gates-pr
card-test: ## 读卡 AC 列表并提示测试先行：make card-test CARD=<issue#>
	@test -n "$(CARD)" || { echo "用法: make card-test CARD=<issue#>（缺 CARD）" >&2; exit 2; }
	@echo "== 卡 $(REPO)#$(CARD) 的 AC（测试先行：先按 AC 写红测试再实现）=="
	@gh issue view "$(CARD)" -R "$(REPO)" --json number,title,body \
	  --jq '"#\(.number) \(.title)\n\n\(.body)"' 2>/dev/null \
	  | awk 'NR==1{print;print ""} /^## AC/{f=1} f{print} f && /^## / && !/^## AC/{exit}' | head -60
	@echo "(空=拉取失败或卡无 AC 节——手动: gh issue view $(CARD) -R $(REPO))"
	@echo "== 提示：CIW 改动的完整测试面在 ci.yml 各 selftest job；用 make gates-pr 自检后再开 PR =="

gates-pr: ## 本地等价关卡清单（ci.yml 语义）：make gates-pr
	@echo "== gates-pr：ci.yml 的本地可等价部分（真实执行；CI 关卡仍以 ci.yml 为准）=="
	@find scripts pipeline -name '*.sh' -print0 | xargs -0 -n1 bash -n \
	  && echo "OK   bash -n（scripts+pipeline 全部 shell 脚本）"
	@find scripts pipeline -name '*.py' -print0 | xargs -0 -n1 python3 -W ignore -m py_compile \
	  && echo "OK   py_compile（scripts+pipeline 全部 python）"
	@python3 -c "import glob,yaml;fs=glob.glob('.github/workflows/*.yml')+glob.glob('policy/*.yaml')+['zizmor.yml']+glob.glob('pipeline/**/*.yaml',recursive=True)+glob.glob('pipeline/**/*.yml',recursive=True);[yaml.safe_load(open(f,encoding='utf-8')) for f in fs];print(f'OK   yaml 解析（{len(fs)} 个：workflows/policy/pipeline）')"
	@bash scripts/test-integrity-fixtures/run.sh >/dev/null \
	  && echo "OK   test-integrity 自测（T8 fixtures，与 ci.yml 同款）"
	@bash scripts/suppression-budget-selftest.sh >/dev/null \
          && echo "OK   suppression-budget 自测（与 ci.yml 同款）"
	@bash pipeline/attestation/selftest/run-selftest.sh >/dev/null \
	  && echo "OK   attestation 自测（W4-R3 签名证据包，与 ci.yml 同款）"
	@echo "== 开 PR 前检查单（机器不可判部分）：PR body 引用 ADR-NNNN（C1）/ body 带 Card: 元数据行 / diff<400 行 =="
	@echo "== 注：metering/adversary/bugflow/entropy 等 pipeline selftest 本地未跑（依赖 CI 环境），以 ci.yml 各 job 为准 =="
