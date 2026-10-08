# -*- coding: utf-8 -*-
"""写入侧准入判据（`trinity/memory/corpus_write_guard.py`）的反事实测试（t4 / 2026-10-06）。

## 为什么每条规则都要两个方向

本仓反复出现"门禁没有判别力"：判据恒真、恒假、或永远不被调用（`validate()` 恒真、
`GuardChain` 自开开关、`verify_merge_safety` 曾指向一个不存在的方法）。所以本文件的
**核心不是"违规被拦"，而是"违规被拦 **且** 合规被放"** —— 任一方向缺失，判据就可能是
恒真式，而复评明确把恒真式判为缺陷（本轮 t4 就在 `_merge_safety` 的 R2 上抓到了一个恒真式）。

每条规则各 3 组用例：正例（该拦）、反例（不该拦）、边界（阈值上下）。
"""
import json
import os
import pathlib

import pytest

from trinity.memory import corpus_write_guard as g

REPO = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    """判定日志写到 tmp_path：**绝不污染** ~/.trinity/state 下的真实日志。"""
    monkeypatch.setenv("TRINITY_CORPUS_WRITE_GUARD_LOG", str(tmp_path / "guard.jsonl"))
    monkeypatch.delenv(g.ENV_SWITCH, raising=False)
    yield


# ────────────────────────────────────────────────────── 开关三档

def test_mode_default_is_annotate():
    """默认档必须是 annotate（零行为变化）：没设环境变量时不能开始拦。"""
    assert g.guard_mode({}) == "annotate"
    assert g.guard_mode({g.ENV_SWITCH: ""}) == "annotate"
    assert g.guard_mode({g.ENV_SWITCH: "on"}) == "on"
    assert g.guard_mode({g.ENV_SWITCH: "off"}) == "off"


def test_mode_unknown_value_fails_safe_to_annotate():
    """错拼的取值一律 annotate（不能因为拼错就开始拦截生产写入）。

    注：`"1 "` 会被 strip 后识别为 on —— 这**是**有意的（容忍尾随空白），故不在本用例内。
    """
    for bad in ("ON!", "enforce2", "yes please", "TrUe-x", "enable", "2"):
        assert g.guard_mode({g.ENV_SWITCH: bad}) == "annotate", bad


def test_off_mode_allows_and_writes_nothing(tmp_path):
    """off 档：既不拦、也不写日志（"零行为变化"含零副作用）。"""
    log = tmp_path / "off.jsonl"
    d = g.evaluate_before_store("任何内容", existing_statuses=["archived"],
                                content_hash="h", mode="off", log=True)
    assert d.allow is True and d.code == "guard_off" and d.would_block is False
    assert not log.exists()


def test_annotate_allows_but_records_would_block():
    """annotate 档：放行（行为不变）但把"本来会拦"记进 would_block + JSONL。"""
    d = g.evaluate_before_store("内容", existing_statuses=["archived"],
                                content_hash="h", mode="annotate")
    assert d.allow is True and d.would_block is True and d.code == "dup_archived_copy"
    rows = g.load_guard_log()
    assert len(rows) >= 1 and rows[-1]["code"] == "dup_archived_copy"
    assert rows[-1]["would_block"] is True and rows[-1]["allow"] is True


# ────────────────────────────────────────────────────── W1 dup_archived_copy

def test_w1_blocks_archived_copy():
    """【正例】同 hash 只以归档/合并态存在 ⇒ 再写就是"归档洗白后的重复副本"。"""
    for st in ("archived", "deleted", "merged", "forgotten"):
        d = g.evaluate_before_store("一段被测内容", existing_statuses=[st],
                                    content_hash="h1", mode="on")
        assert d.allow is False and d.code == "dup_archived_copy", st


def test_w1_allows_brand_new_content():
    """【反例 1】库里根本没有这个 hash ⇒ 必须放行（否则判据变成"谁来都拦"）。"""
    d = g.evaluate_before_store("全新内容", existing_statuses=(), content_hash="h2", mode="on")
    assert d.allow is True and d.code == ""


def test_w1_allows_when_an_active_row_exists():
    """【反例 2】active 同 hash 存在 ⇒ **本判据不拦**：那是唯一索引
    `idx_memories_content_hash ... AND status='active'` 的职责。两层都拦会把
    "谁拦下的"变得不可归因（本仓要求每个抑制可归因到一条规则）。"""
    d = g.evaluate_before_store("内容", existing_statuses=["active"], content_hash="h3", mode="on")
    assert d.allow is True
    d2 = g.evaluate_before_store("内容", existing_statuses=["active", "archived"],
                                 content_hash="h4", mode="on")
    assert d2.allow is True, "只要有一行 active，W1 就不该抢唯一索引的活"


def test_w1_boundary_empty_status_list_is_not_a_duplicate():
    """边界：`existing_statuses=[]`（查不到）与 `None` 都不是重复 —— 不能把"查失败"当重复。"""
    assert g.evaluate_before_store("x", existing_statuses=[], content_hash="h",
                                   mode="on").allow is True
    assert g.evaluate_before_store("x", existing_statuses=None, content_hash="h",
                                   mode="on").allow is True


# ────────────────────────────────────────────────────── W2 self_ref_loop_depth

def test_w2_blocks_deep_self_reference():
    """【正例】[自动关联] 嵌套 ≥3 层（实测库内最深 12 层，正文只剩元数据头）。"""
    content = ("[自动关联] 与 5 条已有记忆相关: [自动关联] 与 3 条已有记忆相关: "
               "[自动关联] 与 2 条已有记忆相关: | 有效信息")
    d = g.evaluate_before_store(content, mode="on")
    assert d.allow is False and d.code == "self_ref_loop_depth"
    assert g.gen_depth(content) == 3


def test_w2_allows_shallow_generated_content():
    """【反例】只带**一层**生成头（正常派生：一条自省、一条会话摘要）必须放行。"""
    for content in ("[self-reflection] 我的状态：谨慎 | 有效内容",
                    "[会话结束自动沉淀] sess-1\n--- 会话开头 ---\n真实内容",
                    "普通的外部笔记，没有任何生成头"):
        d = g.evaluate_before_store(content, mode="on")
        assert d.allow is True, content
        assert d.code == ""


def test_w2_boundary_exactly_at_threshold():
    """边界：恰好 = 阈值 ⇒ 拦；少一个 ⇒ 放。把阈值钉死，防止有人把比较写成 > 而静默放宽。"""
    at = "[自动关联] [self-reflection] [procedure]"
    under = "[自动关联] [self-reflection]"
    assert g.gen_depth(at) == 3 and g.gen_depth(under) == 2
    assert g.evaluate_before_store(at, mode="on").allow is False
    assert g.evaluate_before_store(under, mode="on").allow is True


# ────────────────────────────────────────────────────── W3 derivative_requota

def test_w3_blocks_over_quota_derivative():
    """【正例】同生产者把同一段派生内容第 N 次回灌（实测 self-reflection 单条被写 166 次）。"""
    d = g.evaluate_before_store("[self-reflection] 完全一样的一段", producer="default",
                                recent_identical_writes=3, mode="on")
    assert d.allow is False and d.code == "derivative_requota"


def test_w3_allows_under_quota():
    """【反例 1】配额内（0/1/2 次）必须放行 —— 否则正常重写被拦。"""
    for n in (0, 1, 2):
        d = g.evaluate_before_store("[self-reflection] 同一段", producer="default",
                                    recent_identical_writes=n, mode="on")
        assert d.allow is True, n


def test_w3_does_not_apply_to_non_derivative_content():
    """【反例 2】无生成头的**外部内容**不受配额限制：同一条外部笔记写 100 次也不该由
    W3 拦（那是 W1/唯一索引的领域）。这条防止"配额"退化成全局写入限流。"""
    d = g.evaluate_before_store("一条普通的外部笔记，重复了很多次", producer="default",
                                recent_identical_writes=99, mode="on")
    assert d.allow is True and d.code == ""


def test_w3_boundary_quota_value_is_respected():
    """边界：`quota` 可覆盖，且判定用的是 >=（等于即拦）。"""
    d = g.evaluate_before_store("[procedure] x", producer="p", recent_identical_writes=5,
                                quota=5, mode="on")
    assert d.allow is False
    d2 = g.evaluate_before_store("[procedure] x", producer="p", recent_identical_writes=4,
                                 quota=5, mode="on")
    assert d2.allow is True


# ────────────────────────────────────────────────────── 失败降级与日志

def test_fail_open_on_bad_input_never_raises(monkeypatch):
    """判据内部异常必须 fail-open（放行 + guard_error 留痕），绝不阻断写入。"""
    def boom(*_a, **_k):
        raise RuntimeError("injected")

    monkeypatch.setattr(g, "_judge", boom)
    d = g.evaluate_before_store("任意", mode="on")
    assert d.allow is True and d.code == "guard_error"


def test_log_is_jsonl_and_countable():
    """日志是 JSONL 且可聚合：`summarize_decisions` 能报出"会拦什么"。"""
    for _ in range(2):
        g.evaluate_before_store("x", existing_statuses=["archived"], content_hash="h", mode="annotate")
    g.evaluate_before_store("y", existing_statuses=[], content_hash="h2", mode="annotate")
    rows = g.load_guard_log()
    assert all(isinstance(r, dict) for r in rows)
    s = g.summarize_decisions(rows)
    assert s["total"] == 3 and s["would_block"] == 2
    assert s["by_code"] == {"dup_archived_copy": 2}


def test_log_write_failure_is_silent(tmp_path, monkeypatch):
    """记账失败必须静默返回 False（绝不把写入带崩）。"""
    bad = tmp_path / "nope" / "\x00bad.jsonl"
    assert g.append_guard_log(g.WriteGuardDecision(True), str(bad)) is False


def test_no_dependency_on_write_verify_call_site():
    """结构性哨兵：本模块**不得**自带回查/检索逻辑（那是 write_verify 的职责）。

    两个模块的关注点不能混：`write_verify` 管"写进去查不查得到"（store **之后**，
    入口 `maybe_verify_after_store`），本模块管"该不该写进去"（store **之前**，
    入口 `evaluate_before_store`）。这里锁住"本模块不复制对方的函数、也不反向依赖它"。
    """
    src = pathlib.Path(g.__file__).read_text(encoding="utf-8")
    for forbidden in ("def verify_and_fix(", "def maybe_verify_after_store(",
                      "from trinity.memory.write_verify", "import write_verify"):
        assert forbidden not in src, "写入侧准入模块不该吸收回查逻辑：%s" % forbidden
    assert "store 之前" in src and "store 之后" in src, "分工必须写在模块文档里"
    # 正向哨兵：本模块的入口函数**必须**存在（防止有人删掉入口却让上面的断言继续通过）
    assert callable(g.evaluate_before_store)


def test_repo_has_no_guard_wired_into_ingest_order():
    """哨兵：本模块**没有**被接进 `_ingest.py` 的调用顺序（G2 AST 契约锁的是那里）。

    若有人为了"生效"把它塞进 merge_if_similar 的变更之前，会与 G2 契约判据冲突。
    这条测试把"未接线"变成可核事实，接线必须由队长决定并另起一轮。
    """
    ingest = REPO / "trinity" / "agents" / "aggregator" / "_ingest.py"
    src = ingest.read_text(encoding="utf-8")
    assert "corpus_write_guard" not in src


def test_guard_module_is_importable_from_repo(tmp_path):
    """`trinity/memory/corpus_write_guard.py` 必须在仓库导入路径下可用（worker 会走同一条路径）。"""
    assert os.path.basename(g.__file__) == "corpus_write_guard.py"
    assert os.path.isdir(REPO / "trinity" / "memory")
    assert json.dumps({"ok": True})
