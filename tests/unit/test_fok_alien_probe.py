#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""§1013 验收测试：**超时分支的 alien_vocabulary 探测**（索引化 · 词元级）。

被它修掉的真缺陷（2026-09-21 实测）：
  慢路「≤15 个 content ILIKE × 44k 行」在 250ms 预算下**几乎必然超时**，超时分支
  原先硬编码 `unknown_terms=0` ⇒ 依赖该字段的 alien_vocabulary 判据在**新词**上
  永不触发（新词恰是它唯一要抓的对象）⇒ cognitive-eval gap_recall 卡 2/4=0.5
  （门槛 0.75），连续 10 天 PASS=False、/health 长期 degraded。

## 判据（S1 双向，缺一不可）

① **能判绿**：随机 nonce 词 → 探测判"全库零命中"（absent 含该词）；
   库中已有的词 → 不被判零；
② **能判红（判据本身必须先能失败）**：开关 off → 返回 None（旧行为=unknown_terms 0），
   即"没有它这个缺陷就复现"；
③ **测不出来 ≠ 测出来是 0**：查询异常必须返回 None（调用方按旧行为处理），不得静默当 0。

## 语义依据（不是换更松的尺，是换一把**能测出真值**的尺）

实测 32 个真实检索词：**tsv=0 而子串>0 的假零 0 例**（判零方向不弱于慢路）；
反向 5 例（tsv 见到、子串扫不到）——active 面 23,168/26,052 行是 `enc:v1:` 密文，
慢路子串扫描对密文天然失明，tsv 由写入时明文建，反而更全。
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import trinity.brain.metamemory as M


class _Cur:
    """最小游标替身：values 里 None 代表"查不到该词"（零命中）。"""

    def __init__(self, values=None, boom=False):
        self.values = list(values or [])
        self.boom = boom
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((str(sql), params))
        if self.boom:
            raise RuntimeError("模拟词元索引不可用")

    def fetchone(self):
        if not self.values:
            return None
        return self.values.pop(0)


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.rollbacks = 0

    def cursor(self):
        return self._cur

    def rollback(self):
        self.rollbacks += 1


class TestAlienProbeUnit:
    def test_开关默认开启(self, monkeypatch):
        monkeypatch.delenv("TRINITY_FOK_ALIEN_PROBE", raising=False)
        assert M._alien_probe_on() is True, "本探测默认必须开启（否则缺陷原样存在）"

    def test_nonce词被判全库零命中(self):
        nonce = "6ab0b80bzzq"
        cur = _Cur([None])                      # 索引查不到 ⇒ fetchone()=None
        conn = _Conn(cur)
        absent, probed = M._alien_terms_via_tsv(conn, [nonce])
        assert absent == [nonce], absent
        assert probed == 1
        sql = " ".join(s for s, _p in cur.sql)
        assert "content_tsv_zh @@" in sql, "必须走 tsv 的 GIN 索引（慢路子串扫描会超时）"
        assert "LIMIT 1" in sql, "存在性探测必须 LIMIT 1"
        assert conn.rollbacks >= 1, "探测结束必须收尾事务（SET LOCAL 不残留）"

    def test_库里已有的词不被判零(self):
        cur = _Cur([(1,)])                      # 索引查到 ⇒ 不是未知词
        absent, probed = M._alien_terms_via_tsv(_Conn(cur), ["数据库"])
        assert absent == [], absent
        assert probed == 1

    def test_测不出来必须返回None而不是空表(self):
        cur = _Cur(boom=True)
        absent, probed = M._alien_terms_via_tsv(_Conn(cur), ["任意词"])
        assert absent is None, "查询异常必须返回 None（否则'测不出来'会被当成'测出来是 0'）"

    def test_开关off即回旧行为_可判红(self, monkeypatch):
        monkeypatch.setenv("TRINITY_FOK_ALIEN_PROBE", "off")
        before = dict(M._FOK_ALIEN_STATS)
        cur = _Cur([None])
        absent, probed = M._alien_terms_via_tsv(_Conn(cur), ["6ab0b80bzzq"])
        assert absent is None and probed == 0, "关掉开关必须回到旧行为（缺陷可复现=判据能失败）"
        assert M._FOK_ALIEN_STATS["skipped"] == before["skipped"] + 1
        assert not cur.sql, "关闭时不得发起任何查询"

    def test_空候选不发查询(self):
        cur = _Cur([])
        absent, probed = M._alien_terms_via_tsv(_Conn(cur), [])
        assert absent == [] and probed == 0 and not cur.sql


class TestAlienProbeAgainstRealIndex:
    """打真索引的验收（无 PG 时 skip）：这是"判据能判绿"的唯一硬证据。"""

    @staticmethod
    def _pg():
        """真 PG 连接（走**仓内统一入口** `scripts/_pg_std.py`）。返回 (conn, 失败原因)。

        2026-10-06（t26）**修两处同族缺陷**：

        ① **不 merge `refs` 的读法**：原实现自己 `yaml.safe_load(...)` 再取**顶层键**
           `TRINITY_PG_USER/PASSWORD` —— 而 `~/.dsh/.credentials.yaml` 自 2026-09-18 起是
           版本化结构（顶层只有 `version`/`refs`/`records`，真键**缩进在 `refs` 下**）
           ⇒ 顶层取到 `None` ⇒ 口令空串 ⇒ 连接**必失败**（实测 `fe_sendauth: no password supplied`）。
           `scripts/_pg_std.py::pg_creds()` 做了 `{**(raw.get("refs") or {}), **raw}`，
           且 env 优先 —— 是仓内既有的统一入口，故直接改用它。
        ② **假 skip 理由**：原来无论什么原因失败都 `skip("无 PG / 无凭证")`。
           实测本机 **PG 可达、凭证就在 `refs` 下** ⇒ 那个理由是**错的**，而本判据是
           全仓唯一一条"打真 PG 索引"的验收、**从未真正跑过**。
           现在把**真实异常**带进 skip 理由，让"跑不了"这件事不可能被误述。
        """
        try:
            import psycopg2  # noqa: F401 —— 缺驱动要如实报出来
            _sd = os.path.join(ROOT, "scripts")
            if _sd not in sys.path:
                sys.path.insert(0, _sd)
            from _pg_std import pg_connect      # noqa: E402 —— 统一凭据入口（merge refs）
            return pg_connect(), None
        except Exception as exc:  # noqa: BLE001 —— 失败要带原因，不许吞成"无凭证"
            return None, "%s: %s" % (type(exc).__name__, str(exc)[:160])

    def test_真索引上_nonce零命中_已知词命中_且足够快(self, monkeypatch):
        conn, why = self._pg()
        if conn is None:
            import pytest
            pytest.skip("PG 不可达（真实原因：%s）—— 本判据需要真索引，"
                        "**不是**『无 PG / 无凭证』这种笼统理由" % why)
        monkeypatch.setenv("TRINITY_FOK_ALIEN_PROBE", "on")
        nonce = "zz%s" % os.urandom(4).hex()
        t0 = time.time()
        absent, probed = M._alien_terms_via_tsv(conn, [nonce, "记忆"])
        ms = (time.time() - t0) * 1000
        assert absent is not None, "真索引上不得返回 None"
        assert nonce in absent, "随机 nonce 必须判为零命中"
        assert "记忆" not in absent, "库中高频词不得被判零命中"
        assert probed == 2
        assert ms < 2000, "两个词的探测必须在 2s 内（生产预算 250ms 量级）"
        conn.close()
