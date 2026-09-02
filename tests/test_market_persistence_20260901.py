#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场持久化测试（2026-09-01）：订单簿 JSON 落盘→重启恢复闭环。

要点：TRINITY_TESTING 必须为空（否则 _load/_save 跳过持久化）；
_ORDERBOOK_FILE 临时改到 tmp 目录，避免污染真实市场。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)


class TestMarketPersistence(unittest.TestCase):
    def setUp(self):
        import trinity.market.orderbook as ob_mod
        self.ob_mod = ob_mod
        self._tmp = tempfile.mkdtemp(prefix="mkt_persist_")
        self._orig_file = ob_mod._ORDERBOOK_FILE
        ob_mod._ORDERBOOK_FILE = os.path.join(self._tmp, "memory_market_orderbook.json")
        # 2026-09-02 (EXECUTION 457): env 必须在 setUp 内切换并还原——
        # 原 import 期 pop 会在同进程污染其他测试（finish 套件加载真实订单簿）
        self._prev_testing = os.environ.get("TRINITY_TESTING")
        os.environ.pop("TRINITY_TESTING", None)

    def tearDown(self):
        self.ob_mod._ORDERBOOK_FILE = self._orig_file
        if self._prev_testing is None:
            os.environ.pop("TRINITY_TESTING", None)
        else:
            os.environ["TRINITY_TESTING"] = self._prev_testing

    def test_orderbook_roundtrip(self):
        from trinity.market.memory_asset import create_asset
        ob = self.ob_mod.OrderBook()
        asset = create_asset(
            memory={"memory_id": "mem_persist_unit_1", "modality": "test"},
            owner="default")
        ob.list_asset(asset=asset, price=0.0, currency="trust_score")
        # 模拟重启：新实例从 JSON 恢复
        ob2 = self.ob_mod.OrderBook()
        self.assertIn("mem_persist_unit_1", ob2._orders)
        self.assertTrue(ob2.is_listed("mem_persist_unit_1"))
        # 清理
        ob2.delist_asset("mem_persist_unit_1")

    def test_orderbook_file_written(self):
        from trinity.market.memory_asset import create_asset
        ob = self.ob_mod.OrderBook()
        ob.list_asset(asset=create_asset(
            memory={"memory_id": "mem_persist_unit_2", "modality": "test"},
            owner="kb-harvester"), price=0.0)
        self.assertTrue(os.path.exists(self.ob_mod._ORDERBOOK_FILE))
        data = json.load(open(self.ob_mod._ORDERBOOK_FILE, encoding="utf-8"))
        self.assertIn("mem_persist_unit_2", data)
        ob.delist_asset("mem_persist_unit_2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
