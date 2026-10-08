# -*- coding: utf-8 -*-
"""T5：敏感分级三档语义与边界（2026-10-06）。

自行复核的两条既有结论（均**被本文件实测确认**）：
  ① 「抑郁症」在「我得了抑郁症」这类**最直白的个人陈述**上只判 medium
     ⇒ 内容会被存下来（原判据只认「确诊/住院/用药」动词）。
  ② 中文「监狱」整族未被覆盖（原 medium 表只有 犯罪/判刑/拘留/被捕/案底
     与英文 jail/prison）⇒ `我弟弟进了监狱` 完全不命中。

本文件钉住三档语义 **存 / 脱敏存 / 拒存** 与两侧边界（个人语境 vs 公共语境）：
  - 个人语境 ⇒ high ⇒ refuse（拒存）
  - 公共语境（新闻/小说/研究/科普）⇒ medium ⇒ redact（脱敏存，不阻断）
每条判据都带反事实：同一关键词换语境，判定必须翻转。

跑法：
    python -m pytest tests/unit/test_sensitive_tiers_20261006.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trinity.security.sensitive import (  # noqa: E402
    ACTION_REDACT, ACTION_REFUSE, ACTION_STORE, apply_policy, policy_action,
    redact_identifiers, scan_sensitive,
)

# (id, 类别, 文本, 期望 severity, 期望 action)
PERSONAL_HIGH = [
    ("s-01", "自伤", "我最近总想着自杀，觉得活不下去了。", "high"),
    ("s-02", "心理-直白陈述", "我得了抑郁症，最近在吃药。", "high"),
    ("s-03", "心理-第三人称", "用户患有抑郁症，情绪持续低落。", "high"),
    ("s-04", "心理-确诊", "他确诊了抑郁症，正在服用抗抑郁药物。", "high"),
    ("s-05", "法律-个人记录", "我弟弟去年因为盗窃进了监狱，家里人一直瞒着。", "high"),
    ("s-06", "法律-案底", "用户有犯罪记录，案底在老家派出所。", "high"),
    ("s-07", "性史", "我有过一段性史，那是大学时期……", "high"),
    ("s-08", "未成年身份", "我 14 岁的女儿在实验中学读初二，学籍号 2023001。", "high"),
]

PUBLIC_MEDIUM = [
    ("p-01", "新闻-监狱", "新闻：该监狱因超员被监管部门通报，狱方表示将改善条件。"),
    ("p-02", "小说-监狱", "小说情节：主人公在监狱里度过了十年，出狱后回到小镇。"),
    ("p-03", "研究-监狱", "论文研究了监狱劳动制度的历史演变与经济学解释。"),
    ("p-04", "研究-抑郁", "研究显示，抑郁症患者的睡眠结构与非患者存在显著差异。"),
    ("p-05", "期刊-犯罪", "犯罪心理学是研究犯罪行为成因的学科。"),
    ("p-06", "公益-自杀干预", "这篇文档讨论了自杀干预热线 400-161-9995 的运营。"),
    ("p-07", "偏好", "用户偏好暗色模式，使用 VS Code 与 Python。"),
    ("p-08", "运维", "订单号 DO-20260902-1188 库存锁定失败。"),
]


@pytest.mark.parametrize("sid,kind,text,sev", PERSONAL_HIGH, ids=[s[0] for s in PERSONAL_HIGH])
def test_personal_context_refused(sid, kind, text, sev):
    r = scan_sensitive(text)
    assert r["severity"] == "high", (
        "%s(%s) 期望 high，实得 %s ⇒ 该内容会被存下来" % (sid, kind, r["severity"]))
    assert r["action"] == ACTION_REFUSE
    assert policy_action(r) == ACTION_REFUSE


@pytest.mark.parametrize("sid,kind,text", PUBLIC_MEDIUM, ids=[s[0] for s in PUBLIC_MEDIUM])
def test_public_context_never_refused(sid, kind, text):
    """「不该拦的别拦」：新闻/小说/研究语境**不得**拒存。"""
    r = scan_sensitive(text)
    assert r["severity"] != "high", (
        "%s(%s) 被误拒：%s" % (sid, kind, [h["pattern"] for h in r["hits"]]))
    assert r["action"] in (ACTION_STORE, ACTION_REDACT)


def test_context_flip_counterfactual_legal():
    """同一关键词，两种语境必须给出相反判定（可失败的反事实）。"""
    personal = scan_sensitive("我弟弟去年进了监狱。")
    public = scan_sensitive("新闻：该监狱超员被通报。")
    assert personal["severity"] == "high" and personal["action"] == ACTION_REFUSE
    assert public["severity"] != "high" and public["action"] == ACTION_REDACT
    assert "legal_status" in public["categories"], "中文「监狱」必须至少被标记（原实现完全不覆盖）"


def test_context_flip_counterfactual_psych():
    personal = scan_sensitive("我得了抑郁症，最近在吃药。")
    public = scan_sensitive("研究显示，抑郁症患者睡眠结构存在差异。")
    assert personal["severity"] == "high" and personal["action"] == ACTION_REFUSE
    assert public["severity"] == "medium" and public["action"] == ACTION_REDACT


def test_three_tier_semantics_are_total():
    """三档必须覆盖全部取值，且 medium 绝不拒存。"""
    assert scan_sensitive("用户偏好暗色模式")["action"] == ACTION_STORE
    assert scan_sensitive("最近有点抑郁，睡得不好。")["action"] == ACTION_REDACT
    assert scan_sensitive("我想自杀，活着太累了。")["action"] == ACTION_REFUSE


# ── 脱敏存（redact）的执行体 ─────────────────────────────────────────
def test_redact_identifiers_masks_and_is_idempotent():
    """G2/D1 之后：**保留前 3、去尾号**；邮箱只留 TLD。"""
    text = "身份证号 310101199001011234，手机 13812345678，邮箱 zhang.san@corp.cn。"
    out, labels = redact_identifiers(text)
    assert "310101199001011234" not in out
    assert "13812345678" not in out
    assert "zhang.san@corp.cn" not in out
    assert "310" in out, "保留前 3 位（省份/号段）"
    assert "34" not in out, "G2/D1：**尾号必须去掉**（改前保留后 2 位）"
    assert "1234" not in out and "5678" not in out, "手机号尾 4 位不得保留"
    assert "138********" in out
    assert "@***.cn" in out, "邮箱只保留 TLD（不保留完整域名）"
    assert any("身份证号" in x for x in labels) and any("邮箱" in x for x in labels)
    # 幂等：脱敏后的文本再脱敏不再变化
    assert redact_identifiers(out)[0] == out


def test_redact_identifiers_noop_on_clean_text():
    out, labels = redact_identifiers("用户偏好暗色模式，使用 VS Code 与 Python。")
    assert labels == []
    assert "暗色模式" in out


def test_apply_policy_store_redact_refuse():
    st = apply_policy("用户偏好暗色模式")
    assert st["action"] == ACTION_STORE and st["content"] == "用户偏好暗色模式"

    rd = apply_policy("最近有点抑郁，联系方式 13812345678。")
    assert rd["action"] == ACTION_REDACT
    assert "13812345678" not in rd["content"], "脱敏存必须真的脱敏"

    rf = apply_policy("我想自杀，活着太累了。")
    assert rf["action"] == ACTION_REFUSE and rf["content"] is None


def test_quarantine_is_refuse_downgrade(monkeypatch):
    monkeypatch.setenv("TRINITY_SENSITIVE_POLICY", "quarantine")
    r = scan_sensitive("他确诊了抑郁症，正在服用抗抑郁药物。")
    assert r["severity"] == "high" and r["action"] == "quarantine"
    out = apply_policy("他确诊了抑郁症，正在服用抗抑郁药物。", r)
    assert out["isolate"] is True and out["content"] is not None


# ── 可解释性 ─────────────────────────────────────────────────────────
def test_every_hit_has_reason():
    for text in ["我得了抑郁症", "新闻：该监狱超员被通报", "最近有点抑郁"]:
        for h in scan_sensitive(text)["hits"]:
            assert h.get("reason"), h


# ── 修复前/后对照表（可复现证据）────────────────────────────────────
def _load_original_module():
    """从 git HEAD 取修复前的 sensitive.py（不写任何文件）。"""
    import subprocess
    import types
    src = subprocess.run(["git", "show", "HEAD:trinity/security/sensitive.py"],
                         cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8").stdout
    if not src:
        return None
    mod = types.ModuleType("sensitive_before_t5")
    exec(compile(src, "sensitive_before_t5", "exec"), mod.__dict__)  # noqa: S102
    return mod


def main() -> int:
    before = _load_original_module()
    print("=" * 96)
    print("敏感分级样本：修复前 / 修复后（severity → 动作）")
    print("=" * 96)
    rows = [("s-01", "个人-自伤", "我最近总想着自杀，觉得活不下去了。"),
            ("s-02", "个人-心理「直白陈述」", "我得了抑郁症，最近在吃药。"),
            ("s-03", "个人-心理第三人称", "用户患有抑郁症，情绪持续低落。"),
            ("s-05", "个人-法律「进监狱」", "我弟弟去年因为盗窃进了监狱，家里人一直瞒着。"),
            ("p-01", "新闻-监狱", "新闻：该监狱因超员被监管部门通报，狱方表示将改善条件。"),
            ("p-02", "小说-监狱", "小说情节：主人公在监狱里度过了十年。"),
            ("p-04", "研究-抑郁", "研究显示，抑郁症患者的睡眠结构与非患者存在显著差异。"),
            ("p-07", "普通偏好", "用户偏好暗色模式。")]
    for sid, kind, text in rows:
        rb = before.scan_sensitive(text) if before else {"severity": None, "categories": []}
        ra = scan_sensitive(text)
        print("%-6s | %-14s | 修复前 %-6s %-24s | 修复后 %-6s %-8s %s"
              % (sid, kind, rb.get("severity"), ",".join(rb.get("categories") or []) or "-",
                 ra.get("severity"), ra.get("action"), ",".join(ra.get("categories")) or "-"))
    b_missed = sum(1 for _, _, t in rows[:4]
                   if (before.scan_sensitive(t).get("severity") if before else None) != "high")
    a_missed = sum(1 for _, _, t in rows[:4] if scan_sensitive(t).get("severity") != "high")
    b_legal = sum(1 for _, _, t in rows if (before.scan_sensitive(t).get("categories") if before else []) and
                  "legal_status" in before.scan_sensitive(t).get("categories", []))
    print("-" * 96)
    print("个人语境应拒存的 4 条：修复前漏检 %d/4 → 修复后漏检 %d/4" % (b_missed, a_missed))
    print("「监狱」被覆盖的样本数：修复前 %d/3 → 修复后 %d/3"
          % (b_legal, sum(1 for _, _, t in [r for r in rows if "监狱" in r[2]]
                          if scan_sensitive(t).get("categories"))))
    print("公共语境误拒（应 0）：修复前 %d → 修复后 %d"
          % (sum(1 for _, _, t in rows[4:] if (before.scan_sensitive(t).get("severity") if before else None) == "high"),
             sum(1 for _, _, t in rows[4:] if scan_sensitive(t).get("severity") == "high")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
