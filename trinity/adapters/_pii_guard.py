# -*- coding: utf-8 -*-
"""适配器写入边界的 **PII 守卫**（t48/G7）—— **复用 G2 的同一策略与开关**。

## 为什么需要
客户端层（`trinity/core/client/_ingestion.py`，G2/t43）只覆盖「**经客户端**的写入」。
G1（t42）§5 枚举出 **5 条未覆盖路径**：聚合池、daemon/memory/brain/evolution/vms/pipeline
**直写适配器**、`scripts/` 下 35 个脚本、镜像回填。G1 自己的结论是：
**「不下沉 adapter 层，『所有 PII 都掩码』不成立」**。

## 设计原则（三条，逐条可核）
1. **不另造策略**：判定/掩码/开关全部来自 `trinity.security.sensitive`（G2）——
   `scan_sensitive()` 判定、`redact_identifiers()` 掩码、`sensitive_redact_enabled()` 主开关。
   两套策略必然漂移，所以这里只做**转接**，不复制任何正则。
2. **与既有注入守卫同构**：本适配器 **2026-09-13 已经为「注入扫描」做过同一件事** ——
   `trinity/security/injection.py::adapter_write_guard`（见 `sqlite/_crud.py` 的调用点注释：
   「注入扫描此前只挂 client.ingest，而 consolidator/compressor/extractor/evolution/self_model
   等生产路径**直写本方法** ⇒ 侧门敞开」）。本模块**照抄那个约定**：
   返回 `{"scanned","redacted","labels","severity","refuse","isolate","exempt","policy"}`，
   其中 `isolate=True` ⇒ 调用方应以 `status='archived'` 落库。
3. **幂等 + 可回滚 + 可显式退出**：
   · 若 `metadata["pii_redaction"]` 已存在（客户端已掩过）⇒ **直接放行**，不重扫、不覆盖来源标签
     （这样 `redaction_source` 仍是 `ingestion`，G3 的账本语义不被污染，G2 的计数也不会双记）；
   · `TRINITY_SENSITIVE_REDACT=0`（**G2 的同一开关**）⇒ 完全不介入；
   · `TRINITY_ADAPTER_GUARD=0`（本边界专用）⇒ 供**评测语料 / 镜像回填**这类
     「本就不该掩码」的路径显式退出（G1 §5 第 12/14 条的正当需求）。

## 失败模式（明确写出，不藏）
守卫自身异常 ⇒ **不阻断写入**（fail-open，与注入守卫同款 `swallow` 风格），
但记一条 `logging.warning`（`ADAPTER-PII-GUARD-FAILED`）⇒ 便于事后发现"守卫静默失效"。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_OFF = ("0", "off", "false", "no", "")

#: 无需扫描的极短内容（与"文本太短不可能含 PII"同义；避免给空写/摘要路径加固定开销）
_MIN_LEN = 6


def adapter_guard_state() -> Tuple[bool, str]:
    """本边界是否介入 + 原因（原因进 `info["exempt"]`，便于审计"为什么没管"）。

    **三个开关的语义（t49 与 G2 对齐后钉死）**：

    | 开关 | 语义 | 本边界 |
    |---|---|---|
    | `TRINITY_SENSITIVE_SCAN=off` | **完全不扫描**（主开关） | **必须尊重** ⇒ 不介入（`scan-off`） |
    | `TRINITY_SENSITIVE_REDACT=0` | 扫描但**不掩码** | **必须尊重** ⇒ 不介入（`redact-off`） |
    | `TRINITY_ADAPTER_GUARD=0` | **仅**跳过 adapter 层的守卫 | 本层退出（`guard-off`） |

    ⚠️ **t49 修的回归**：原先**只看 REDACT**、不看 `SCAN=off` ⇒ 开关名「关掉敏感扫描」**名不副实**
    （high 仍被拒存、内容仍被掩码）。⇒ 现在**同源**读 G2 的 `sensitive_scan_enabled()`
    （**不自己解析字符串**，避免两套解析漂移）。
    """
    if str(os.environ.get("TRINITY_ADAPTER_GUARD", "on")).strip().lower() in _OFF:
        return False, "guard-off"
    try:
        from trinity.security import sensitive as _s

        if not _s.sensitive_scan_enabled():
            return False, "scan-off"          # t49：主开关 —— 不扫描就别谈掩码/拒存
        if not _s.sensitive_redact_enabled():
            return False, "redact-off"
        return True, ""
    except Exception as _e:  # noqa: BLE001 — 策略层不可用 ⇒ 不介入（行为与改动前一致）
        logger.warning("ADAPTER-PII-GUARD-DISABLED: 策略层不可导入: %r", _e)
        return False, "policy-unavailable"


def adapter_guard_enabled() -> bool:
    """兼容壳：只看"是否介入"。"""
    return adapter_guard_state()[0]


def adapter_pii_guard(content: str, metadata: Optional[Dict[str, Any]] = None
                      ) -> Tuple[str, Optional[Dict[str, Any]], Dict[str, Any]]:
    """适配器写入边界的 PII 守卫。

    Returns:
        `(content, metadata, info)`：前两项可能已被**替换**（掩码后的正文 + 账本），
        `info` 为判定结果（见模块 docstring 的键说明）。**调用方必须使用返回的前两项**。
    """
    info: Dict[str, Any] = {"scanned": False, "redacted": False, "labels": [], "severity": None,
                            "refuse": False, "isolate": False, "exempt": None, "policy": None}
    try:
        if not content or len(content) < _MIN_LEN:
            info["exempt"] = "too-short"
            return content, metadata, info
        _on, _why = adapter_guard_state()
        if not _on:
            info["exempt"] = _why                # t49：`scan-off` / `redact-off` / `guard-off` 可分别读出
            return content, metadata, info
        if isinstance(metadata, dict) and metadata.get("pii_redaction"):
            # 客户端已掩过 ⇒ 幂等放行（不重扫、不覆盖来源标签、不让 G2 计数双记）
            info["exempt"] = "already-recorded"
            info["policy"] = (metadata.get("pii_redaction") or {}).get("policy")
            return content, metadata, info

        from trinity.security import sensitive as _s

        rep = _s.scan_sensitive(content) or {}
        info["scanned"] = True
        info["severity"] = rep.get("severity")
        info["policy"] = rep.get("policy")
        action = rep.get("action") or "store"

        # high 档：**不掩码、不静默落库** —— 与客户端层同语义（refuse / quarantine）
        if action == _s.ACTION_REFUSE:
            info["refuse"] = True
            return content, metadata, info
        if action == _s.ACTION_QUARANTINE:
            info["isolate"] = True
            return content, metadata, info

        if action == _s.ACTION_REDACT:
            masked, labels = _s.redact_identifiers(
                content, cause=("category" if rep.get("flagged") else "pii"))
            if labels:
                md = dict(metadata or {})
                md["pii_redaction"] = {
                    "policy": ("category" if rep.get("flagged") else "all_pii"),
                    "kinds": [str(x) for x in labels],
                    "count": len(labels),
                    "scanner": "regex_v1+adapter_guard",
                    # **哪一层做的**：G3 的 `redaction_source` 据此区分 ingestion / adapter
                    # （客户端层的账本没有这个键 ⇒ 默认按 ingestion 读，语义不变）。
                    "layer": "adapter",
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                info["redacted"] = True
                info["labels"] = [str(x) for x in labels]
                return masked, md, info
        return content, metadata, info
    except Exception as _e:  # noqa: BLE001 — fail-open（不阻断写入），但留痕
        logger.warning("ADAPTER-PII-GUARD-FAILED: %r（本次写入未脱敏，请查此日志）", _e)
        info["error"] = repr(_e)
        return content, metadata, info
