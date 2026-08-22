# -*- coding: utf-8 -*-
"""Unit tests for trinity.memory.skill_forge — Skill 自动锻造管线。

覆盖：轨迹解析字段容错、规则式降级模板、LLM 输出解析容错（注入假 LLM）、
front-matter 渲染、文件名安全化、dry-run 不写库。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from trinity.memory import skill_forge as sf


# ---- 样例轨迹 ----
TRACE_TOOL = [
    # tool/action/result/ts 常见字段名
    {"trace_id": "s1", "ts": 1, "tool": "scan_dir", "input": {"path": "docs"}, "result": "found 3 files"},
    {"trace_id": "s1", "ts": 2, "tool": "move_file", "input": {"src": "a", "dst": "b"}, "error": "permission denied"},
    {"trace_id": "s2", "ts": 1, "action": "open", "result": "ok"},
]

# sidecar 风格：payload 是 Python 风格 dict 字符串
TRACE_SIDECAR = [
    {"obj_id": "skill_1", "obj_type": "skill_state", "payload": "{'skill': 'file-organizer', 'phase': 'scan'}",
     "round_idx": 1, "pruned_at": 1784039225.5, "dependencies": []},
    {"obj_id": "skill_1", "obj_type": "skill_state", "payload": "{'skill': 'file-organizer', 'phase': 'extract'}",
     "round_idx": 2, "pruned_at": 1784039225.6, "dependencies": []},
]


# ---------------------------------------------------------------------------
# 轨迹解析
# ---------------------------------------------------------------------------
def test_parse_traces_field_fallback():
    seqs = sf.parse_traces(TRACE_TOOL)
    assert len(seqs) >= 3
    by_action = {s["action"]: s for s in seqs}
    assert by_action["scan_dir"]["input"] == '{"path": "docs"}'
    assert by_action["move_file"]["error"] == "permission denied"
    # 按 trace_id 分组
    assert all(s["trace_id"] in ("s1", "s2") for s in seqs)


def test_parse_traces_sidecar_payload_string():
    seqs = sf.parse_traces(TRACE_SIDECAR)
    assert len(seqs) == 2
    acts = [s["action"] for s in seqs]
    # payload 内字符串 dict 被正确解析，skill 名作为高优先 action
    assert acts == ["file-organizer", "file-organizer"]
    # phase 仍被保留在 raw（被折叠进底层解析），raw 非空
    assert all(s["raw"] for s in seqs)


def test_parse_traces_skips_non_dict_and_empty():
    seqs = sf.parse_traces([{}, "not-a-dict", {"action": "x", "input": None}, ["x"]])
    assert len(seqs) == 1
    assert seqs[0]["action"] == "x"


# ---------------------------------------------------------------------------
# 降级模板
# ---------------------------------------------------------------------------
def test_fallback_pattern_no_crash_and_has_steps():
    pats = sf._fallback_pattern(TRACE_TOOL)
    assert pats["steps"]  # 产出含步骤
    assert pats["name"]
    # 不含 llm key 时 extract_patterns 走降级
    os.environ.pop("TRINITY_SKILL_API_KEY", None)
    os.environ.pop("TRINITY_API_KEY", None)
    p2 = sf.extract_patterns(TRACE_TOOL, llm_enabled=True)
    assert "steps" in p2 and p2["steps"]


def test_extract_patterns_llm_disabled_uses_rule():
    p = sf.extract_patterns(TRACE_TOOL, llm_enabled=False)
    assert p["steps"]


# ---------------------------------------------------------------------------
# LLM 输出解析容错
# ---------------------------------------------------------------------------
def test_parse_pattern_markdown_block():
    text = "```json\n{\"name\": \"x\", \"domain\": \"d\", \"summary\": \"s\", \"steps\": [\"1. a\"], \"pitfalls\": [\"p\"]}\n```"
    p = sf._parse_pattern(text)
    assert p["name"] == "x"
    assert p["steps"] == ["1. a"]
    assert p["pitfalls"] == ["p"]


def test_parse_pattern_truncated_json():
    text = '{"name": "x", "domain": "d", "summary": "s", "steps": ["1. a", "2. b"], "pitfalls": ["p1",'
    p = sf._parse_pattern(text)
    # 截断 JSON 无法仅靠补右括号恢复为合法结构，但必须优雅降级不崩，
    # 并把内容作为 summary 保留，保证下游可继续渲染。
    assert p is not None
    assert isinstance(p, dict)
    assert p.get("summary")


def test_parse_pattern_plain_text_fallback():
    p = sf._parse_pattern("这是纯文本，没有 JSON 结构，但应被解析为 summary")
    assert p is not None
    assert p.get("summary")


def test_extract_patterns_injected_fake_llm():
    captured = {}

    def fake_llm(system, user):
        captured["system"] = system
        return '{"name": "n", "domain": "d", "summary": "s", "steps": ["1. go", "2. stop"], "pitfalls": []}'

    # 强制有 key，走 llm 分支
    os.environ["TRINITY_SKILL_API_KEY"] = "test-key"
    try:
        p = sf.extract_patterns(TRACE_TOOL, llm_enabled=True, llm_call=fake_llm)
    finally:
        os.environ.pop("TRINITY_SKILL_API_KEY", None)
    assert p["name"] == "n"
    assert len(p["steps"]) == 2


# ---------------------------------------------------------------------------
# front-matter 渲染
# ---------------------------------------------------------------------------
def test_render_skill_front_matter():
    pattern = {
        "name": "scan-dir", "domain": "data-org",
        "summary": "扫描目录并归类文件", "steps": ["1. 扫描", "2. 归类"], "pitfalls": ["权限不足"],
    }
    md = sf.render_skill("scan-dir", "data-org", pattern, traces_count=5, source="trace.jsonl")
    assert md.startswith("---")
    for key in sf.FRONT_MATTER_KEYS:
        assert f"{key}:" in md
    assert "traces_count: 5" in md
    assert "## 适用场景" in md
    assert "## 步骤" in md
    assert "1. 扫描" in md
    assert "## 坑位" in md
    assert "权限不足" in md


def test_render_skill_empty_steps_placeholder():
    md = sf.render_skill("x", "g", {"summary": "s", "steps": [], "pitfalls": []}, traces_count=0)
    assert "（无明确步骤，待人工补充）" in md


# ---------------------------------------------------------------------------
# 文件名安全化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("file-organizer", "file-organizer"),
        ("../../evil/../..", "unnamed"),
        ("a b/c*d", "c_d"),
        ("", "unnamed"),
        ("../weird\\name", "name"),
    ],
)
def test_safe_filename(raw, expected):
    assert sf.safe_filename(raw) == expected


def test_safe_filename_replaces_illegal():
    assert "/" not in sf.safe_filename("..\\..\\evil name!@#")
    assert "\\" not in sf.safe_filename("..\\..\\evil name!@#")


# ---------------------------------------------------------------------------
# 写出目录 + dry-run
# ---------------------------------------------------------------------------
def test_write_skill_to_temp_dir():
    md = sf.render_skill("sub-dir", "g", {"summary": "s", "steps": ["1. a"], "pitfalls": []}, traces_count=1)
    with tempfile.TemporaryDirectory() as tmp:
        path = sf.write_skill(md, "sub-dir", tmp)
        assert os.path.isfile(path)
        assert os.path.basename(path) == "sub-dir.md"
        with open(path, "rb") as f:
            assert f.read(3) != b"\xef\xbb\xbf"  # 无 BOM


def test_store_skill_meta_dry_run_writes_nothing():
    # store=None → dry-run，返回 None 且不写库
    md = sf.render_skill("s", "g", {"summary": "s", "steps": [], "pitfalls": []}, traces_count=1)
    assert sf.store_skill_meta(None, md) is None


def test_store_skill_meta_calls_injected_store():
    md = sf.render_skill("mem-skill", "g", {"summary": "sum", "steps": ["1. a"], "pitfalls": []}, traces_count=2)
    calls: list = []

    def fake_store(**kwargs):
        calls.append(kwargs)
        return {"memory_id": 123}

    res = sf.store_skill_meta(fake_store, md)
    assert res == {"memory_id": 123}
    assert len(calls) == 1
    assert calls[0]["category"] == "skill"
    assert calls[0]["metadata"]["skill_name"] == "mem-skill"
    assert "skill" in calls[0]["tags"]
