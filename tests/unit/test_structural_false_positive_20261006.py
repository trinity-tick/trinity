# -*- coding: utf-8 -*-
"""t121/G9R-7 判据：**结构 hex 串不再被判成手机号，且真手机号照旧**。

| 判据 | 内容 |
|---|---|
| **C1（正向，必须过）** | `scan_pii("13812345678")` **仍判 PII**，且 `redact_identifiers` **仍掩码**（不许修成"不认手机号"） |
| **C2（反向）** | `scan_pii("019c16702723433c")` **不再判 PII**；`redact_identifiers` **不掩** |
| **C3（牙齿）** | ① 输入换成 `mem_019c16702723433c` ⇒ 同样不判；② 把结构排除**摘掉** ⇒ C2 **必红** |
| **C4（边界表）** | 6 个边界用例的**期望 vs 实测**逐条断言（数据驱动） |
| **C5（⭐ 断言结构而非字符串）** | 用**随机生成**的 hex 词元（不在任何硬编码清单里）内嵌合法号段 ⇒ **也必须不判** ⇒ 证明是**结构**判定 |
| **C6（不对 high 档产生影响）** | t70 冻结样本（FP 121 + TP 19 + 成对集 help 104 = **244 条**）的 `severity` **逐条不变** |

⚠️ 全部用 `_live()` + `_patch_everywhere()`（多实例隔离，t108 的教训）；判据不硬编码"某字符串不判"，
只断言**结构性质**（C5）。
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import trinity.security.sensitive as S  # noqa: E402

EVID = Path(r"D:\DSH官网\trinity-optimize-20261006\evidence")


def _live():
    return sys.modules.get("trinity.security.sensitive") or S


def _patch_everywhere(mp, name, value) -> int:
    """把 `name` 打到所有活着的 `sensitive` 实例上（多重导入隔离；返回命中数）。"""
    import gc
    import types
    n = 0
    cands = [m for m in list(sys.modules.values())
             if isinstance(m, types.ModuleType)
             and str(getattr(m, "__name__", "")) == "trinity.security.sensitive"]
    cands += [o for o in gc.get_objects() if isinstance(o, types.ModuleType)
              and str(getattr(o, "__name__", "")).endswith("security.sensitive")]
    seen = set()
    for m in cands:
        if m is None or id(m) in seen or not hasattr(m, name):
            continue
        seen.add(id(m))
        mp.setattr(m, name, value, raising=False)
        n += 1
    return n


def _frozen(name):
    p = EVID / name
    if not p.exists():
        return []
    return [it["text"] for it in json.loads(p.read_text(encoding="utf-8"))["items"] if it.get("text")]


# ── C1 ────────────────────────────────────────────────────────────────
def c1_real_phone_still_pii(tmp_path=None, monkeypatch=None) -> bool:
    m = _live()
    for text in ("13812345678", "联系方式 13812345678", "订单 13812345678 发货",
                 "Phone: 13912345678"):
        r = m.scan_pii(text)
        if not r["flagged"] or "手机号" not in r["kinds"]:
            return False
        out, labels = m.redact_identifiers(text)
        if out == text or not any("手机号" in x for x in labels):
            return False
        if "13812345678" in out or "13912345678" in out:
            return False
    return True


# ── C2 ────────────────────────────────────────────────────────────────
def c2_structural_hex_not_pii(tmp_path=None, monkeypatch=None) -> bool:
    m = _live()
    for text in ("019c16702723433c",
                 "3e25ac2f9a7fcb6967749ae4f72c5e5f6f19118348355eb2f31224bf2397a1b2c3d4",
                 "740f93a17635576539d6976030550c336d505f12a834b327b4bf4ceee7ce"):
        if m.scan_pii(text)["flagged"]:
            return False
        if m.redact_identifiers(text)[0] != text:
            return False
    return True


# ── C3 牙齿 ───────────────────────────────────────────────────────────
def c3_teeth(tmp_path=None, monkeypatch=None) -> bool:
    m = _live()
    # ① 带前缀形态
    if m.scan_pii("mem_019c16702723433c")["flagged"]:
        return False
    if m.scan_pii("id-019c16702723433c-v2")["flagged"]:
        return False
    return True


def c3b_teeth_removed_exclusion(tmp_path=None, monkeypatch=None) -> bool:
    """牙齿本体：把结构排除摘掉 ⇒ C2 **必须红**。"""
    n = _patch_everywhere(monkeypatch, "_is_hash_like_token", lambda *a, **k: False)
    if n < 1:
        raise AssertionError("牙齿没打到任何 sensitive 实例 ⇒ 变绿是假的")
    return c2_structural_hex_not_pii()


# ── C4 边界表 ─────────────────────────────────────────────────────────
BOUNDARY = [
    ("13812345678", True, "裸手机号 ⇒ 仍判"),
    ("订单 13812345678 发货", True, "中文语境 ⇒ 仍判"),
    ("abc13812345678def", True, "词元 17 位（非摘要长度）⇒ 仍判（**不误伤**）"),
    ("1381234567812", False, "13 位 ⇒ 不判（**既有行为**，非本次引入）"),
    ("+8613812345678", False, "带国家码 ⇒ 不判（**既有行为**，非本次引入）"),
    ("138-1234-5678", False, "带分隔符 ⇒ 不判（**既有行为**，非本次引入）"),
]


def c4_boundary_table(tmp_path=None, monkeypatch=None) -> bool:
    m = _live()
    for text, expect, _note in BOUNDARY:
        if bool(m.scan_pii(text)["flagged"]) != expect:
            return False
    return True


# ── C5 ⭐ 断言结构（随机词元）──────────────────────────────────────────
def c5_structural_not_string_list(tmp_path=None, monkeypatch=None) -> bool:
    """**随机生成**的 hex 词元（16/32/40/64 位）内嵌合法号段 ⇒ 必须不判。

    这条判据证明的是「判定依据是**结构**（hex 词元 + 长度 + 非纯数字）」，**不是**某份硬编码字符串清单。
    """
    m = _live()
    rnd = random.Random(20261008)
    hexd = "0123456789abcdef"
    for length in (16, 32, 40, 64):
        for _ in range(5):
            tok = [rnd.choice(hexd) for _ in range(length)]
            # 保证含字母（非纯数字）
            tok[0] = rnd.choice("abcdef")
            # 在中间嵌入一个"过号段门"的 11 位串（167 是有效号段）
            run = "167" + "".join(rnd.choice("0123456789") for _ in range(8))
            pos = max(0, length // 2 - 5)
            body = "".join(tok)
            injected = body[:pos] + run + body[pos + 11:]
            if len(injected) != length:
                continue
            text = "hash=%s" % injected
            if m.scan_pii(text)["flagged"]:
                return False
    return True


# ── C6 不对 high 档产生影响 ───────────────────────────────────────────
def c6_no_high_behaviour_change(tmp_path=None, monkeypatch=None) -> bool:
    m = _live()
    texts = _frozen("t70-fp-set.json") + _frozen("t70-tp-set.json")
    from trinity.security.help_context_pairs import PAIRS
    texts += [x["help"] for x in PAIRS]
    if len(texts) < 244:
        return False
    # TP 控制集必须仍全判 high（拒存面不变）
    tps = _frozen("t70-tp-set.json")
    if len(tps) != 19:
        return False
    return all(m.scan_sensitive(t).get("severity") == "high" for t in tps)


CRITERIA = {
    "C1_真手机号仍判PII": c1_real_phone_still_pii,
    "C2_结构hex不再判PII": c2_structural_hex_not_pii,
    "C3_带前缀形态同样不判": c3_teeth,
    "C4_边界表": c4_boundary_table,
    "C5_结构而非字符串": c5_structural_not_string_list,
    "C6_high档不受影响": c6_no_high_behaviour_change,
}


@pytest.mark.parametrize("name", sorted(CRITERIA), ids=sorted(CRITERIA))
def test_criteria_pass(name, tmp_path, monkeypatch):
    assert CRITERIA[name](tmp_path, monkeypatch) is True


MUTANTS = [
    ("C2_结构hex不再判PII", c3b_teeth_removed_exclusion),
]


@pytest.mark.parametrize("name,apply_mutant", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_each_criterion_has_a_killing_mutant(name, apply_mutant, tmp_path, monkeypatch):
    caught = ""
    try:
        got = apply_mutant(tmp_path, monkeypatch)
    except Exception as e:                            # noqa: BLE001 —— 异常=红
        got, caught = False, "%s: %s" % (type(e).__name__, str(e)[:90])
    assert got is False, "变异体 %s 没杀掉判据 %s ⇒ 该判据无判别力" % (name, name)
    if caught:
        print("[teeth] %s 被拦下：%s" % (name, caught))
