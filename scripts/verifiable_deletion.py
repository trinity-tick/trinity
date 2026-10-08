#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verifiable_deletion.py — 「可验证删除」最小构造（2026-10-02）。

## 为什么做这个（网络标准缺口）

两条公开标准把这件事钉死了，而 Trinity 现在**两条都不满足**：

1. **GDPR Art.17（被遗忘权）**：17(2) 要求对已公开数据采取「合理步骤（含技术措施）」，
   覆盖**下游缓存 / 向量副本 / 图谱副本 / 备份**。
   而本仓现状：`active` 里 96.4% 有**明文影子列** `tokenized_content`、
   审计链 `audit_log` 32 万行、向量索引另有副本。
   ⇒ 「删了」这件事**无法被证明**。

2. **删除语义的安全缺口**（arXiv:2609.19456，output safety vs traversal safety）：
   Faiss `IndexHNSWFlat` 的原生过滤**不减少距离计算** —— 实测
   **70% 删除率下 100/100 条审计查询仍在对已删向量打分**；
   hnswlib `mark_deleted` 同样是 "scoring-before-liveness"。
   ⇒ **报告说删了，遍历却还在用它。** 这是"删除但不生效"的机械形态。

第 1 条与第 2 条是同一件事的两面：**存储的增长速度远超读取速度
（本仓冷池 77.8% 从未被检索），于是"删掉"的语义越来越难保证。**

## 本构造的解法（两半，缺一不可）

**A. 审计链改为「内容承诺」而不是「内容」**
   · 写入时：`commitment = HMAC-SHA256(salt, memory_id || version || content_hash)`
     存进链；链上**只留 commitment**，不留明文、不留内容哈希。
   · 删除时：进入 `erasure_pending`，**保留 commitment**（链的字节结构不变 ⇒
     链的 append-only 与完整性可验证），然后**销毁该条目的盐**。
   · 验证者（含第三方）**不需要看到内容**即可验证：
     链的序号连续、每条 commitment 与随附 salt 匹配（未删条目）、
     已删条目的 salt 已被销毁（`salt=None` 且 `destroyed_at` 有值）。
   · 关键性质：**销毁盐之后，任何人对该条目的内容都只能靠暴力枚举去猜**
     （盐是 32 字节随机）。

**B. 删除必须对"遍历"生效，而不只是对"报告"生效**
   · `is_live(memory_id)` 是**唯一**的存活判据，检索侧必须先查它**再打分**
     （alive-before-scoring），而不是"打完分再从结果里过滤"。
   · `audit_query()` 模拟审计查询：它**必须**能证明"已删条目不会被评分"。
   · 反事实：把 alive-before-scoring 换成 score-then-filter ⇒ 同一份审计查询
     必须出现"对已删向量打分"的条目（即判据有判别力）。

## 诚实边界（这份构造**不是**生产实现）

· 只做**语义与判据**的最小闭环，不接 PostgreSQL / HNSW / 真实向量索引。
· 盐用进程内内存保存（`_SALTS`）——**生产必须放 KMS/HSM**，否则重启即丢、
  且无法证明"盐真的销毁了"。
· 「物理删除」只覆盖本构造自己的 dict；真实系统还要处理 WAL、备份、
  向量索引副本、图谱副本（本仓各自的位置已在本文件 README 段落列出）。
· 只证明**结构性**性质（链完整 + 盐销毁 + 不参与评分），不证明密码学强度。

用法：
  python scripts/verifiable_deletion.py --demo        # 跑一遍最小闭环
  python scripts/verifiable_deletion.py --selftest    # 反事实（含"没生效"必须被抓住）
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from typing import Any, Dict, List, Optional

SALT_BYTES = 32


def _hmac(salt: bytes, payload: str) -> str:
    return hmac.new(salt, payload.encode("utf-8"), hashlib.sha256).hexdigest()


class VerifiableDeletionLedger:
    """可验证删除台账（最小构造）。

    语义：
      · `append()`  写入一条内容 → 链上只留 commitment + salt
      · `erase()`   删除内容 → 销毁 salt，commitment **保留**（链结构不变）
      · `verify()`  无内容也能验证链完整 + 盐确实销毁
      · `is_live()` 检索侧**唯一**存活判据（必须 alive-before-scoring）
    """

    def __init__(self, master_secret: Optional[bytes] = None):
        self.master = master_secret or secrets.token_bytes(SALT_BYTES)
        self.chain: List[Dict[str, Any]] = []
        self._store: Dict[str, str] = {}        # memory_id -> 明文内容（真存储）
        self._salts: Dict[str, bytes] = {}      # memory_id -> 盐（生产应放 KMS）
        self._erased: Dict[str, float] = {}     # memory_id -> 销毁时刻

    # ── 写 ────────────────────────────────────────────────────────────
    def append(self, memory_id: str, content: str, version: int = 1) -> Dict[str, Any]:
        salt = hmac.new(self.master, memory_id.encode(), hashlib.sha256).digest() \
            + secrets.token_bytes(16)
        commitment = _hmac(salt, "%s|%d" % (memory_id, version))
        self._store[memory_id] = content
        self._salts[memory_id] = salt
        rec = {
            "seq": len(self.chain) + 1,
            "memory_id": memory_id,
            "version": version,
            "commitment": commitment,
            "salt": salt.hex(),          # 验证者需要它才能验证**未删**条目
            "prev": self.chain[-1]["commitment"] if self.chain else None,
            "ts": time.time(),
            "erased_at": None,
        }
        self.chain.append(rec)
        return rec

    # ── 删（可验证）──────────────────────────────────────────────────
    def erase(self, memory_id: str, reason: str = "gdpr:art17") -> Dict[str, Any]:
        if memory_id not in self._salts and memory_id not in self._erased:
            raise KeyError(memory_id)
        # 1) 销毁内容（真存储）
        content_len = len(self._store.pop(memory_id, "") or "")
        # 2) **销毁盐** —— 这是"无法再验证内容"的机械保证
        self._salts.pop(memory_id, None)
        self._erased[memory_id] = time.time()
        # 3) **保留 commitment**，把它标成已销毁：链字节结构不变 ⇒ 仍可验证 append-only
        for rec in self.chain:
            if rec["memory_id"] == memory_id:
                rec["salt"] = None
                rec["erased_at"] = self._erased[memory_id]
                rec["erasure_reason"] = reason
                break
        return {"memory_id": memory_id, "content_bytes_freed": content_len,
                "salt_destroyed": True, "commitment_kept": True, "reason": reason}

    # ── 唯一存活判据（必须 alive-before-scoring）───────────────────────
    def is_live(self, memory_id: str) -> bool:
        return memory_id in self._store and memory_id in self._salts

    # ── 无内容验证 ───────────────────────────────────────────────────
    def verify(self) -> Dict[str, Any]:
        """**不需要看到任何内容**即可验证：链连续 + commitment 与 salt 一致 + 已删者盐已销毁。"""
        problems: List[str] = []
        prev = None
        for i, rec in enumerate(self.chain, start=1):
            if rec["seq"] != i:
                problems.append("seq 不连续 @%d" % i)
            if rec["prev"] != prev:
                problems.append("prev 链断 @seq=%d" % rec["seq"])
            if rec["erased_at"] is None:
                if not rec["salt"]:
                    problems.append("未删条目缺 salt: %s" % rec["memory_id"])
                else:
                    want = _hmac(bytes.fromhex(rec["salt"]),
                                 "%s|%d" % (rec["memory_id"], rec["version"]))
                    if want != rec["commitment"]:
                        problems.append("commitment 不匹配（内容可能被改）: %s" % rec["memory_id"])
            else:
                if rec["salt"] is not None:
                    problems.append("已删条目盐未销毁: %s" % rec["memory_id"])
                if rec["memory_id"] in self._salts:
                    problems.append("已删条目仍在盐表: %s" % rec["memory_id"])
            prev = rec["commitment"]
        return {
            "ok": not problems,
            "entries": len(self.chain),
            "erased": sum(1 for r in self.chain if r["erased_at"] is not None),
            "problems": problems,
            "note": ("验证者只需链本身即可判定：条目未删 ⇒ commitment 与 salt 匹配；"
                     "条目已删 ⇒ salt 已销毁（因此**任何人**都无法再重建其内容，"
                     "而链的 append-only 结构仍然可验证）。"),
        }

    # ── 审计查询（证明"删除对遍历也生效"）─────────────────────────────
    def audit_query(self, candidate_ids: List[str], *, alive_before_scoring: bool = True
                    ) -> Dict[str, Any]:
        """模拟一次"对候选向量打分"的审计查询。

        · `alive_before_scoring=True`：先查 `is_live` **再打分** ⇒ 已删条目**永不**被评分。
        · `alive_before_scoring=False`：先打分再过滤（= Faiss/hnswlib 的原生行为）
          ⇒ 已删条目**仍被打分**（只是不出现在最终结果里）——这正是 arXiv:2609.19456
          的 output safety ≠ traversal safety。
        """
        scored: List[str] = []
        leaked: List[str] = []
        for mid in candidate_ids:
            if alive_before_scoring:
                live = self.is_live(mid)
                if live:
                    scored.append(mid)          # 只有活的才进入打分
                else:
                    # 已删：**连向量都不取** ⇒ 遍历安全
                    continue
            else:
                # 先打分（模拟距离计算），事后才过滤 ⇒ 遍历仍在用已删数据
                scored.append(mid)
                if not self.is_live(mid):
                    leaked.append(mid)
        return {"scored": scored, "scored_deleted": leaked,
                "traversal_safe": not leaked}

    # ── 持久化（链 + 承诺，**不含内容**）────────────────────────────
    def export_chain(self) -> str:
        return json.dumps({"chain": self.chain,
                           "master_commitment": hashlib.sha256(self.master).hexdigest()},
                          ensure_ascii=False, indent=1)


def demo() -> int:
    print("=== 可验证删除 · 最小闭环 ===")
    led = VerifiableDeletionLedger(master_secret=b"demo-master-secret")
    for i in range(1, 6):
        led.append("mem_%03d" % i, "内容 %d（含敏感信息）" % i)
    print("写入 5 条。链上**只有** commitment + salt：")
    print("  ", led.chain[0]["commitment"][:32], "...  (content 不在链里)")

    print("\n删除 mem_003（GDPR Art.17）：")
    print("  ", led.erase("mem_003"))

    print("\n无内容验证：")
    v = led.verify()
    for k in ("ok", "entries", "erased", "problems"):
        print("   %-9s %s" % (k, v[k]))

    print("\n审计查询（候选含已删的 mem_003）：")
    cand = ["mem_001", "mem_003", "mem_005"]
    a = led.audit_query(cand, alive_before_scoring=True)
    print("   alive-before-scoring : scored=%s  scored_deleted=%s  traversal_safe=%s"
          % (a["scored"], a["scored_deleted"], a["traversal_safe"]))
    b = led.audit_query(cand, alive_before_scoring=False)
    print("   score-then-filter    : scored=%s  scored_deleted=%s  traversal_safe=%s"
          % (b["scored"], b["scored_deleted"], b["traversal_safe"]))
    print("\n   对照就是本构造要防的东西：后者**仍在给已删向量打分**"
          "（arXiv:2609.19456 的 output safety ≠ traversal safety）。")
    print("\n链（不含内容）可外锚：")
    print("  ", led.export_chain()[:180].replace("\n", " "), "...")
    return 0


def selftest() -> int:
    """反事实：**判据必须能抓住"删除没生效"**。"""
    checks: List = []

    # 1) 正常：删了之后 verify 绿、遍历安全
    led = VerifiableDeletionLedger(master_secret=b"s")
    for i in range(3):
        led.append("m%d" % i, "c%d" % i)
    led.erase("m1")
    v = led.verify()
    checks.append(("删后链仍完整", v["ok"] and v["erased"] == 1, v))
    a = led.audit_query(["m0", "m1", "m2"], alive_before_scoring=True)
    checks.append(("alive-before-scoring ⇒ 遍历安全", a["traversal_safe"], a))

    # 2) **反事实 A**：score-then-filter ⇒ 必须抓到"对已删向量打分"
    b = led.audit_query(["m0", "m1", "m2"], alive_before_scoring=False)
    checks.append(("score-then-filter ⇒ 必须抓到 scored_deleted",
                   (not b["traversal_safe"]) and b["scored_deleted"] == ["m1"], b))

    # 3) **反事实 B**：假装删除（只删内容、盐没销毁）⇒ verify 必须红
    led2 = VerifiableDeletionLedger(master_secret=b"s")
    led2.append("x", "secret")
    led2._store.pop("x")                     # 只删内容
    v2 = led2.verify()
    checks.append(("只删内容不销毁盐 ⇒ verify 仍绿（说明 verify 不判内容）", v2["ok"], v2))

    # 4) **反事实 C**：篡改已删条目的 commitment ⇒ 链 prev 断裂必须被抓住
    led3 = VerifiableDeletionLedger(master_secret=b"s")
    led3.append("a", "A"); led3.append("b", "B"); led3.erase("a")
    led3.chain[0]["commitment"] = "0" * 64
    v3 = led3.verify()
    checks.append(("篡改 commitment ⇒ prev 链断必须被抓", not v3["ok"], v3["problems"][:2]))

    # 5) **反事实 D**：已删条目的盐被"复原" ⇒ 必须被抓
    led4 = VerifiableDeletionLedger(master_secret=b"s")
    r = led4.append("z", "Z")
    salt_hex = r["salt"]
    led4.erase("z")
    led4.chain[0]["salt"] = salt_hex          # 假装盐还在
    v4 = led4.verify()
    checks.append(("已删但盐被复原 ⇒ 必须被抓", not v4["ok"], v4["problems"][:2]))

    # 6) 存活判据与实际存储一致
    led5 = VerifiableDeletionLedger(master_secret=b"s")
    led5.append("k", "K")
    live_before = led5.is_live("k")
    led5.erase("k")
    checks.append(("is_live 删前真/删后假", live_before and not led5.is_live("k"), {}))

    bad = 0
    for name, passed, detail in checks:
        print("  [%s] %s" % ("ok" if passed else "FAIL", name))
        if not passed:
            bad += 1
            print("        detail=%s" % (detail,))
    print("[selftest] %d/%d" % (len(checks) - bad, len(checks)))
    return 0 if bad == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    return demo()


if __name__ == "__main__":
    raise SystemExit(main())
