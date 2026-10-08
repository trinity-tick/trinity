# -*- coding: utf-8 -*-
r"""「声明 ≠ 供料」可报警判据（B5 / t89，2026-10-07）

## 为什么要有这个文件

A5（t84）把这条列成档 1：**注册 47 通道 / 懒加载贡献 0 / 健康 4 / 融合供应商 1（仅 bm25）**。
本仓里"47 个通道"其实是**四个不同的数**，混成一个数就会得出相反结论 ——
实测（本文件在同一进程内只读复算，见 `B5-DECLARED-VS-SUPPLIED.md`）：

| 面 | 名字 | 机器读法 | 实测 |
|---|---|---|---|
| ① | **declared** | `SecondBrainLoader(lazy=True).diagnostics()['retrieval_channels_registered']` | **47** |
| ② | **contributing** | 同 dict 的 `['retrieval_channels_contributing']` | **0** |
| ③ | **available** | `DegradationManager().statistics()['active_channels']` | **5** |
| ④ | **fusion_roster** | `DegradationManager().statistics()['capability_roster']` | **4** |

⭐ 而且 ③ 与 ④ **互不包含**：
`available = {beamlight, exabase, keyword, second_brain, vector}`（其中 **3 条是 NON_CAPABILITY**）
`fusion_roster = {graph_ppr, keyword, serendipity, vector}`（其中 **2 条 health 从未登记**）
⇒ **任何一个单数都会同时高估能力（多算 3 条未接线）又低估供料（少算 2 条真供料）**。
这就是"**不得把『未接线』报成『健康』**"的可机器验证形态。

## 怎么查（一句话）

```powershell
python D:\DSH官网\trinity-optimize-20261006\declared_vs_supplied_probe.py
```

它打印**四面的名字、值、来源路径** + `divergence`（逐面之差），不产出任何"总分"。

## 边界

只读（不调用会 bump `access_count` 的检索入口）；本文件**不接线、不改生产**（接线是各自 owner 的事）。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 四个面的**权威机器读法**（每面各自独立，**禁止**合并成"总分"）
FACE_SOURCES = {
    "declared": "SecondBrainLoader(lazy=True).diagnostics()['retrieval_channels_registered']",
    "contributing": "SecondBrainLoader(lazy=True).diagnostics()['retrieval_channels_contributing']",
    "available": "DegradationManager().statistics()['active_channels']",
    "fusion_roster": "DegradationManager().statistics()['capability_roster']",
}

#: 背离必须在这两个方向上都**显式报出来**（`DegradationManager.statistics()` 提供）
DIVERGENCE_DIRECTIONS = ("declared_but_not_capability", "capability_but_not_declared")


def read_faces() -> dict:
    """只读地把四个面各读一次。**不合并**：返回四元组 + 两条背离方向。"""
    from trinity.modules.second_brain.loader import SecondBrainLoader
    from trinity.agents.degradation import DegradationManager

    d = SecondBrainLoader(lazy=True).diagnostics()
    st = DegradationManager().statistics()
    cross = st.get("capability_roster_vs_declared") or {}
    return {
        "declared": int(d.get("retrieval_channels_registered", 0)),
        "contributing": int(d.get("retrieval_channels_contributing", 0)),
        "available": sorted(st.get("active_channels") or []),
        "fusion_roster": sorted(st.get("capability_roster") or []),
        "registry_only": sorted(st.get("registry_only_channels") or []),
        "declared_but_not_capability": sorted(cross.get("declared_but_not_capability") or []),
        "capability_but_not_declared": sorted(cross.get("capability_but_not_declared") or []),
        "non_capability_names": sorted((st.get("non_capability_names") or {}).keys()),
    }


#: 这些名字**必须**被 **runtime 字段**（`registry_only_channels` 或 `non_capability_names`）点名，
#: 否则"声明的却没供料"这件事在数据里**没有去向** ⇒ 红。
#: ⚠️ 只查 runtime 字段，**不查**下面的账本常量 —— 否则就是自证（把账本并进 accounted 再查账本）。
MUST_BE_ACCOUNTED_BY_RUNTIME = ("retrieval_v47", "beamlight", "exabase", "second_brain")

#: 人可读的逐条理由（**说明用**；判据的可失败性不依赖它，而依赖上面的 runtime 字段）
ACCOUNTED_DECLARED_NOT_SUPPLIED = {
    "retrieval_v47": "frozen_registry: 47 条注册名册本身（RetrievalSystemV47 冻结点）；"
                     "实际贡献由 contributing_count() 独立计数（当前 0），不计入融合名册",
    "beamlight": "no_call_site_at_all（NON_CAPABILITY_NAMES 已逐条登记）",
    "exabase": "wired_but_empty_source（有调用点但恒空，NON_CAPABILITY_NAMES 已登记）",
    "second_brain": "reranker_not_a_channel（只做 RRF 后置微调，NON_CAPABILITY_NAMES 已登记）",
}


def divergence_violations(f: dict) -> list:
    """**背离报警器**（纯函数，便于负向实测）：返回违规说明列表；空 = 无未解释背离。

    规则四条，都**不是自证**（都拿 runtime 的两个独立字段互相对账）：

      R1 **去向点名**：`MUST_BE_ACCOUNTED_BY_RUNTIME` 的每个名字必须出现在
          `registry_only_channels` 或 `non_capability_names` 里（runtime 字段）；
          否则"声明了却没供料"在数据里没有去向 ⇒ 红。
      R1b **交叉对账**：`declared_but_not_capability` 必须 == `available ∩ non_capability_names`；
      R1c **交叉对账**：`capability_but_not_declared` 必须 == `fusion_roster − available`；
      R2 **上限（牙齿）**：`contributing ≤ len(fusion_roster)`
          ⇒「把实际贡献恒设为声明值」必红；
      R3 **未接线 ≠ 健康**：`non_capability_names` 的成员不得出现在 `fusion_roster` 里；
      R4 **两面都必须显式对外**（背离方向字段存在）。
    """
    bad = []
    roster = set(f.get("fusion_roster") or [])
    avail = set(f.get("available") or [])
    noncap = set(f.get("non_capability_names") or [])
    regonly = set(f.get("registry_only") or [])
    declared = int(f.get("declared") or 0)
    contributing = int(f.get("contributing") or 0)

    # R1：声明的每条都要有 runtime 去向
    if declared > 0:
        for name in MUST_BE_ACCOUNTED_BY_RUNTIME:
            if name not in noncap and name not in regonly:
                bad.append("R1：%r 既不在 registry_only_channels 也不在 non_capability_names "
                           "⇒ 『声明但未供料』没有去向登记" % name)

    # R1b/R1c：两个方向必须与集合运算**逐字一致**（payload 不许自说自话）
    exp_dbnc = sorted(avail & noncap)
    got_dbnc = sorted(f.get("declared_but_not_capability") or [])
    if got_dbnc != exp_dbnc:
        bad.append("R1b：declared_but_not_capability=%s，而 available∩non_capability_names=%s "
                   "⇒ 同一份 payload 内两个字段互相矛盾" % (got_dbnc, exp_dbnc))
    exp_cbnd = sorted(roster - avail)
    got_cbnd = sorted(f.get("capability_but_not_declared") or [])
    if got_cbnd != exp_cbnd:
        bad.append("R1c：capability_but_not_declared=%s，而 fusion_roster−available=%s "
                   "⇒ 少报/多报" % (got_cbnd, exp_cbnd))

    # R2：上限（牙齿）
    if contributing > len(roster):
        bad.append("R2：contributing=%d > 真融合名册 %d 条 ⇒ 贡献数超出有调用点证据的通道数"
                   "（『实际贡献＝声明值』这类谎报在这一条上必红）" % (contributing, len(roster)))

    # R3：未接线不得报成健康/能力
    wrongly = sorted(noncap & roster)
    if wrongly:
        bad.append("R3：以下通道**没有供料能力**却出现在能力名册里（未接线被报成健康）：%s" % wrongly)

    # R4：两个面都必须是**显式对外的**（不能只有一个数）
    for key in DIVERGENCE_DIRECTIONS:
        if key not in f:
            bad.append("R4：缺少背离方向字段 %r ⇒ 只有一个数，背离不可见" % key)
    return bad


def supplying_count(supplying: dict) -> int:
    """**供料面**计数（可注入 ⇒ 便于反事实）：只数"真的往融合里供了一个非空元素"的通道。"""
    return sum(1 for ch, supplies in (supplying or {}).items() if supplies)


# ── 判据 ──────────────────────────────────────────────────────────────────

_REAL = read_faces()


def test_四个面各自独立可读_不得混成一个数() -> None:
    """四面必须各自有名字与来源；且 `available` 与 `fusion_roster` **互不包含**（不是同一个数）。"""
    assert set(FACE_SOURCES) == {"declared", "contributing", "available", "fusion_roster"}
    for k, src in FACE_SOURCES.items():
        assert src, "面 %s 没有机器读法" % k

    avail, roster = set(_REAL["available"]), set(_REAL["fusion_roster"])
    assert avail != roster, "两个面读出来是同一个集合 ⇒ 口径没分开"
    # 实测：available 里有非能力项、roster 里有 health 未登记项 ⇒ 两向都不包含
    assert avail - roster, "available 未比 roster 多出任何东西 ⇒ 口径可能被合并"
    assert roster - avail, "roster 未比 available 多出任何东西 ⇒ 两向背离至少一向不可见"

    # 背离必须是**逐方向**的两个字段，而不是一个合并后的数
    for key in DIVERGENCE_DIRECTIONS:
        assert key in _REAL, "缺少背离方向 %r" % key


def test_背离必须被逐条登记_否则红() -> None:
    """① 背离存在 ⇒ 必须能被判据**看见**（未解释的背离一律红）。"""
    bad = divergence_violations(_REAL)
    assert bad == [], "背离报警器报出违规：%s" % bad
    assert _REAL["declared"] > len(_REAL["fusion_roster"]), \
        "声明的 47 条本应多于真融合名册；若不再如此，请复核本判据是否还有意义"


def test_负向实测_移走一条去向登记必须红() -> None:
    """① 的负向：把去向登记人为抽掉一条 ⇒ 报警器必须红（证明"登记"是承重的）。"""
    fake = dict(_REAL)
    fake["non_capability_names"] = []
    fake["registry_only"] = []
    bad = divergence_violations(fake)
    assert bad, "去向被抽空却没判红 ⇒ 判据没牙齿"
    assert any("R1" in b for b in bad)


def test_牙齿_把实际贡献恒设为声明值必须红() -> None:
    """③ **核心牙齿**：`contributing = declared`（47）⇒ 必红。

    真供料数不可能超过"有调用点证据的名册"（当前 4 条）；把贡献数谎报成 47 会被 R2 抓住。
    """
    fake = dict(_REAL)
    fake["contributing"] = fake["declared"]
    bad = divergence_violations(fake)
    assert any("R2" in b for b in bad), "把贡献数设成声明值却没红 ⇒ 没有牙齿：%s" % bad

    # 边界：恰好不超过名册时不应误报（避免"过宽 ⇒ 恒红"）
    ok = dict(_REAL)
    ok["contributing"] = len(ok["fusion_roster"])
    assert not any("R2" in b for b in divergence_violations(ok)), "合法值被误判"


def test_负向实测_未接线不得报成健康() -> None:
    """④ 未接线（NON_CAPABILITY）出现在能力名册里 ⇒ 必红。

    这正是历史 bug 的形态：`contributing_channels` 曾是 `active` 的复制品，
    于是 `beamlight`（**全仓没有任何 search()**）被当成在贡献。
    """
    fake = dict(_REAL)
    fake["fusion_roster"] = sorted(set(fake["available"]))
    bad = divergence_violations(fake)
    assert any("R3" in b for b in bad), "把 available 抄进能力名册却没红：%s" % bad
    assert "beamlight" in " ".join(bad) or "exabase" in " ".join(bad)


def test_反事实_打开一路真实ANN供料面必须1到2() -> None:
    """② **A5 给的证伪**：只把**一路真实 ANN** 打开 ⇒ 供料面计数必须 **1 → 2**。

    ⚠️ 这是对**度量函数**的反事实（生产接线本轮不动；接线是各自 owner 的事）。
    """
    before = {"bm25": True}
    assert supplying_count(before) == 1
    after = dict(before)
    after["ann"] = True          # 打开一路真实 ANN
    assert supplying_count(after) == 2, "打开一路真实供料通道后计数没有 1→2 ⇒ 度量失效"
    # 声明了但不供料 ⇒ **不计入**
    after["declared_but_offline"] = False
    assert supplying_count(after) == 2, "未供料的通道被计入 ⇒ 又回到『声明 = 供料』"


def test_供料面只数真正供料的_声明面单独读() -> None:
    """声明面与供料面**分开**：声明 47 不影响供料计数。"""
    f = _REAL
    assert f["declared"] == 47, "注册名册必须保留（冻结点）"
    assert f["contributing"] == 0, "懒加载贡献当前为 0（frozen 注册器不得计入贡献）"
    assert supplying_count({"a": True, "b": False}) == 1
    assert supplying_count({}) == 0
