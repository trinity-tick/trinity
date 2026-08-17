# -*- coding: utf-8 -*-
"""tests/ 根 conftest——保证 tests/ 下所有测试处于隔离模式。

2026-08-16(价值评估轮):此前仅 trinity/tests/conftest.py 设置
TRINITY_TESTING=1;当只运行 `pytest tests/`(不收集 trinity/tests)时
该变量未设置,进化/市场模块会读真实文件与线上审计(如
TestMetaEvolutionObservation 断言 len==1 实际收到线上观察而失败)。
"""
import os

os.environ.setdefault("TRINITY_TESTING", "1")
