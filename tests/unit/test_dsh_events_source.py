# -*- coding: utf-8 -*-
"""DSH 事件源连接器单元测试 (2026-08-18)。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from trinity.memory.dsh_events_source import (  # noqa: E402
    DshEventsSource,
    classify_event,
    apply_event,
    PERSIST_TOOLS,
)
from trinity.memory.active_collector import (  # noqa: E402
    EventDrivenCollector,
    HookPoint,
)


def _ev(etype, data=None, session="s1", seq=1):
    return {"seq": seq, "session_id": session, "type": etype, "data": data or {}}


def test_user_message_maps_to_conversation_start():
    cls = classify_event(_ev("user/message", {"content": "汇总 trinity 情况"}))
    assert cls is not None
    assert cls["hook"] == "conversation_start"
    assert "汇总" in cls["task_desc"]
    assert cls["metadata"]["dsh_session"] == "s1"


def test_goal_write_maps_to_decision_point():
    cls = classify_event(_ev("goal/write", {"action": "create", "objective": "目标文本"}))
    assert cls is not None
    assert cls["hook"] == "decision_point"
    assert "Goal lifecycle" in cls["decision"]


def test_persist_tool_call_maps_to_decision_point():
    for tool in ("create_goal", "trinity_write", "schedule_create"):
        cls = classify_event(_ev("tool/call", {"name": tool}))
        assert cls is not None, tool
        assert cls["hook"] == "decision_point"
    assert tool in PERSIST_TOOLS


def test_ordinary_tool_call_ignored():
    cls = classify_event(_ev("tool/call", {"name": "pwsh"}))
    assert cls is None


def test_error_result_maps_to_error_event():
    cls = classify_event(_ev("tool/result", {"isError": True, "error": {"name": "CodeRunFailedError", "message": "boom"}}))
    assert cls is not None
    assert cls["hook"] == "error_event"
    assert cls["error_type"] == "CodeRunFailedError"


def test_ok_result_ignored():
    cls = classify_event(_ev("tool/result", {"isError": False}))
    assert cls is None


def test_aborted_turn_maps_to_error_event():
    cls = classify_event(_ev("turn/end", {"reason": {"kind": "aborted"}}))
    assert cls is not None
    assert cls["hook"] == "error_event"
    assert cls["error_type"] == "TurnAborted"


def test_normal_turn_ignored():
    cls = classify_event(_ev("turn/end", {"reason": {"kind": "completed"}}))
    assert cls is None


def test_capture_flags_disable():
    off = {"user_messages": False, "goal_write": False, "persist_tools": False, "errors": False}
    assert classify_event(_ev("user/message", {"content": "x"}), off) is None
    assert classify_event(_ev("goal/write", {}), off) is None
    assert classify_event(_ev("tool/call", {"name": "trinity_write"}), off) is None
    assert classify_event(_ev("tool/result", {"isError": True}), off) is None


def test_apply_event_into_collector():
    collector = EventDrivenCollector(importance_threshold=0.10)
    payload = apply_event(collector, _ev("goal/write", {"objective": "测试目标"}))
    assert payload is not None
    assert payload.hook_point == HookPoint.DECISION_POINT
    assert payload.importance >= 0.40
    assert payload.metadata.get("dsh_session") == "s1"


def test_cursor_roundtrip(tmp_path):
    cursor_file = str(tmp_path / "cursor.json")
    src = DshEventsSource(event_collector=None, store_path=str(tmp_path / "nope.db"), cursor_file=cursor_file)
    assert src._load_cursor() == 0
    src._save_cursor(12345)
    src2 = DshEventsSource(event_collector=None, store_path=str(tmp_path / "nope.db"), cursor_file=cursor_file)
    assert src2._load_cursor() == 12345
    # 游标基于 id(rowid)：seq 按会话分配非全局单调，禁止回退到 last_seq 语义
    assert src2._stats["last_id"] == 12345


def test_real_config_has_dsh_events_enabled():
    from trinity.memory.dsh_events_source import load_config
    cfg = load_config()
    assert cfg.get("enabled", True) is not False
    assert cfg.get("store_path", "").endswith("trinity_store.db")
