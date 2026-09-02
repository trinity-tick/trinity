#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自传注入刷新测试（2026-09-01，第三十一轮）：
update_dsh_agents_md 的 TRINITY_SNAPSHOT 段替换逻辑（USERPROFILE 隔离，不碰真实 ~/.dsh）。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)


class TestDshAgentsMdRefresh(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="dsh_agents_")
        self._old_home = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = self._tmp
        os.makedirs(os.path.join(self._tmp, ".dsh"), exist_ok=True)
        with open(os.path.join(self._tmp, ".dsh", "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("# AGENTS.md test\n\n<!-- TRINITY_SNAPSHOT -->\nold\n<!-- /TRINITY_SNAPSHOT -->\n")

    def tearDown(self):
        if self._old_home:
            os.environ["USERPROFILE"] = self._old_home
        else:
            os.environ.pop("USERPROFILE", None)

    def test_snapshot_block_replaced(self):
        import runpy
        try:
            runpy.run_path(os.path.join(ROOT, "scripts", "update_dsh_agents_md.py"),
                           run_name="__main__")
        except SystemExit:
            pass  # 脚本 sys.exit(0) 经 runpy 传播
        with open(os.path.join(self._tmp, ".dsh", "AGENTS.md"), encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("old", content)
        self.assertIn("TRINITY_SNAPSHOT -->\n- API:", content)
        self.assertIn("快照时间", content)

    def test_markers_preserved(self):
        import runpy
        try:
            runpy.run_path(os.path.join(ROOT, "scripts", "update_dsh_agents_md.py"),
                           run_name="__main__")
        except SystemExit:
            pass
        with open(os.path.join(self._tmp, ".dsh", "AGENTS.md"), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("<!-- TRINITY_SNAPSHOT -->", content)
        self.assertIn("<!-- /TRINITY_SNAPSHOT -->", content)
        self.assertEqual(content.count("<!-- TRINITY_SNAPSHOT -->"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
