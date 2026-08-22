#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单测：trinity.memory.offload — 短期记忆 Mermaid 符号卸载（context offload）。

全部用例用临时目录（TRINITY_OFFLOAD_DIR 指向 tmp_path），不碰 ~/.trinity 运行时
大库。覆盖：
  - 画布生成含全部 node_id、节点串联
  - 原文逐条落盘 + drill_down 往返一致
  - search 命中 / 不命中
  - TRINITY_OFFLOAD_LLM 关闭时摘要走规则抽取（自带 summary 优先）
  - 自带 summary 优先于规则抽取
  - 损坏 ref 文件 drill_down 返回 None 不崩
  - 同 task_id 重跑为覆盖写（旧节点不残留）
  - 缺 entries / 空 content 容错
  - 路径穿越 task_id 被净化
  - canvas_path / index 文件确实落盘
"""

import json
import os

import pytest

from trinity.memory import offload as offload_mod


@pytest.fixture(autouse=True)
def offload_tmp(monkeypatch, tmp_path):
    """把 offload 根目录重定向到临时目录，并强制规则模式。"""
    monkeypatch.setenv("TRINITY_OFFLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("TRINITY_OFFLOAD_LLM", "off")
    monkeypatch.delenv("TRINITY_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return tmp_path


def _entries():
    return [
        {"node_type": "step", "content": "分析了 WMS 对标，识别出 3 处缺口。", "ts": "2026-08-21"},
        {"node_type": "result", "summary": "输出迁移方案文档", "content": "生成了迁移方案。", "ts": "2026-08-21"},
        {"node_type": "step", "content": "第二步骤，测试通过。", "ts": "2026-08-22"},
    ]


# ── 画布生成 ────────────────────────────────────────────────────────────────
def test_canvas_contains_all_node_ids():
    res = offload_mod.offload_task("t1", _entries())
    assert res["node_count"] == 3
    canvas = offload_mod.get_canvas("t1")
    assert canvas is not None
    # 全部 node_id 以 [id:...] 形式出现在画布里
    for n in res["nodes"]:
        assert f"[id:{n['node_id']}]" in canvas
    assert canvas.strip().startswith("graph LR")
    # 节点按 --> 串联
    assert " --> " in canvas


def test_canvas_path_and_index_files_on_disk(tmp_path):
    res = offload_mod.offload_task("t1", _entries())
    assert res["canvas_path"]
    # 画布文件真实存在且可读
    assert os.path.isfile(res["canvas_path"])
    # 索引文件落盘且结构正确
    idx_path = offload_mod.index_path("t1")
    assert os.path.isfile(idx_path)
    with open(idx_path, "r", encoding="utf-8") as fh:
        idx = json.load(fh)
    assert set(idx) == {"t1:0", "t1:1", "t1:2"}
    assert idx["t1:0"]["ts"] == "2026-08-21"
    assert idx["t1:0"]["summary"]


# ── 原文落盘 + drill_down 往返 ──────────────────────────────────────────────
def test_original_written_and_drill_down_roundtrip():
    offload_mod.offload_task("t1", _entries())
    # 原文逐条落盘为 md
    ref = offload_mod.node_ref_path("t1", 0)
    assert os.path.isfile(ref)
    with open(ref, "r", encoding="utf-8") as fh:
        assert fh.read() == "分析了 WMS 对标，识别出 3 处缺口。"
    # drill_down 往返一致
    node = offload_mod.drill_down("t1:0")
    assert node is not None
    assert node["content"] == "分析了 WMS 对标，识别出 3 处缺口。"
    assert node["node_id"] == "t1:0"


# ── search 命中 / 不命中 ────────────────────────────────────────────────────
def test_search_hit_and_miss():
    offload_mod.offload_task("t1", _entries())
    hits = offload_mod.search_offload("WMS")
    assert len(hits) == 1
    assert hits[0]["node_id"] == "t1:0"
    assert "WMS" in hits[0]["snippet"]
    # 不命中返回空
    assert offload_mod.search_offload("绝不存在词xyz") == []


def test_search_in_ref_body_and_scoped_by_task():
    offload_mod.offload_task("t1", _entries())
    offload_mod.offload_task("t2", [{"content": "另一处包含 WMS 的独立记录。"}])
    # 全局命中两处
    assert len(offload_mod.search_offload("WMS")) == 2
    # 限定 task 后只命中一个
    hits = offload_mod.search_offload("WMS", task_id="t1")
    assert [h["node_id"] for h in hits] == ["t1:0"]


# ── 摘要：规则模式 / 自带 summary 优先 ────────────────────────────────────────
def test_rule_summary_when_no_llm_and_summary_given_wins():
    # LLM 关闭 → 无自带 summary 的走规则抽取（首句/截断）
    assert offload_mod.rule_summarize("第一句。第二句。") == "第一句。"
    assert offload_mod.rule_summarize("没有标点的一长串内容") == "没有标点的一长串内容"
    res = offload_mod.offload_task("t1", _entries())
    # node 0/2 无 summary → 规则抽取；node 1 用了自带 summary
    by_id = {n["node_id"]: n for n in res["nodes"]}
    assert by_id["t1:0"]["summary"] == "分析了 WMS 对标，识别出 3 处缺口。"
    assert by_id["t1:1"]["summary"] == "输出迁移方案文档"
    assert by_id["t1:2"]["summary"] == "第二步骤，测试通过。"
    # index 中的 summary 与节点一致
    idx = json.load(open(offload_mod.index_path("t1"), "r", encoding="utf-8"))
    assert idx["t1:1"]["summary"] == "输出迁移方案文档"


def test_make_summary_returns_rule_when_llm_disabled():
    assert offload_mod.llm_enabled() is False
    assert offload_mod.make_summary("很长的内容第一句。其余不动") == "很长的内容第一句。"


# ── 容错：损坏 ref / 空输入 ─────────────────────────────────────────────────
def test_drill_down_missing_and_corrupt_ref_returns_none(offload_tmp):
    # 从不存在的 node_id
    assert offload_mod.drill_down("nope:5") is None
    # 造一个非 UTF-8 的损坏 ref 文件 → drill_down 返回 None 不崩
    offload_mod.offload_task("t1", [{"content": "正常"}] * 2)
    ref = offload_mod.node_ref_path("t1", 0)
    assert os.path.isfile(ref)
    with open(ref, "wb") as fh:
        fh.write(b"\xff\xfe\x00\x81garbage\x01")
    assert offload_mod.drill_down("t1:0") is None
    # 其余节点仍可读（隔离）
    assert offload_mod.drill_down("t1:1") is not None


def test_empty_inputs_degrade_gracefully():
    # 空 entries
    res = offload_mod.offload_task("t1", [])
    assert res["node_count"] == 0
    assert os.path.isfile(offload_mod.index_path("t1"))
    # entries 含空 content / 非 dict → 跳过
    res2 = offload_mod.offload_task("t2", [{"content": ""}, "not-a-dict", {"content": "有效"}])
    assert res2["node_count"] == 1
    assert res2["nodes"][0]["node_id"] == "t2:2"
    # search 空 query → 空结果不崩
    assert offload_mod.search_offload("") == []


# ── task 覆盖写 ──────────────────────────────────────────────────────────────
def test_overwrite_same_task_id_rebuilds():
    res1 = offload_mod.offload_task("t1", [{"content": "第一版内容"}] * 2)
    node_ids1 = {n["node_id"] for n in res1["nodes"]}
    assert node_ids1 == {"t1:0", "t1:1"}
    # 重跑同一个 task_id：只写 1 个节点，旧的 1 号 ref 应被清理
    res2 = offload_mod.offload_task("t1", [{"content": "第二版内容"}])
    assert res2["node_count"] == 1
    assert not os.path.exists(offload_mod.node_ref_path("t1", 1))
    # index / 画布也被重建，只含新节点
    idx = json.load(open(offload_mod.index_path("t1"), "r", encoding="utf-8"))
    assert list(idx) == ["t1:0"]
    canvas = offload_mod.get_canvas("t1") or ""
    assert "[id:t1:1]" not in canvas
    assert "[id:t1:0]" in canvas
    # drill_down 只回读新内容
    assert offload_mod.drill_down("t1:0")["content"] == "第二版内容"


# ── 路径安全 ─────────────────────────────────────────────────────────────────
def test_task_id_path_traversal_is_sanitized():
    res = offload_mod.offload_task("../evil", [{"content": "x"}])
    assert res["node_count"] == 1
    node_id = res["nodes"][0]["node_id"]
    # node_id 不含穿越分隔符；文件落在 offload 根之内
    assert not node_id.startswith("..")
    base = offload_mod.offload_root()
    assert base in os.path.abspath(offload_mod.node_ref_path("..", 0))


def test_corrupt_index_search_degrades():
    # 造一个损坏的索引 JSON → search 降级为返回空，不崩
    offload_mod.offload_task("t1", [{"content": "机器人焊接参数已校准。"}])
    with open(offload_mod.index_path("t1"), "w", encoding="utf-8") as fh:
        fh.write("{not valid json")
    assert offload_mod.search_offload("焊接") == []
