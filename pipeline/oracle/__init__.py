"""Cloudbird-Software oracle 工具包（IR-0004 rev6 AC-11/12）。

PM 优先范式：oracle 制作可选，但接口与消费必须。
- registry：注册表读写+校验（schema 见 oracle-registry.schema.yaml）
- diffbench：差分对拍消费器（gate 侧）
- cycle：换代脚本（只换代不修补）
- miniyaml：离线 YAML 子集解析器（标准库 only）
"""
