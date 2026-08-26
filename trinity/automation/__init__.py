"""Trinity Automation — Budibase 借鉴的声明式事件驱动规则层（Phase 1，默认关闭）。

用法: automation.emit("memory.write", {...})；规则见 ~/.trinity/automation/rules.yaml。
"""
from .engine import AutomationEngine, emit, enabled, get_engine  # noqa: F401
