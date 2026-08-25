"""质量仪器（IR-0004 AC-3/4/5/6/7，rev6：仪器+机械判定）。

子模块：
  fuzz/        schema 感知种子生成 + corpus 台账 + 崩溃栈去重（AC-3）
  metamorphic/ 蜕变关系 catalog + 可执行检查（AC-4）
  symbolic/    符号执行试点评估器（AC-5）
  sast/        SAST 分诊台账 + 全量 sweep（AC-6）
  formal/      形式化条件触发 checklist（AC-7）

本包 Python 3.11 标准库 only；第三方工具（pynguin 等）只探测调用，不硬依赖。
"""
