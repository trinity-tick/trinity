# -*- coding: utf-8 -*-
"""T5：「脱敏存」（medium 档）**端到端**接线判据（2026-10-06）。

动机：本轮的教训是「写好了没接上」——`redact_identifiers()` 在 `trinity/security/`
里存在且有单测，但写路径 `trinity/core/client/_ingestion.py` **从不调用它**，
于是「脱敏存」只是个名义档位。本文件钉住**真的接上了**：

  - 行为：含 PII 的 medium 内容入库后**读回是掩码后的文本**；
  - 反事实一：`TRINITY_SENSITIVE_REDACT=0` ⇒ 逐字回到旧行为（原样落库）；
  - 反事实二：**没有**命中敏感类别的文本（哪怕含手机号）**不得**被掩码（作用域收窄）；
  - 反事实三：high 档仍走拒存（不经过掩码路径）；
  - 结构性：调用点源码里必须出现 `redact_identifiers(`（拔掉接线即红）。

隔离：全部用临时 SQLite 库，不触碰 PG / 常驻服务。
跑法：``python -m pytest tests/unit/test_sensitive_redact_wiring_20261006.py -q``
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trinity.security.sensitive import sensitive_redact_enabled  # noqa: E402

AGENT = "t5-redact-wiring"
PII_TEXT = ("最近有点抑郁，联系方式 13812345678，"
            "身份证 310101199001011234，邮箱 zhang.san@corp.cn。")


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_STORAGE_BACKEND", "sqlite")
    from trinity.core.client import Trinity
    return Trinity(store_path=str(tmp_path), adapter="sqlite", evolution_enabled=False)


def _stored(tmp_path, memory_id):
    from trinity.adapters.sqlite import SQLiteAdapter
    ad = SQLiteAdapter(db_path=str(tmp_path / "trinity_store.db"))
    ad.connect()
    try:
        row = ad.get_memory(memory_id)
    finally:
        ad.disconnect()
    return row or {}


def test_medium_with_pii_is_masked_end_to_end(tmp_path, monkeypatch):
    cli = _client(tmp_path, monkeypatch)
    res = cli.ingest(PII_TEXT, agent_id=AGENT, postprocess=False)
    mid = res.get("memory_id")
    assert mid, res
    got = _stored(tmp_path, mid)["content"]
    assert "13812345678" not in got, "手机号仍原样落库 ⇒ 脱敏档没接上"
    assert "310101199001011234" not in got
    assert "zhang.san@corp.cn" not in got
    assert "抑郁" in got, "非标识符内容不得被改动"
    assert "138********" in got, "G2/D1：保留前 3、去尾号"


def test_redact_can_be_rolled_back_to_verbatim_old_behaviour(tmp_path, monkeypatch):
    """回滚档：`TRINITY_SENSITIVE_REDACT=0` ⇒ **完全不掩码**（= t5 接线之前的形态）。"""
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT", "0")
    cli = _client(tmp_path, monkeypatch)
    res = cli.ingest(PII_TEXT, agent_id=AGENT, postprocess=False)
    got = _stored(tmp_path, res["memory_id"])["content"]
    assert got == PII_TEXT, "回滚开关必须逐字回到旧行为（原样落库）"


def test_scope_category_is_the_precise_rollback_of_g2(tmp_path, monkeypatch):
    """**精确回滚本次 G2 改动**：`SCOPE=category` ⇒ 只有类别命中才掩码（纯 PII 原样）。"""
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT_SCOPE", "category")
    cli = _client(tmp_path, monkeypatch)
    text = "订单号 DO-20260902-1188 的发货联系人手机 13812345678，请仓库核对。"
    res = cli.ingest(text, agent_id=AGENT, postprocess=False)
    got = _stored(tmp_path, res["memory_id"])["content"]
    assert got == text, "SCOPE=category 下纯 PII 必须原样落库（G2 之前的行为）"


def test_medium_without_pii_is_untouched(tmp_path, monkeypatch):
    cli = _client(tmp_path, monkeypatch)
    text = "最近有点抑郁，睡得不好。"
    res = cli.ingest(text, agent_id=AGENT, postprocess=False)
    got = _stored(tmp_path, res["memory_id"])["content"]
    assert got == text


def test_scope_all_pii_masks_non_category_text(tmp_path, monkeypatch):
    """G2 扩范围的正例：**没命中敏感类别**但含 PII ⇒ 也掩码（默认 all_pii）。"""
    cli = _client(tmp_path, monkeypatch)
    text = "订单号 DO-20260902-1188 的发货联系人手机 13812345678，请仓库核对。"
    res = cli.ingest(text, agent_id=AGENT, postprocess=False)
    got = _stored(tmp_path, res["memory_id"])["content"]
    assert got != text and "138********" in got
    assert "订单号 DO-20260902-1188" in got, "非 PII 部分逐字不变"


def test_high_severity_still_refused_not_masked(tmp_path, monkeypatch):
    cli = _client(tmp_path, monkeypatch)
    res = cli.ingest("我想自杀，活着太累了。联系人 13812345678。",
                     agent_id=AGENT, postprocess=False)
    assert res.get("error") == "policy_refused_sensitive"
    assert res.get("memory_id", "") == ""


def test_switch_default_is_on_and_off_value_works(monkeypatch):
    monkeypatch.delenv("TRINITY_SENSITIVE_REDACT", raising=False)
    assert sensitive_redact_enabled() is True
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT", "0")
    assert sensitive_redact_enabled() is False
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT", "off")
    assert sensitive_redact_enabled() is False


def test_call_site_really_invokes_redaction():
    """结构性反事实：拔掉接线（删掉调用）本判据即红。"""
    src = (ROOT / "trinity/core/client/_ingestion.py").read_text(encoding="utf-8")
    assert "redact_identifiers(" in src, "写路径没有调用 redact_identifiers ⇒ 脱敏档未接线"
    assert "sensitive_redact_enabled()" in src, "写路径没有读取回滚开关"


# ── 追加（队长要求）：不可逆变换必须**可查** —— 计数 + 判据 ────────────────
# 理由：medium 档掩码**默认 on 且不可还原**；判据只能证明"作用域正确"，
# 证明不了"生产语料上的实际掩码量级"。故必须有计数，且"掩码发生 ⇒ 计数 +1"。
def test_redact_counter_increments_on_mask():
    from trinity.security.sensitive import redact_identifiers, redact_stats, reset_redact_stats
    reset_redact_stats()
    assert redact_stats()["redacted_total"] == 0
    # 反事实：未命中标识符的文本**不得**推高计数
    redact_identifiers("最近有点抑郁，睡得不好。")
    assert redact_stats()["redacted_total"] == 0
    # 正例：发生掩码 ⇒ +1，并记下被掩码的标识符**个数**与种类
    redact_identifiers("联系方式 13812345678，身份证 310101199001011234，邮箱 a@b.com。")
    st = redact_stats()
    assert st["redacted_total"] == 1, st
    assert st["redacted_identifiers_total"] == 3, st
    assert set(st["by_kind"]) == {"手机号", "身份证号", "邮箱"}, st
    # 再掩码一次 ⇒ 再 +1（累计语义）
    redact_identifiers("联系人 13900001111。")
    assert redact_stats()["redacted_total"] == 2
    reset_redact_stats()


def test_redact_counter_end_to_end_and_scope(tmp_path, monkeypatch):
    """端到端：写路径发生掩码 ⇒ 计数 +1；回滚档**不得**推高计数（G2 起纯 PII 也计入）。"""
    from trinity.security.sensitive import redact_stats, reset_redact_stats
    reset_redact_stats()
    cli = _client(tmp_path, monkeypatch)
    cli.ingest(PII_TEXT, agent_id=AGENT, postprocess=False)
    assert redact_stats()["redacted_total"] == 1
    # G2：含手机号但**未命中敏感类别** ⇒ **也会掩码**（这是扩范围本身）⇒ 计数 +1
    cli.ingest("订单号 DO-1 的发货联系人手机 13812345678，请核对。", agent_id=AGENT,
               postprocess=False)
    assert redact_stats()["redacted_total"] == 2
    assert redact_stats()["redacted_pii_only_total"] == 1, "因 PII（非类别）而掩码要单独可读"
    # 反事实：回滚档（开关关）⇒ 不掩码 ⇒ 计数不动
    monkeypatch.setenv("TRINITY_SENSITIVE_REDACT", "0")
    cli.ingest(PII_TEXT, agent_id=AGENT, postprocess=False)
    assert redact_stats()["redacted_total"] == 2
    reset_redact_stats()


def test_redact_counter_is_exposed_on_api_metrics():
    """`/metrics` 可查（resident API 进程内）：掩码后注册表里必须出现该指标。"""
    import sys as _sys
    from trinity.security.sensitive import redact_identifiers, reset_redact_stats
    import trinity.api.middleware as _mw  # 本进程显式加载 API 层，模拟 resident API
    assert "trinity.api.middleware" in _sys.modules
    reset_redact_stats()
    redact_identifiers("联系方式 13812345678。")
    text = _mw.get_metrics().render()
    assert "trinity_sensitive_redacted_total" in text, text[-400:]
    assert "trinity_sensitive_redacted_by_kind_total" in text and 'kind="手机号"' in text
    reset_redact_stats()


def test_metrics_bridge_does_not_import_api_package_by_itself():
    """零副作用反事实：本进程若从未加载 API 层，掩码**不得**把 `trinity.api` 拖进来。

    （否则一次掩码会在一切离线脚本里触发 `trinity.api` 包导入 → GraphQL 重依赖。）
    """
    import os as _os
    import subprocess
    code = (
        "import sys; sys.path.insert(0, r'%s');\n"
        "from trinity.security.sensitive import redact_identifiers, redact_stats;\n"
        "assert 'trinity.api' not in sys.modules, '预置失败：进程已加载 API 层';\n"
        "redact_identifiers('联系方式 13812345678。');\n"
        "assert redact_stats()['redacted_total'] == 1;\n"
        "print('API_LOADED=' + str('trinity.api' in sys.modules))\n"
    ) % str(ROOT)
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**_os.environ, "TRINITY_MEMORY_ENABLED": "0"})
    assert r.returncode == 0, (r.stdout, (r.stderr or "")[-400:])
    assert "API_LOADED=False" in (r.stdout or ""), r.stdout
