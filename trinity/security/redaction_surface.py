# -*- coding: utf-8 -*-
"""t106/G3 · 脱敏面登记表：**D-2 掩码口径/掩码点** · **D-3 开关面** · **D-5 占位符与占位域名**。

设计原则（与 G1/t104 的写入协议一致）：
  · **只登记事实，不改变行为**：本模块**不改任何判定/掩码语义**（纯读 + 探针）；
  · **能实测的就不用推断**：掩码点用**真调用**取观察值；开关默认值用**子进程实测**（见 `measure_defaults`）；
  · **差异要明写**：与口径不一致的点**登记为 `convention_compliant=False` + `note`**，
    **不为了"看起来统一"而顺手改语义**（改语义需队长先裁定）。

调用者：判据 `tests/unit/test_redaction_surface_20261006.py` + 报告 `G3-REDACTION-SURFACE.md`。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── D-2：掩码口径（G2/D1 裁定）──────────────────────────────────────────
#: 「**保留前 3 位、去掉尾部**」；邮箱只留 TLD。
MASK_CONVENTION: Dict[str, Any] = {
    "policy_id": "G2/D1-head3-no-tail",
    "digits": {"head": 3, "tail": 0, "mask_char": "*",
               "example": "13814141414 -> 138********"},
    "email": {"local_head": 1, "domain": "***", "keep": "TLD only",
              "example": "zhangsan@corp.cn -> z***@***.cn"},
    "secret": {"form": "prefix + ***", "example": "sk-abcdef... -> sk-***"},
    "grouped_card": {"form": "digits[:3] + '*' * (len-3)", "example": "4111 1111 1111 1111 -> 411*************"},
}

#: D-2 掩码点清单（**逐点**：谁在掩、掩哪类、是否符合口径、为什么）。
#: `in_domain` = 是否在 t106 的写域内（`trinity/security/**` + `trinity/adapters/_pii_guard.py`）。
MASK_POINTS: List[Dict[str, Any]] = [
    {"id": "sensitive._mask_head", "module": "trinity/security/sensitive.py",
     "owner": "security", "in_domain": True, "kinds": ["身份证号", "手机号", "银行卡号", "学籍号", "护照", "社保号"],
     "form": "head3 + '*'*(len-3)", "convention_compliant": True, "note": "G2/D1 口径本体"},
    {"id": "sensitive._mask_email", "module": "trinity/security/sensitive.py",
     "owner": "security", "in_domain": True, "kinds": ["邮箱"],
     "form": "local[:1] + '***@***.' + tld", "convention_compliant": True, "note": "只保留 TLD"},
    {"id": "sensitive._mask_secret", "module": "trinity/security/sensitive.py",
     "owner": "security", "in_domain": True, "kinds": ["密钥·token（字面量）"],
     "form": "prefix + '***'", "convention_compliant": True, "note": "保留族前缀便于人工识别"},
    {"id": "sensitive._mask_secret_assign", "module": "trinity/security/sensitive.py",
     "owner": "security", "in_domain": True, "kinds": ["密钥·token（赋值）"],
     "form": "键名保留 + 值全遮", "convention_compliant": True, "note": "同口径（非数字族）"},
    {"id": "sensitive._mask_grouped_card", "module": "trinity/security/sensitive.py",
     "owner": "security", "in_domain": True, "kinds": ["银行卡号（4-4-4-4 分组）"],
     "form": "digits[:3] + '*'*(len-3)", "convention_compliant": True, "note": "分隔符一并抹掉"},
    {"id": "adapters._pii_guard.adapter_pii_guard", "module": "trinity/adapters/_pii_guard.py",
     "owner": "security", "in_domain": True, "kinds": ["全部（转接）"],
     "form": "delegates -> sensitive.redact_identifiers", "convention_compliant": True,
     "note": "**不自己实现掩码**，只转接（口径随 sensitive）"},
    {"id": "security.credentials.dsn_redacted", "module": "trinity/security/credentials.py",
     "owner": "security", "in_domain": True, "kinds": ["DSN 口令/用户名"],
     "form": "postgresql://user:***@host/db", "convention_compliant": True,
     "note": "**另一类制品（连接串）**：不适用「数字前 3 位」口径，属**已登记的口径外点**"},
    # ── G9R-9/t123：原先的"域外潜伏分叉"点 ⇒ **已修并与主口径对齐** ──────────
    {"id": "adapters.sqlite._crypto._detect_pii", "module": "trinity/adapters/sqlite/_crypto.py",
     "owner": "adapters", "in_domain": True, "kinds": ["手机号", "邮箱", "身份证号"],
     "form": "**已对齐 G2/D1**：数字 `[:3] + '*'*(len-3)`；邮箱 `local[0] + '***@***.' + tld`",
     "convention_compliant": True,
     "note": "⭐ **G9R-9/t123 已修**：旧口径（手机 `[:3]+'****'+[-4:]`、邮箱保留**完整域名**、身份证 `[:6]+'********'+[-4:]`）"
             "与主口径冲突 ⇒ 已改为与 `MASK_CONVENTION` 一致。**只改口径那几行，未动调用面**："
             "`store_memory(auto_redact_pii=…)` 全仓**无调用方传 True**（实测 grep 命中 0，见 "
             "`evidence/g9r9-no-caller.txt`）⇒ 该路径当前不生效；本次只为消除「一打开就分叉」的 landmine"},
    {"id": "scripts.knowledge_pack._redact", "module": "scripts/knowledge_pack.py",
     "owner": "scripts", "in_domain": False, "kinds": ["手机号", "邮箱"],
     "form": "占位符替换：`[PHONE]` / `[EMAIL]`（**不保留任何前缀**）", "convention_compliant": False,
     "note": "⭐ **另一套掩码（占位符式）**：知识包导出用；与 D-5 的占位符登记同源。"
             "**故意不同**（导出件要可读占位符），**登记为口径外点**，不在 t106 改（域外 + 有既有判据钉住）"},
    {"id": "scripts.sync_sqlite_to_pg.dsn_redacted", "module": "scripts/sync_sqlite_to_pg.py",
     "owner": "scripts", "in_domain": False, "kinds": ["DSN"],
     "form": "复用 `credentials.dsn_redacted`", "convention_compliant": True, "note": "同上：连接串类"},
]

# ── D-5：占位符 / 占位域名登记 ──────────────────────────────────────────
#: 掩码字符/占位 token（**谁在用**必须写清）
MASK_TOKENS: List[Dict[str, Any]] = [
    {"token": "*", "used_by": ["trinity/security/sensitive.py（全部数字/邮箱掩码器）",
                               "trinity/adapters/sqlite/_crypto.py（口径外）"],
     "why": "统一掩码字符（无信息量、可 grep）", "consumers": ["人读", "tests/unit/test_*sensitive*"]},
    {"token": "[PHONE]", "used_by": ["scripts/knowledge_pack.py::_redact"],
     "why": "知识包导出件的占位符（导出件要人可读）", "consumers": ["knowledge_pack 导入端（不再脱敏）", "tests/unit/test_knowledge_pack.py"]},
    {"token": "[EMAIL]", "used_by": ["scripts/knowledge_pack.py::_redact"],
     "why": "同上", "consumers": ["同上"]},
]

#: **占位/保留域名**（RFC 2606 / RFC 6761 + 内网名）：被**有意排除**、不掩码（I10/G3-R3 登记）
PLACEHOLDER_DOMAINS: Dict[str, Any] = {
    "policy_id": "RFC2606-6761-placeholders",
    "domains": ["example.com", "example.org", "example.net"],
    "reserved_names": ["localhost", "local", "test", "invalid", "example", "internal", "corp"],
    "why": "RFC 2606/6761 保留 + 内网专用名 ⇒ 不可能承载真实个人邮箱，掩它只是噪声",
    "consumers": ["trinity/security/sensitive.py::_email_ok（判定）",
                  "tests/unit/test_high_category_false_positives_20261006.py（C9/C10 钉住）"],
    "readers_note": "**没有下游消费者**：它们只影响「要不要掩」的判定 ⇒ 掩码产物里不会出现它们",
}

# ── D-3：开关面（名字 · 默认值 · 层 · 关系 · 谁读）────────────────────────
#: `default` 的写法是**观察值**（见 `measure_defaults`）；判据会把它与实测比对。
SWITCHES: List[Dict[str, Any]] = [
    {"name": "TRINITY_SENSITIVE_SCAN", "default": True, "off_values": ["off", "0", "false"],
     "layer": ["client(_ingestion)", "adapter(_pii_guard)", "engine_worker(客户端路径)"],
     "helper": "trinity.security.sensitive.sensitive_scan_enabled",
     "readers": ["trinity/security/sensitive.py: sensitive_scan_enabled()",
                 "trinity/core/client/_ingestion.py（写路径前置门）",
                 "trinity/adapters/_pii_guard.py: adapter_guard_state() -> 'scan-off'"],
     "semantics": "**主开关**：关掉后 **客户端与适配器守卫都不扫描**（实测见 criteria D3c）；"
                  "但**不阻止**其它代码**直接调用** `redact_identifiers()/apply_policy()`（脚本可直调）⇒ "
                  "「覆盖全部层」**不成立**：它覆盖的是**两条写入路径**，不是「所有能调用掩码的代码」",
     "related": ["TRINITY_ADAPTER_GUARD", "TRINITY_SENSITIVE_REDACT"]},
    {"name": "TRINITY_SENSITIVE_REDACT", "default": True, "off_values": ["off", "0", "false", "no"],
     "layer": ["client(_ingestion)", "adapter(_pii_guard：作为第二级门)"],
     "helper": "trinity.security.sensitive.sensitive_redact_enabled",
     "readers": ["trinity/security/sensitive.py: sensitive_redact_enabled()（并被 B1 决策记录的 tier 计算读取）",
                 "trinity/core/client/_ingestion.py（掩码开关）",
                 "trinity/adapters/_pii_guard.py: adapter_guard_state() -> 'redact-off'"],
     "semantics": "掩码总闸：关掉后**不掩码**（但 high 档的拒存由 `severity` 决定，**不受它影响**）",
     "related": ["TRINITY_SENSITIVE_REDACT_SCOPE", "TRINITY_SENSITIVE_SCAN"]},
    {"name": "TRINITY_SENSITIVE_REDACT_SCOPE", "default": "all_pii", "off_values": [],
     "layer": ["判定层（sensitive.policy_action）"],
     "helper": "trinity.security.sensitive.sensitive_redact_scope",
     "readers": ["trinity/security/sensitive.py: policy_action() / _build_decision_record()"],
     "semantics": "`all_pii`（默认）⇒ 有 PII 即掩；`category` ⇒ **只**在命中敏感类别时掩（= G2 之前的精确回滚）",
     "related": ["TRINITY_SENSITIVE_REDACT"]},
    {"name": "TRINITY_SENSITIVE_POLICY", "default": "refuse", "off_values": [],
     "layer": ["判定层（sensitive._policy → policy_action）"],
     "helper": "trinity.security.sensitive._policy",
     "readers": ["trinity/security/sensitive.py: policy_action()", "_build_decision_record()"],
     "semantics": "high 档的处置：`refuse`（默认，不落库）｜`quarantine`（落 `status='archived'`，不进检索面）",
     "related": []},
    {"name": "TRINITY_ADAPTER_GUARD", "default": True, "off_values": ["0", "off", "false", "no", ""],
     "layer": ["adapter 写入边界（_crud/_pii_guard）"],
     "helper": "trinity.adapters._pii_guard.adapter_guard_enabled",
     "readers": ["trinity/adapters/_pii_guard.py: adapter_guard_state()（返回 'guard-off'）"],
     "semantics": "**只跳过适配器层**的守卫（客户端层不受影响）；与 `SCAN=off`/`REDACT=0` 是**并联**关系",
     "related": ["TRINITY_SENSITIVE_SCAN", "TRINITY_SENSITIVE_REDACT"]},
    {"name": "TRINITY_HIGH_PERSONAL_CONTEXT", "default": True, "off_values": ["off", "0", "false", "no"],
     "layer": ["判定层（sensitive._HIGH_PATTERNS 在**导入时**按它选表）"],
     "helper": "trinity.security.sensitive.high_personal_context_required",
     "readers": ["trinity/security/sensitive.py: high_personal_context_required()（import 期 + 每次 scan 的留痕分支）"],
     "semantics": "I10/t70：high 档是否**要求个人语境**；`off` ⇒ **逐字回到裸词行为**（121/121 零差异，实测）",
     "related": []},
    {"name": "TRINITY_DECISION_LOG_SIZE", "default": 512, "off_values": ["0", "off", "false", "no"],
     "layer": ["可观测层（B1/t85 决策记录）"],
     "helper": "trinity.security.sensitive.decision_log_size",
     "readers": ["trinity/security/sensitive.py: decision_records_enabled() / _decision_deque()"],
     "semantics": "记录缓冲上限；`0` ⇒ 关闭记录（**判定语义不变**）",
     "related": ["TRINITY_DECISION_LOG_PATH"]},
    {"name": "TRINITY_DECISION_LOG_PATH", "default": None, "off_values": [],
     "layer": ["可观测层（B1/t85 决策记录）"],
     "helper": "(直接 os.environ.get，见 sensitive._record_decision)",
     "readers": ["trinity/security/sensitive.py: _record_decision()（JSONL 追加）"],
     "semantics": "⭐ **默认 None ⇒ 不落盘**（决策记录只在内存 deque）",
     "related": ["TRINITY_DECISION_LOG_SIZE"]},
]

#: ⭐ D-13 澄清（队长要求点名"制品 / 开关名 / 默认值 / 读取点"）：**我这一路**的制品明细。
DECISION_LOG_ARTIFACT: Dict[str, Any] = {
    "artifact": "决策记录（B1/t85）：**内存 deque**；仅当设置 PATH 才追加 JSONL 文件",
    "switch": "TRINITY_DECISION_LOG_PATH",
    "default_expected": None,
    "read_point": "trinity/security/sensitive.py::_record_decision()（`path = os.environ.get(...)`；"
                  "非空才 `open(path, \"a\")`）",
    "scope_note": "**「默认不落盘」只对这一路成立**；本仓**另有**默认落盘的路径（见 `OTHER_DISK_WRITERS`）",
}

#: ⭐ 本仓**其它默认落盘**的路径（队长要求"发现别的路径默认落盘 ⇒ 单列"）。
#: 依据：代码读取点 + 默认值（判据里用子进程实测默认值）；**本任务不改它们**。
OTHER_DISK_WRITERS: List[Dict[str, Any]] = [
    {"artifact": "语料索引持久化（corpus index）", "switch": "TRINITY_CORPUS_INDEX_PERSIST",
     "default_on": True, "read_point": "trinity/core/client/_corpus_persist.py:53",
     "note": "⭐ verifier 抓到的反例候选：`os.environ.get(\"TRINITY_CORPUS_INDEX_PERSIST\",\"1\") != \"0\"` ⇒ "
             "**默认 ON = 默认落盘**。⇒ **它算「默认落盘」**；因此本仓**不能**统一说「默认不落盘」，"
             "只能说「**决策记录这一路默认不落盘**」"},
    {"artifact": "聚合向量缓存（aggregator vectors）", "switch": "(无开关，固定路径)",
     "default_on": True, "read_point": "聚合器（进程启动日志可见 `aggregator_vectors.pkl`）",
     "note": "日志实测出现过 `vector index is faiss-format … aggregator_vectors.pkl`（路径固定，无开关）"},
]


def _in_domain_probe() -> List[Dict[str, Any]]:
    """**真调用**掩码器（只用 in-domain + 已登记的安全样本），返回观察到的格式。"""
    from trinity.security import sensitive as S
    sample_digits = "13814141414"
    got_digits, _ = S.redact_identifiers(sample_digits)
    got_email, _ = S.redact_identifiers("zhangsan@corp.cn")
    got_card, _ = S.redact_identifiers("4111111111111111")
    got_grouped, _ = S.redact_identifiers("4111 1111 1111 1111")
    got_secret, _ = S.redact_identifiers("sk-abcdefghijklmnopqrst")
    return [
        {"point": "数字（手机/身份证/卡号）", "observed": got_digits,
         "head3_ok": got_digits.startswith("138") and got_digits[3:] == "*" * (len(got_digits) - 3)},
        {"point": "邮箱", "observed": got_email, "tld_only_ok": got_email.endswith(".cn") and "corp" not in got_email},
        {"point": "银行卡号（裸）", "observed": got_card, "head3_ok": got_card.startswith("411")},
        {"point": "银行卡号（4-4-4-4）", "observed": got_grouped, "no_separator_ok": " " not in got_grouped},
        {"point": "密钥字面量", "observed": got_secret, "prefix_ok": got_secret.startswith("sk-")},
    ]


def mask_point_probe() -> Dict[str, Any]:
    """D-2 的可机读探针结果（判据直接吃它）。"""
    probes = _in_domain_probe()
    ok = all(all(v for k, v in p.items() if k.endswith("_ok")) for p in probes)
    return {"convention": MASK_CONVENTION, "probes": probes, "in_domain_conform": ok,
            "registered_points": len(MASK_POINTS),
            "non_conforming": [p["id"] for p in MASK_POINTS if not p["convention_compliant"]]}


def placeholder_registry() -> Dict[str, Any]:
    """D-5 的可机读登记（占位域名 + 占位 token + 消费者）。"""
    return {"policy": PLACEHOLDER_DOMAINS, "tokens": MASK_TOKENS}


# ══════════════════════════════════════════════════════════════════════════
# G9R-9/t123 · **跨点口径一致性**（主口径 `MASK_CONVENTION` ↔ 各掩码点）
# ══════════════════════════════════════════════════════════════════════════
# ⭐ **例外登记表的读取路径（将来新增例外走这里）**：
#     `trinity/security/redaction_surface.py` 的 `MASK_POINT_EXCEPTIONS`（本模块常量）
#     ⇒ 也经 `mask_point_exceptions()` 程序化读取；每条必须填
#     `reason`（为什么故意不同）/ `caliber`（它实际的口径）/ `owner` / `since`。
#     ⚠️ 例外不是"豁免检查"：判据仍会把它**算出来并与主口径对比**，只是**不计入失败**，
#     并在报告里**显式列出差异**（`diff` 字段）—— 所以"登记为例外"不等于"没人看"。
MASK_POINT_EXCEPTIONS: Dict[str, Dict[str, str]] = {
    "scripts.knowledge_pack._redact": {
        "reason": "知识包**导出件**要人可读，占位符 `[PHONE]`/`[EMAIL]` 是**有意的不同口径**（G3/D-5 登记）",
        "caliber": "占位符替换（不保留任何前缀）", "owner": "scripts", "since": "2026-10-06(G3/t106)",
    },
    "security.credentials.dsn_redacted": {
        "reason": "**另一类制品**（连接串）：`scheme://user:***@host/db`；数字型「保留前 3」口径对它不适用",
        "caliber": "口令/用户名全遮，主机与库名保留", "owner": "security", "since": "2026-10-06(G3/t106)",
    },
}


def mask_point_exceptions() -> Dict[str, Dict[str, str]]:
    """**例外登记表**（读取路径见模块内 `MASK_POINT_EXCEPTIONS` 上方的注释）。"""
    return {k: dict(v) for k, v in MASK_POINT_EXCEPTIONS.items()}


#: 探针：每个**口径内**掩码点 → 用同一批**规范样本**取它的**实测**掩码产物。
#: ⚠️ 判据不比较硬编码字符串，而是把实测值与**由 `MASK_CONVENTION` 推导出的结构**比对
#:   （⇒ 改主口径时，未登记为例外的点会自动变红）。
_SAMPLE_DIGITS = "13814141414"
_SAMPLE_EMAIL = "zhangsan@corp.cn"


def _probe_sensitive_digits() -> str:
    from trinity.security import sensitive as _s
    return _s.redact_identifiers(_SAMPLE_DIGITS)[0]


def _probe_sensitive_email() -> str:
    from trinity.security import sensitive as _s
    return _s.redact_identifiers(_SAMPLE_EMAIL)[0]


def _probe_guard_email() -> str:
    from trinity.adapters._pii_guard import adapter_pii_guard
    out, _md, _info = adapter_pii_guard("联系 %s" % _SAMPLE_EMAIL, {})
    return out.replace("联系 ", "")


def _probe_crypto_digits() -> str:
    from trinity.adapters.sqlite._crypto import _CryptoMixin
    return _CryptoMixin._detect_pii(_CryptoProbeStub(), _SAMPLE_DIGITS)["redacted"]


def _probe_crypto_email() -> str:
    from trinity.adapters.sqlite._crypto import _CryptoMixin
    return _CryptoMixin._detect_pii(_CryptoProbeStub(), _SAMPLE_EMAIL)["redacted"]


class _CryptoProbeStub:
    """`_detect_pii` 只用 `self._PII_PATTERNS` ⇒ 给它一个最小 stub 即可**真调用**该点。"""

    @property
    def _PII_PATTERNS(self):                          # noqa: D401 —— 与适配器同源，不复制副本
        from trinity.adapters.sqlite._crypto import _CryptoMixin
        return _CryptoMixin._PII_PATTERNS


#: 点 → (探针, 期望的**结构族**)
PROBES: Dict[str, Dict[str, Any]] = {
    "sensitive._mask_head": {"probe": _probe_sensitive_digits, "family": "digits"},
    "sensitive._mask_email": {"probe": _probe_sensitive_email, "family": "email"},
    "adapters._pii_guard.adapter_pii_guard": {"probe": _probe_guard_email, "family": "email"},
    "adapters.sqlite._crypto._detect_pii": {"probe": _probe_crypto_digits, "family": "digits"},
    "adapters.sqlite._crypto._detect_pii#email": {"probe": _probe_crypto_email, "family": "email"},
}


def _check_structure(observed: str, family: str, convention: Optional[Dict[str, Any]] = None):
    """按**结构**（由 `convention` 推导）判断实测值是否符合主口径。返回 `(ok, expected_desc, diff)`。"""
    import re as _re
    conv = convention if convention is not None else MASK_CONVENTION
    if family == "digits":
        head = int(conv["digits"]["head"])
        ch = str(conv["digits"]["mask_char"])
        pat = r"^(\d{%d})(%s{%d,})$" % (head, _re.escape(ch), max(1, len(_SAMPLE_DIGITS) - head))
        desc = "`digits[:%d] + '%s'*(len-%d)`（**不留尾号**）" % (head, ch, head)
        m = _re.fullmatch(pat, observed)
        ok = bool(m) and len(observed) == len(_SAMPLE_DIGITS) and observed[:head] == _SAMPLE_DIGITS[:head]
        diff = "" if ok else ("实测 `%s` 不符合 %s" % (observed, desc))
        return ok, desc, diff
    head = int(conv["email"]["local_head"])
    dom = str(conv["email"]["domain"])
    pat = r"^(.{%d})%s@%s\.([A-Za-z]{2,})$" % (head, _re.escape(dom), _re.escape(dom))
    desc = "`local[:%d] + '%s@%s.' + tld`（**只留 TLD**）" % (head, dom, dom)
    m = _re.fullmatch(pat, observed)
    tld_src = _SAMPLE_EMAIL.rsplit(".", 1)[-1]
    ok = bool(m) and m.group(2) == tld_src and "corp" not in observed
    diff = "" if ok else ("实测 `%s` 不符合 %s（或保留了非 TLD 的域名部分）" % (observed, desc))
    return ok, desc, diff


def caliber_report(convention: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """⭐ **跨点口径一致性报告**：逐点给出「实测 / 期望 / 是否符合 / 差在哪 / 是否登记为例外」。

    `all_ok` = **口径内的点全部符合**（例外点**不计入失败**，但仍在 `points` 里带 `diff` 出现）。
    """
    conv = convention if convention is not None else MASK_CONVENTION
    exceptions = MASK_POINT_EXCEPTIONS
    points: List[Dict[str, Any]] = []
    for pid, spec in PROBES.items():
        base_id = pid.split("#")[0]
        try:
            observed = spec["probe"]()
            err = ""
        except Exception as exc:                      # noqa: BLE001 —— 探针失败要**报出来**，不静默
            observed, err = "", "%s: %s" % (type(exc).__name__, str(exc)[:80])
        ok, expected, diff = _check_structure(observed, spec["family"], conv)
        points.append({"id": pid, "observed": observed, "expected": expected,
                       "complies": bool(ok) and not err, "diff": err or diff,
                       "is_exception": base_id in exceptions,
                       "exception_reason": exceptions.get(base_id, {}).get("reason", "")})
    for pid, info in exceptions.items():
        if pid in {p["id"] for p in points}:
            continue
        points.append({"id": pid, "observed": info["caliber"], "expected": "（例外：不适用主口径）",
                       "complies": True, "diff": "刻意不同：%s" % info["reason"],
                       "is_exception": True, "exception_reason": info["reason"]})
    failing = [p["id"] for p in points if not p["complies"] and not p["is_exception"]]
    return {"convention": conv, "points": points, "failing": failing, "all_ok": not failing,
            "exception_keys": sorted(exceptions)}


def mask_char_mismatch_hint(email_out: str) -> bool:
    """占位符口径自检：邮箱掩码输出必须形如 `x***@***.tld`（D-5 用于牙齿判据）。"""
    return "***@***." in email_out


# ── D-3：开关面（机器可读 + **子进程实测默认值**）────────────────────────
def switch_surface() -> Dict[str, Any]:
    """开关面表（可 `json.dumps`）。"""
    return {"count": len(SWITCHES), "switches": SWITCHES,
            "decision_log_artifact": DECISION_LOG_ARTIFACT,
            "other_disk_writers": OTHER_DISK_WRITERS}


def observed_defaults_inproc() -> Dict[str, Any]:
    """**本进程**实测各开关的取值（默认环境下）。"""
    from trinity.security import sensitive as S
    from trinity.adapters import _pii_guard as G
    env_backup = {k: os.environ.pop(k, None) for k in (
        "TRINITY_SENSITIVE_SCAN", "TRINITY_SENSITIVE_REDACT", "TRINITY_SENSITIVE_REDACT_SCOPE",
        "TRINITY_SENSITIVE_POLICY", "TRINITY_ADAPTER_GUARD", "TRINITY_HIGH_PERSONAL_CONTEXT",
        "TRINITY_DECISION_LOG_SIZE", "TRINITY_DECISION_LOG_PATH")}
    try:
        return {
            "TRINITY_SENSITIVE_SCAN": bool(S.sensitive_scan_enabled()),
            "TRINITY_SENSITIVE_REDACT": bool(S.sensitive_redact_enabled()),
            "TRINITY_SENSITIVE_REDACT_SCOPE": S.sensitive_redact_scope(),
            "TRINITY_SENSITIVE_POLICY": S._policy(),
            "TRINITY_ADAPTER_GUARD": bool(G.adapter_guard_enabled()),
            "TRINITY_HIGH_PERSONAL_CONTEXT": bool(S.high_personal_context_required()),
            "TRINITY_DECISION_LOG_SIZE": S.decision_log_size(),
            "TRINITY_DECISION_LOG_PATH": os.environ.get("TRINITY_DECISION_LOG_PATH"),
        }
    finally:
        for k, v in env_backup.items():
            if v is not None:
                os.environ[k] = v


def measure_defaults(timeout: float = 60.0) -> Dict[str, Any]:
    """⭐ **子进程实测**默认值（队长要求：不要读代码推断）—— 产物**先落盘再读**（避开管道）。

    子进程：清空相关环境变量 ⇒ import 真模块 ⇒ 调用真函数 ⇒ 把结果 JSON 写到临时文件。
    """
    code = (
        "import json,os,sys\n"
        "for k in ('TRINITY_SENSITIVE_SCAN','TRINITY_SENSITIVE_REDACT','TRINITY_SENSITIVE_REDACT_SCOPE',"
        "'TRINITY_SENSITIVE_POLICY','TRINITY_ADAPTER_GUARD','TRINITY_HIGH_PERSONAL_CONTEXT',"
        "'TRINITY_DECISION_LOG_SIZE','TRINITY_DECISION_LOG_PATH','TRINITY_CORPUS_INDEX_PERSIST'):\n"
        "    os.environ.pop(k, None)\n"
        "sys.path.insert(0, r'%s')\n"
        "from trinity.security import sensitive as S\n"
        "from trinity.adapters import _pii_guard as G\n"
        "from trinity.core.client import _corpus_persist as C\n"
        "out = {\n"
        " 'TRINITY_SENSITIVE_SCAN': bool(S.sensitive_scan_enabled()),\n"
        " 'TRINITY_SENSITIVE_REDACT': bool(S.sensitive_redact_enabled()),\n"
        " 'TRINITY_SENSITIVE_REDACT_SCOPE': S.sensitive_redact_scope(),\n"
        " 'TRINITY_SENSITIVE_POLICY': S._policy(),\n"
        " 'TRINITY_ADAPTER_GUARD': bool(G.adapter_guard_enabled()),\n"
        " 'TRINITY_HIGH_PERSONAL_CONTEXT': bool(S.high_personal_context_required()),\n"
        " 'TRINITY_DECISION_LOG_SIZE': S.decision_log_size(),\n"
        " 'TRINITY_DECISION_LOG_PATH': os.environ.get('TRINITY_DECISION_LOG_PATH'),\n"
        " 'TRINITY_CORPUS_INDEX_PERSIST': os.environ.get('TRINITY_CORPUS_INDEX_PERSIST','1') != '0',\n"
        "}\n"
        "open(sys.argv[1],'w',encoding='utf-8').write(json.dumps(out))\n"
    ) % str(Path(__file__).resolve().parents[2])
    # ⭐ G10R10/t142：临时目录**保证清理**（此前 `tempfile.mkdtemp` **从不清理** ⇒
    # 全量判据 `tests/unit/test_mkdtemp_cleanup.py` 报「25 处 > 基线 23」，本文件是新增的第 2 处）。
    # 用 `TemporaryDirectory` 而不是 `mkdtemp`：**正常返回、`if tmp.exists()` 早退、异常分支**
    # 三条路径**全部**由上下文管理器收口（下游只按路径用 `td`，无需保留 `Path` 形态 ⇒ 不需要 try/finally）。
    with tempfile.TemporaryDirectory(prefix="g3_switch_") as _td:
        tmp = Path(_td) / "defaults.json"
        try:
            subprocess.run([sys.executable, "-c", code, str(tmp)], timeout=timeout,
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if tmp.exists():
                return json.loads(tmp.read_text(encoding="utf-8"))
        except Exception as exc:                      # noqa: BLE001
            return {"_error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
        return {"_error": "subprocess wrote nothing"}


def switch_surface_report() -> Dict[str, Any]:
    """合并：登记表 + 本进程观察 + 子进程实测（判据用这个）。"""
    return {"surface": switch_surface(),
            "observed_inproc": observed_defaults_inproc(),
            "measured_subproc": measure_defaults()}


def documented_default(name: str) -> Optional[Any]:
    for sw in SWITCHES:
        if sw["name"] == name:
            return sw["default"]
    return None
