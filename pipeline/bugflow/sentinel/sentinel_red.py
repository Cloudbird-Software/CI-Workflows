#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""故意失败哨兵（W3-C1 .github#218 / ADR-0064 决策 3）

复现运行前的环境自证证据之一：本用例被设计为【必红】，并以 SENTINEL-RED
标记声明"这是断言红"（区别于命令找不到/崩溃等基础设施红——后者不能证明
环境看得见断言失败）。它绿了或标记缺失 = 这套环境看不见红 = 一切"复现
失败"类结论失效 → bugflow.py 拒绝判定（fail-closed，AC-1）。
配套证据 = 基线套件全绿（tests/run-tests.sh）：绿能看见 + 红能看见，
环境才算自证（ADR-0064 决策 3）。
"""
print("SENTINEL-RED: 本用例必须失败——看见本行且退出码非 0 才算环境自证通过")
print("REPRO_OUTCOME: fail")
raise SystemExit(1)
