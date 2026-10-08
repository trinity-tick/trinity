"""Trinity client - ingestion & write pipeline mixin (split from client.py, 2026-08-17).

Part of the Trinity client package decomposition. Behavior identical to
the pre-split single-file implementation.
"""

import hashlib
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced

from ._base import _ClientMixinBase
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None

logger = logging.getLogger("trinity.core.client.ingestion")  # 2026-09-11: 写入门告警


def _is_eval_namespace(agent_id, category) -> bool:
    """评测/压测命名空间判定（658.81/658.82）。

    benchmark/lme/stress-test 类目，或 eval-*/ablate*/bench-* 前缀的 agent。
    用途：跳过 ① SAGE 图谱摄取、② 审计链写入——这两者都是生产级记录，
    评测语料（单次评测可灌 1.5 万块）混入会污染图谱与审计链并增加开销。
    检索侧的排除由 _search.py 的 _RETRIEVAL_EXCLUDE_CATEGORIES 负责（同口径）。
    """
    try:
        c = str(category or "").lower()
        a = str(agent_id or "").lower()
        return (c in ("benchmark", "lme", "stress-test")
                or a.startswith(("eval-", "ablate", "bench-")))
    except Exception:
        return False


def _admission_verdict(adapter, *, content, importance, category, persona_id, agent_id, mode) -> Dict[str, Any]:
    """写入侧准入判定的**唯一入口**（annotate / on 共用；off 由调用方短路）。

    判据的**单一来源** = `scripts.memory_write_policy.admission_decision`（纯函数，dry-run 与生产同一份规则）。
    返回 `{"action","reason","dup_checked","policy"}`：
      · action ∈ {"keep","drop"}；reason = 判据名（可审计、可 SQL 计数）；
      · dup_checked = 是否真的查过"库内是否已有活跃同 hash 行"（查不了就 false，宁可多留）。

    fail-open：准入是**可用性之外**的优化，自身出错一律 keep（reason=admission_error），
    绝不因为判据坏了而阻断写入。异常原因仍进 reason，故"恒 admission_error"可被 SQL 发现。
    """
    try:
        _root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))       # trinity/core/client -> ROOT
        for _p in (os.path.join(_root, "scripts"), _root):
            if _p and _p not in sys.path:
                sys.path.insert(0, _p)
        from scripts.memory_write_policy import ADMISSION_POLICY_V1, admission_decision
        row: Dict[str, Any] = {"importance": importance, "category": category, "content": content}
        dup_checked = False
        if callable(getattr(adapter, "check_content_hash_collision", None)):
            try:
                _h = hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()
                row["_dup_active_in_db"] = adapter.check_content_hash_collision(
                    persona_id, agent_id, _h) is not None
                dup_checked = True
            except Exception as _e:      # 单条探测失败不算错误：当作"不重复"
                swallow(__name__, _e)
        _act, _why = admission_decision(row)
        return {"action": _act, "reason": _why, "dup_checked": dup_checked,
                "policy": dict(ADMISSION_POLICY_V1), "mode": mode}
    except Exception as _e:
        swallow(__name__, _e)
        return {"action": "keep", "reason": "admission_error", "dup_checked": False,
                "policy": {}, "mode": mode}


class _IngestionMixin(_ClientMixinBase):
    def ingest_code(
        self,
        content: str,
        language: str = "python",
        file_path: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """写入代码记忆，自动提取语言/函数名/imports 等元数据。

        Args:
            content: 代码文本。
            language: 编程语言（python/javascript/go/rust 等）。
            file_path: 源代码文件路径（可选）。
            **kwargs: 透传给 ingest() 的其它参数。

        Returns:
            ingest() 结果。
        """
        from trinity.core.code_analyzer import analyze_code

        analysis = analyze_code(content, language)
        metadata = {
            "language": language,
            "functions": analysis.get("functions", []),
            "imports": analysis.get("imports", []),
            "classes": analysis.get("classes", []),
            "loc": analysis.get("loc", len(content.splitlines())),
        }

        return self.ingest(
            content=content,
            modality="code",
            metadata=metadata,
            source_uri=file_path,
            **kwargs,
        )
    def ingest_image_description(
        self,
        description: str,
        image_source: Optional[str] = None,
        image_dimensions: Optional[Dict[str, int]] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """写入图片描述记忆。

        Args:
            description: 图片的文字描述。
            image_source: 图片来源 URL 或本地路径。
            image_dimensions: {"width": 1920, "height": 1080} 格式的尺寸信息。
            **kwargs: 透传给 ingest() 的其它参数。

        Returns:
            ingest() 结果。
        """
        metadata = {"source": image_source} if image_source else {}
        if image_dimensions:
            metadata["dimensions"] = image_dimensions

        return self.ingest(
            content=description,
            modality="image_description",
            metadata=metadata,
            source_uri=image_source,
            **kwargs,
        )
    def ingest_trace(
        self,
        steps: List[str],
        task_name: str = "",
        elapsed_seconds: Optional[float] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """写入执行轨迹记忆。

        Args:
            steps: 步骤描述列表，如 ['Step 1: 读取文件', 'Step 2: 解析 JSON']。
            task_name: 任务名称。
            elapsed_seconds: 总耗时（秒）。
            **kwargs: 透传给 ingest() 的其它参数。

        Returns:
            ingest() 结果。
        """
        content = f"[Trace] {task_name}\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(steps))
        metadata = {
            "step_count": len(steps),
            "task_name": task_name,
        }
        if elapsed_seconds is not None:
            metadata["elapsed_seconds"] = elapsed_seconds

        return self.ingest(
            content=content,
            modality="trace",
            metadata=metadata,
            **kwargs,
        )
    @traced("memory.ingest")
    def ingest(
        self,
        content: str,
        source_window: str = "",
        wait_backfill: bool = False,  # 2026-09 (EXECUTION 165): 短进程同步回填
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
        metadata: Optional[Dict[str, Any]] = None,
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        agent_id: str = "default",
        ttl_seconds: Optional[int] = None,
        modality: str = "text",
        source_uri: Optional[str] = None,
        postprocess: bool = True,
    ) -> Dict[str, Any]:
        """Write memory (CRDT versioned, SHA-256 audited).

        Args:
            content: Memory text content.
            source_window: Source window identifier.
            role: user/assistant/system.
            importance: Importance 0-1（2026-09 EXECUTION 105.9：默认值 0.5
            时由 quick_value 规则启发式实时填充——写入瞬间即有价值信号，
            零 LLM 依赖；深度五因素评估由每日 value-recalib 补标）。
            tags: List of tags.
            category: Memory category.
            metadata: Additional metadata dict.
            persona_id: Persona/user identifier (multi-tenant).
            session_id: Session identifier (multi-tenant).
            tenant_id: Tenant/organization identifier (multi-tenant).
            agent_id: Agent identifier (namespace isolation).
            ttl_seconds: Time-to-live in seconds (None = never expire).
            modality: Memory modality (text/image_description/code/json/trace/audio_transcript).
            source_uri: Original file path or URL (optional).

        Returns:
            Dict with memory_id, version_id, sha256_hash, timestamp, pushed_memories.
        """
        # 2026-09-21 §1033 **评测语料 persona 隔离（写侧）**。
        # 背景：19,195 行 benchmark 语料与生产记忆同挂 persona=default（实测见 EXECUTION §1026）。
        # 为什么必须与存量迁移**同一轮**：唯一索引是 (persona_id, agent_id, content_hash)，
        # 只改写侧 ⇒ 下一次评测会在新 persona 下再写一份（内容跨 persona 翻倍）。
        # 开关：TRINITY_EVAL_PERSONA=0 恢复旧行为（回滚）。
        if os.environ.get("TRINITY_EVAL_PERSONA", "1") == "1":
            if (agent_id or "").startswith("eval-") or (category or "") == "benchmark":
                persona_id = "eval-corpus"

        tags = tags or []
        # 2026-09-11（ROADMAP P0#3 写确认门）：**写前**声明式约束门。
        # 对标 Semantica 的 SHACL 校验层，但不引 pyshacl/rdflib——
        # 规则声明在 trinity/audit/constraints.yaml，报告用 SHACL 词表。
        # 默认 off（零行为变化）；warn=审计放行；on=critical 违规拒绝写入。
        try:
            from trinity.audit.constraints import (check_record, gate_mode,
                                                   should_block)
            _gate_mode = gate_mode()
            if _gate_mode != "off":
                _gate_record = {
                    "content": content, "role": role, "importance": importance,
                    "tags": tags, "category": category, "persona_id": persona_id,
                    "tenant_id": tenant_id, "agent_id": agent_id,
                    "modality": modality, "source_uri": source_uri,
                    "ttl_seconds": ttl_seconds,
                }
                _gate_report = check_record(_gate_record, focus_node="memory-write")
                if not _gate_report.conforms:
                    logger.warning("write gate (%s): %s 条违规 %s", _gate_mode,
                                   len(_gate_report.results), _gate_report.by_severity())
                    if (self._adapter and hasattr(self._adapter, "write_audit_log")
                            and not _is_eval_namespace(agent_id, category)):
                        self._adapter.write_audit_log(
                            memory_id=None, action="WRITE_GATE",
                            agent_id=agent_id, persona_id=persona_id,
                            details={"mode": _gate_mode,
                                     "blocked": bool(should_block(_gate_report)),
                                     "report": _gate_report.to_dict()},
                        )
                if should_block(_gate_report):
                    return {
                        "memory_id": None, "version_id": None,
                        "sha256_hash": None, "timestamp": None,
                        "pushed_memories": [], "rejected": True,
                        "error": "write_gate_rejected",
                        "gate": _gate_report.to_dict(),
                    }
        except Exception as _gate_exc:  # 门自身故障绝不阻断写入
            logger.warning("write gate 跳过: %s", _gate_exc)

        # 2026-09（EXECUTION 105.9）写入时实时价值编码：importance 为默认值
        # 0.5 时用 quick_value 规则启发式填充（毫秒级、零 LLM 依赖、失败静默）
        # ——写入瞬间即有价值信号；深度五因素 LLM 评估由每日 value-recalib 补标。
        if importance == 0.5:
            try:
                from trinity.brain.value_encoder import quick_value
                importance = quick_value(str(content or ""), str(category or ""))
            except Exception as _e:
                swallow(__name__, _e)
        # 2026-09 (EXECUTION 132): 情感层——写入即情感标记（杏仁核通路扩展，零 LLM）
        try:
            from trinity.brain.affect import assess as _affect_assess
            _aff = _affect_assess(str(content or ""))
            if _aff.get("polarity") != "neu":
                metadata = dict(metadata or {})
                metadata["affect"] = {"valence": _aff["valence"],
                                       "arousal": _aff["arousal"],
                                       "polarity": _aff["polarity"]}
        except Exception as _e:
            swallow(__name__, _e)
        # 2026-09-09 大脑化优化：情感价强度 emotional_salience（此前全库仅 10 条）
        # 词典价（零 LLM）+ 已有 affect 融合；TRINITY_VALENCE_TAGGING=on 才生效。
        try:
            from trinity.brain.valence import is_enabled as _val_on, tag_metadata as _tag_valence
            if _val_on():
                metadata = _tag_valence(metadata, str(content or ""))
        except Exception as _e:
            swallow(__name__, _e)
        # 2026-09-09 情感惯性（EVD/Affective Inertia）：跨会话情绪基调 EMA
        try:
            from trinity.brain.affective_inertia import is_enabled as _ai_on, update as _ai_update
            if _ai_on() and isinstance(metadata, dict) and metadata.get("affect"):
                _aff2 = metadata["affect"]
                _ai_update(float(_aff2.get("valence", 0.0)), float(_aff2.get("arousal", 0.3)),
                           persona=str(persona_id or "default"))
        except Exception as _e:
            swallow(__name__, _e)

        # 2026-09-02（Fable 5.1 对照审计 P0-②）：provenance_role 强制归一——
        # 记忆来源语义：explicit（用户明示）/ inferred（系统推断）/
        # derived（派生产物：摘要/感知/代码/轨迹/知识导入等）。写路径强制
        # 落 metadata.provenance_role：显式传入优先；否则按 role/modality/
        # content_type 启发式推断。检索侧据此区分"用户说过的话"与"系统的
        # 推测/加工物"，防止推断内容被当作事实长期固化（Fable 泄露揭示的
        # 记忆治理第一问：did the user provide it, or did the model infer it?）。
        metadata = dict(metadata or {})
        # 2026-09-02（Fable 对照审计 P1-④）：条目级 expires_at 归一——datetime
        # 转 ISO 字符串（JSON 可落库）；过去时刻由下方"写入即到期"块即时归档。
        _ea_val = metadata.get("expires_at")
        if isinstance(_ea_val, datetime):
            metadata["expires_at"] = _ea_val.isoformat()
        _prov = metadata.get("provenance_role")
        if _prov not in ("explicit", "inferred", "derived"):
            _ct = str(metadata.get("content_type") or "")
            if role in ("assistant", "system") or modality != "text" \
                    or _ct in ("kb", "kb_harvested", "wms_knowledge", "harvested") \
                    or metadata.get("generated") is True:
                _prov = "derived"
            elif metadata.get("user_verbatim") is True or metadata.get("user_stated") is True:
                _prov = "explicit"
            else:
                _prov = "inferred"
            metadata["provenance_role"] = _prov

        # 2026-08-16(基建夯实):压测/锁测试写入隔离        # 2026-08-16(基建夯实):压测/锁测试写入隔离——已知测试 agent/category/
        # 标签/内容标记的写入强制 archived(仍落库可查、不占 active 检索面)。
        # 开关 TRINITY_ISOLATE_TEST_WRITES=off 可关闭。
        isolated_test_write = self._is_isolated_test_write(
            agent_id=agent_id, category=category, tags=tags, content=content)

        # 2026-08-24（R8 P1-6）：记忆投毒写入扫描（OWASP AG 类）——
        # 命中高危注入模式（指令覆盖/角色仿冒/数据外泄/恶意指令）的写入
        # 强制归档（仍落库、不进 active 检索面），与压测隔离同一机制；
        # 中危仅打 metadata 标记。TRINITY_INJECTION_SCAN=off 关闭。
        injection_report: Optional[Dict[str, Any]] = None
        try:
            from trinity.security.injection import injection_scan_enabled, scan_injection
            if injection_scan_enabled():
                injection_report = scan_injection(content or "")
                if injection_report.get("flagged"):
                    metadata = dict(metadata or {})
                    metadata["injection_scan"] = {
                        "severity": injection_report.get("severity"),
                        "patterns": [h["pattern"] for h in injection_report.get("hits", [])],
                    }
                    if injection_report.get("severity") == "high":
                        isolated_test_write = True  # 复用隔离归档机制
        except Exception as _e:
            # 2026-09-11（审计 R2-1）：扫描失败**仍不阻断写入**（可用性优先，原设计），
            # 但**不再静默** —— 此前失败即 injection_report=None 后无声放过，导致
            # 「扫描器一坏，注入防护就静默失效且无人知晓」（审计④ 把它列为最危险一类）。
            # 现复用既有 metadata 机制给本次写入打标 scan_status.injection=failed，
            # 使"这笔写入未经扫描"**可被 SQL 直接计数**、可回溯、可告警。
            # 开关 TRINITY_SCAN_FAILURE_TAG（默认 on）；打标本身失败亦静默。
            injection_report = None
            try:
                if os.environ.get("TRINITY_SCAN_FAILURE_TAG", "on").lower() in ("1", "on", "true", "yes"):
                    metadata = dict(metadata or {})
                    _ss = dict(metadata.get("scan_status") or {})
                    _ss["injection"] = "failed"
                    metadata["scan_status"] = _ss
            except Exception as _e:
                swallow(__name__, _e)

        # 2026-09-14（684，H1-1 接线工程）：**反射注意** → 注意力总线异常信号。
        # 动机：reflex_attention 属 unbound（代码在、无调用者）。语义 = "免主动注意的异常
        # 自动捕获"；此前含「失败/超时/崩溃/异常」等偏差词的写入**不触发任何注意信号**。
        # 约束：① 只在高危（>=2 个偏差词）时发信号，不把总线当日志；
        #       ② 走既有 emit_signal（去重窗口 + 消费侧日预算），失败静默；
        #       ③ 运行期计数：signals.jsonl 中 signal=reflex_anomaly 的行数。
        # 开关 TRINITY_REFLEX_ATTENTION=off。
        try:
            if os.environ.get("TRINITY_REFLEX_ATTENTION", "on").lower() not in ("off", "0", "false"):
                from trinity.brain.reflex_attention import reflex_flag
                _rf = reflex_flag(content or "")
                if _rf.get("flagged") and _rf.get("severity") == "high":
                    from trinity.brain.attention import emit_signal
                    emit_signal("reflex_anomaly", key=str(agent_id or "")[:64],
                                payload={"anomalies": _rf.get("anomalies"),
                                         "severity": _rf.get("severity"),
                                         "source": "ingest"})
        except Exception as _e:
            swallow(__name__, _e)
        # 2026-09-14（683，H1-1 接线工程）：**来源可信度** → 写入置信修正。
        # 动机：trinity/brain/source_credibility.py 属注册表 unbound（代码在、无调用者）；
        # 其语义正是写入路径该管的事——"防传闻洗成自信事实"（FACTWASH 2026）。
        # 设计约束（三条，均有理由）：
        #   ① **只有声明了来源的内容**才修正（`[web`/`[log`/`[social`/`[action-experience`…）；
        #      未声明来源的内容 source=unknown(0.4)，若一律套用会把普通记忆的 importance
        #      砍到 40% —— 那是行为剧变，不是接线；
        #   ② **只下调不上调**（高可信来源不得成为 importance 放大器）；
        #   ③ 纯函数、无 IO，失败静默（可用性优先）；TRINITY_SOURCE_CREDIBILITY=off 关闭。
        # 运行期计数：产物写进 metadata.source_credibility，可直接 SQL 计数对账。
        try:
            if os.environ.get("TRINITY_SOURCE_CREDIBILITY", "on").lower() not in ("off", "0", "false"):
                from trinity.brain.source_credibility import adjust_confidence
                _sc = adjust_confidence(content or "", base_confidence=float(importance or 0.5))
                if _sc and _sc.get("source") not in (None, "unknown"):
                    metadata = dict(metadata or {})
                    metadata["source_credibility"] = _sc
                    _adj = float(_sc.get("adjusted_confidence") or 0.0)
                    if 0.0 < _adj < float(importance or 0.5):
                        importance = round(_adj, 3)
        except Exception as _e:
            swallow(__name__, _e)
        # 2026-09-02（Fable 5.1 对照审计 P0-①）：敏感类别写入门控——对齐
        # Anthropic 泄露揭示的"至死不记"隐私禁区（未成年身份/法律敏感/
        # 心理诊断/性史/自残轻生）。高危命中默认**拒存**（内容不落库，
        # 审计 action=POLICY_PURGE）；TRINITY_SENSITIVE_POLICY=quarantine
        # 降级为隔离归档（落库但 archived 不进检索面，审计
        # action=POLICY_QUARANTINE）；中危仅打 metadata["sensitive_scan"]
        # 标记。TRINITY_SENSITIVE_SCAN=off 关闭（默认 on）。
        sensitive_report: Optional[Dict[str, Any]] = None
        sensitive_quarantine = False
        try:
            from trinity.security.sensitive import (
                ACTION_REDACT, policy_block_result, redact_identifiers, scan_sensitive,
                sensitive_redact_enabled, sensitive_scan_enabled)
            if sensitive_scan_enabled():
                sensitive_report = scan_sensitive(content or "")
                if sensitive_report.get("flagged"):
                    metadata = dict(metadata or {})
                    metadata["sensitive_scan"] = {
                        "severity": sensitive_report.get("severity"),
                        "categories": sensitive_report.get("categories", []),
                    }
                # 2026-10-06（G2/t43）：掩码条件由"在 flagged 分支内"改为
                # **有 PII 即掩码**。范围判定在 `scan_sensitive` 的 `action` 里
                # （TRINITY_SENSITIVE_REDACT_SCOPE=all_pii 默认 / =category 精确回滚本次改动）。
                # high 档不走这里（action=refuse/quarantine ⇒ 下面的 high 分支）。
                # 回滚：TRINITY_SENSITIVE_REDACT=0 ⇒ 完全不掩码（= t5 接线之前的形态）。
                if (sensitive_report.get("action") == ACTION_REDACT
                        and sensitive_redact_enabled()):
                    _redacted, _labels = redact_identifiers(
                        content or "",
                        cause=("category" if sensitive_report.get("flagged") else "pii"))
                    if _labels:
                        content = _redacted
                        metadata = dict(metadata or {})
                        metadata["pii_redaction"] = {
                            "policy": ("category" if sensitive_report.get("flagged")
                                       else "all_pii"),
                            "kinds": _labels,
                            "count": len(_labels),
                            "scanner": "regex_v1",
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                if sensitive_report.get("severity") == "high":
                        if sensitive_report.get("policy") == "quarantine":
                            sensitive_quarantine = True
                            isolated_test_write = True  # 复用隔离归档机制
                        else:
                            # refuse（默认策略）：拒存——审计 POLICY_PURGE 后
                            # 直接返回，内容根本不落库（Fable 语义"强制不记"）。
                            try:
                                if self._adapter and hasattr(
                                        self._adapter, "write_audit_log"):
                                    self._adapter.write_audit_log(
                                        memory_id=None, action="POLICY_PURGE",
                                        agent_id=agent_id, persona_id=persona_id,
                                        details={
                                            "category": category, "tags": tags,
                                            "severity": sensitive_report.get("severity"),
                                            "sensitive_categories":
                                                sensitive_report.get("categories", []),
                                            "labels": [h["pattern"] for h in
                                                       sensitive_report.get("hits", [])],
                                            "policy": "refuse",
                                        },
                                    )
                            except Exception as _e:
                                swallow(__name__, _e)
                            return policy_block_result(sensitive_report)
        except Exception as _e:
            # 2026-09-11（审计 R2-1）：同注入侧——失败不阻断，但打标可见。
            sensitive_report = None
            try:
                if os.environ.get("TRINITY_SCAN_FAILURE_TAG", "on").lower() in ("1", "on", "true", "yes"):
                    metadata = dict(metadata or {})
                    _ss = dict(metadata.get("scan_status") or {})
                    _ss["sensitive"] = "failed"
                    metadata["scan_status"] = _ss
            except Exception as _e:
                swallow(__name__, _e)

        # ── 2026-09-19（EXECUTION §915.3）：写入侧准入**接线**（此前只有纯函数 + dry-run）──
        # 基线（§912.4，7 日实测）：agent_id='default' 写 11,261 条 / 98.9% 从未被读 / 9,898 条重复；
        # 类目 general 11,583 写 / 11,485 冷 / 9,797 重复 ⇒ 第一目标是"别重复写、别写垃圾"。
        # 三档 TRINITY_WRITE_ADMISSION：off / **annotate（默认，只标注不改行为）** / on（拦：落 archived）。
        # 这里同时是"运行期计数"的来源：命中原因落 metadata['write_admission']，
        # 可直接 SQL 计数（无需在热路径额外写状态文件）：
        #   SELECT metadata->'write_admission'->>'reason' AS reason, count(*)
        #   FROM memories WHERE created_at > now() - interval '1 day' GROUP BY 1 ORDER BY 2 DESC;
        admission: Optional[Dict[str, Any]] = None
        try:
            from scripts.memory_write_policy import admission_mode
            _amode = admission_mode()
            if _amode != "off" and not isolated_test_write and self._adapter:
                admission = _admission_verdict(
                    self._adapter, content=content, importance=importance, category=category,
                    persona_id=persona_id, agent_id=agent_id, mode=_amode)
                metadata = dict(metadata or {})
                metadata["write_admission"] = admission
                if _amode == "on" and admission.get("action") == "drop":
                    # 复用既有 B6 机制：状态随**同一条 INSERT** 落 archived（不占 active 检索面）
                    isolated_test_write = True
        except Exception as _e:
            swallow(__name__, _e)

        result: Dict[str, Any] = {}
        if self._adapter:
            result = self._adapter.store_memory(
                content=content,
                persona_id=persona_id,
                session_id=session_id,
                tenant_id=tenant_id or self.tenant_id,
                agent_id=agent_id,
                ttl_seconds=ttl_seconds,
                role=role,
                importance=importance,
                tags=tags,
                category=category,
                modality=modality,
                metadata=metadata,
                source_uri=source_uri,
                # 2026 优化轮 B6：隔离判定在**写入前**已确定（下方 isolated_test_write），
                # 传进适配器以**同一条 INSERT** 落 status='archived'。
                # 此前是"先以 active 提交、再由下方 archive_memories 事后归档"——
                # 两次独立 commit，实测存在窗口：归档失败即静默留在 active 检索面。
                status="archived" if isolated_test_write else None,
            )
        else:
            result = (
                self._adapter.store_memory(
                    content=content, persona_id=persona_id,
                    session_id=session_id, tenant_id=tenant_id or self.tenant_id,
                    agent_id=agent_id, ttl_seconds=ttl_seconds,
                    role=role, importance=importance, tags=tags, category=category,
                    modality=modality, metadata=metadata, source_uri=source_uri,
                ) if self._adapter else {"memory_id": "", "error": "no adapter"}
            )

        memory_id = result.get("memory_id", "")

        # 2026-09 (EXECUTION 126): 写入即建图——SAGE 图谱自动摄入（异步，
        # 节流：每 10 次写入或 60s 才 persist 一次快照，避免高频写入损耗）。
        # 658.81：**评测/压测语料跳过图谱摄入**。评测语料（benchmark/lme/stress-test 类目或
        # eval-*/ablate*/bench-* 命名空间）不参与生产图谱；此前它们同样触发图谱抽取线程——
        # 实测长历史评测灌 1.5 万块 → 约 1.5 万个抽取线程，把 relations 从 6.5 万推到 9 万，
        # 并产生大量无分层行/孤儿边（纯浪费 CPU、内存与库空间）。
        _is_eval_write = _is_eval_namespace(agent_id, category)
        if memory_id and not isolated_test_write and not _is_eval_write:
            try:
                import threading as _th
                _gcontent = str(content or "")[:500]
                if _gcontent.strip():
                    def _graph_ingest(_c: str) -> None:
                        try:
                            eng = self.sage
                            if eng is not None:
                                _cnt = getattr(eng, "_turn_count", 0)
                                eng.ingest_turn(_c, {"source": "ingest"})
                                # 节流持久化：每 10 次或首次
                                if _cnt % 10 == 0:
                                    eng._persist()
                        except Exception as _e:
                            swallow(__name__, _e)
                    _th.Thread(target=_graph_ingest, args=(_gcontent,),
                               daemon=True, name="sage-ingest").start()
            except Exception as _e:
                swallow(__name__, _e)

        # 2026-09（EXECUTION 105.11）：写入即深度价值评估（系统 2 即时化）——
        # 仅当快速评估 >= 0.65（高显著候选）才异步 LLM 深度评估（成本控制：
        # 低价值/普通内容不浪费 LLM）；更新 importance + metadata；失败静默
        # （快速值已足够，写入不阻塞、不失败）。
        if memory_id and importance >= 0.65 and self._adapter:
            try:
                import threading as _th
                _content = str(content or "")
                _mid = memory_id

                def _deep_value() -> None:
                    try:
                        from trinity.brain.value_encoder import estimate_value
                        ev = estimate_value(_content)
                        if not ev or ev.get("value", 0.0) <= 0.5:
                            return
                        # EXECUTION 612（H2 修复）：经当前 adapter 回写价值评分——
                        # 不再硬编码直连 live PG（曾致隔离评测隐性连库 + 源码明文凭据）
                        upd = getattr(self._adapter, "update_importance", None)
                        if not upd:
                            return
                        upd(_mid, float(ev["value"]), None,
                            {"value_model": str(ev.get("version", "")),
                             "value_factors": ev.get("factors", {}),
                             "value_reason": str(ev.get("reason", ""))})
                    except Exception as _e:
                        swallow(__name__, _e)

                _th.Thread(target=_deep_value, daemon=True,
                           name="ingest-deep-value").start()
            except Exception as _e:
                swallow(__name__, _e)

        # 2026-09（EXECUTION 105.19）写入缓存失效：语义缓存无写入失效——
        # 写入后同 query 300s 内返回旧结果（一致性缺陷，实测 gap_fill 场景
        # 被缓存遮蔽）；写入后清空语义缓存（写入低频，命中损失可接受；
        # TRINITY_CACHE_BACKEND=off 时 invalidate 为 no-op）。
        # EXECUTION 622（P0-B4）：失效旋钮 TRINITY_CACHE_INVALIDATE=full|off
        # （默认 full=现状全清保证强一致；off 依赖 TTL 300s，供高写频批量
        # runner 选用——一致性窗口 ≤300s）。按前缀/session 粒度需缓存 key
        # schema v2（现 key 无结构化前缀），另行窗口。
        if os.environ.get("TRINITY_CACHE_INVALIDATE", "full").strip().lower() != "off":
            try:
                from trinity.core.cache import get_cache
                get_cache().invalidate(pattern="*")
            except Exception as _e:
                swallow(__name__, _e)

        # 隔离写入:立即归档(不进入 active 检索面),并留审计痕迹
        #
        # 2026 优化轮 B6：状态已在 `store_memory(status="archived")` 内与记忆行
        # 同一条 INSERT 落库 ⇒ 本段不再是"唯一的隔离手段"，降级为**幂等复核**
        # （archive_memories 对已 archived 行是 no-op）。两处改动：
        #   ① 归档失败**不再吞掉审计** —— 审计是治理记录，必须与归档独立成败
        #      （原先二者同处一个 try，归档一失败审计连带丢失）；
        #   ② 归档失败改为**显式 ERROR 日志**（原为纯静默 swallow，违反本模块
        #      写路径 fail-closed 原则；历史事故正是"静默进了 active 面"）。
        if isolated_test_write and memory_id and self._adapter:
            try:
                self._adapter.archive_memories([memory_id])
            except Exception as _e:
                logger.error(
                    "B6 隔离归档复核失败（记忆行已按 status='archived' 落库，"
                    "此处仅复核；若适配器未支持 status 参数则隔离可能未生效）: %s", _e)
                swallow(__name__, _e)
            try:
                action = "ISOLATED_TEST_WRITE"
                details: Dict[str, Any] = {"category": category, "tags": tags}
                if admission and admission.get("mode") == "on" and admission.get("action") == "drop":
                    # 2026-09-19（§915.3）：写入侧准入拦截单独记审计（与压测隔离/注入隔离区分开）
                    action = "WRITE_ADMISSION_DROP"
                    details = {"category": category, "reason": admission.get("reason"),
                               "dup_checked": admission.get("dup_checked"),
                               "policy": admission.get("policy")}
                elif sensitive_quarantine:
                    # 2026-09-02（Fable 对照审计 P0-①）：敏感内容隔离归档单独记审计
                    action = "POLICY_QUARANTINE"
                    details = {
                        "severity": (sensitive_report or {}).get("severity"),
                        "sensitive_categories":
                            (sensitive_report or {}).get("categories", []),
                        "policy": "quarantine",
                    }
                elif injection_report is not None and injection_report.get("flagged"):
                    # 2026-08-24（R8 P1-6）：投毒注入隔离单独记审计
                    action = "INJECTION_ISOLATED"
                    details = {
                        "severity": injection_report.get("severity"),
                        "patterns": [h["pattern"] for h in injection_report.get("hits", [])],
                    }
                # 658.82：**评测/压测写入不进审计链**——审计链是治理记录，评测语料（单次
                # 评测可灌 1.5 万块）会把链从 8 万条推到 9.6 万条，淹没真实操作记录，
                # 并增加每次写入的开销。评测写入已由类目/命名空间排除在检索与图谱之外，
                # 此处保持一致（安全网另有 eval_purged 兜底）。
                if hasattr(self._adapter, "write_audit_log") and not _is_eval_namespace(agent_id, category):
                    self._adapter.write_audit_log(
                        memory_id=memory_id, action=action,
                        agent_id=agent_id, persona_id=persona_id,
                        details=details,
                    )
            except Exception as _e:
                swallow(__name__, _e)

        # 2026-09-02（Fable 对照审计 P1-④）：条目级 expires_at——写入即到期
        # （expires_at 已是过去时刻）→ 立即归档（不进 active 检索面）+
        # 链式审计 action=EXPIRED_AT（source=ingest）。未来到期由每日
        # maintenance expiry-review 任务扫入复核队列
        # （scripts/run_expiry_review.py，临期 7 天 + 到期两组清单）。
        if memory_id and self._adapter and not isolated_test_write:
            try:
                _ea_v2 = (metadata or {}).get("expires_at")
                if _ea_v2:
                    _ea_dt = (
                        _ea_v2 if isinstance(_ea_v2, datetime)
                        else datetime.fromisoformat(str(_ea_v2).replace("Z", "+00:00")))
                    if _ea_dt.tzinfo is None:
                        _ea_dt = _ea_dt.replace(tzinfo=timezone.utc)
                    if _ea_dt <= datetime.now(timezone.utc):
                        self._adapter.archive_memories([memory_id])
                        if hasattr(self._adapter, "write_audit_log"):
                            self._adapter.write_audit_log(
                                memory_id=memory_id, action="EXPIRED_AT",
                                agent_id=agent_id, persona_id=persona_id,
                                details={"expires_at": str(_ea_v2),
                                         "source": "ingest",
                                         "reason": "already expired at write"},
                            )
            except Exception as _e:
                swallow(__name__, _e)

        # 自动审计日志（同步：核心写入 + 审计链即时落账，保证可信链完整）
        # 658.82：**评测/压测写入不进审计链**——审计链是治理记录，一次评测可灌 1.5 万块，
        # 会把链从 8 万条推到 9.6 万条、淹没真实操作记录并增加写入开销。判定口径与
        # 检索侧（类目排除）和图谱侧（跳过摄取）一致。
        if (self._adapter and hasattr(self._adapter, "write_audit_log")
                and not _is_eval_namespace(agent_id, category)):
            try:
                self._adapter.write_audit_log(
                    memory_id=memory_id, action="create", agent_id=agent_id,
                    persona_id=persona_id,
                    details={"importance": importance, "tags": tags,
                             "category": category, "modality": modality},
                )
            except Exception as _e:
                swallow(__name__, _e)

        # 2026-08-26（Budibase 借鉴 Phase 1）：事件驱动自动化——memory.write
        # 事件（默认关闭 TRINITY_AUTOMATION=off，emit 零开销）。动作经 audit_fn
        # 留痕（action=automation），失败不影响写入主流程。
        # 2026-08-26（二轮）：TRINITY_AUTOMATION_ACTION=1 防循环——自动化动作
        # 子进程内（exec.command 注入）的写入不再触发自动化事件。
        if memory_id and self._adapter and os.environ.get("TRINITY_AUTOMATION_ACTION") != "1":
            try:
                from trinity.automation import emit as _automation_emit
                _automation_emit(
                    "memory.write",
                    {
                        "memory_id": memory_id,
                        "importance": importance,
                        "category": category,
                        "tags": tags,
                        "persona_id": persona_id,
                        "agent_id": agent_id,
                        "modality": modality,
                        "content_preview": (content or "")[:100],
                    },
                    audit_fn=lambda rule, ok, detail: self._adapter.write_audit_log(
                        memory_id=memory_id, action="automation",
                        agent_id=agent_id, persona_id=persona_id,
                        details={"rule": rule, "ok": ok, **detail},
                    ),
                )
            except Exception as _e:
                swallow(__name__, _e)

        # 2026-09-09（优化执行 P1 修复）：中文 tsv 同步回填——此前只在
        # _postprocess_memory 内维护（postprocess=False / 线程未跑时新记忆
        # content_tsv_zh=NULL，当日关键词检索仅剩 ILIKE 兜底 0.1 平权按
        # importance 排序，相关性失效：金丝雀/新写入被无关高 importance 行压顶）。
        # jieba 分词毫秒级；失败静默（夜间 backfill_tsv_zh.py 兜底）。
        # 2026-09-09（658.28 修复）：**embedding 同步回填**——同款缺口：嵌入只在
        # _postprocess_memory 维护，postprocess=False（worker/金丝雀/评测/脚本）
        # 路径写入的行 embedding=NULL → pgvector 通道静默漏检（外部评测实测
        # vector R@5=0，回填后 0.6）。与 tsv 同处同步补，失败静默（夜间
        # backfill_pg_embeddings.py 兜底）。
        if memory_id:
            _adp = self._adapter
            try:
                import jieba as _jb
                _jb.setLogLevel(60)
                if _adp is not None and hasattr(_adp, "set_content_tsv_zh"):
                    _words = [w.strip() for w in _jb.cut(str(content))
                              if w.strip() and len(w.strip()) >= 2][:12]
                    if _words:
                        _adp.set_content_tsv_zh(memory_id, " | ".join(_words))
            except Exception as _e:
                swallow(__name__, _e)
            try:
                if (_adp is not None and hasattr(_adp, "set_embedding")
                        and "postgres" in type(_adp).__name__.lower()):
                    from trinity.core.client._helpers import _get_embedding_engine
                    _eng = _get_embedding_engine()
                    if _eng is not None:
                        _vec = [float(x) for x in _eng.embed(str(content)[:500])]
                        _adp.set_embedding(memory_id, _vec)
            except Exception as _e:
                swallow(__name__, _e)

        # 加工管线（语义关联 + 实体提取 + 主动推送）
        # 2026-08-15（二轮压测修复）：postprocess 默认后台线程执行——
        # 写入即时返回、加工后台完成（_postprocess_memory 幂等、内部异常
        # 保护、daemon 线程），调用方无需再传 postprocess=False 规避同步
        # 加工成本（实测同步管线占写入 ~97%，单条 430-665ms vs 13ms）。
        # result 为共享 dict 引用，后台线程回填 pushed_memories /
        # extracted_entities / postprocess（pending → done），API 返回
        # 时可能仍为 pending，属设计内的异步语义。
        # 例外：TRINITY_LLM_EXTRACT=on 默认异步（2026-08-16 优化，实测真实 LLM
        # 提取 ~4.5s/条，同步会阻塞写路径）；TRINITY_LLM_EXTRACT_SYNC=on 强制同步
        # （调用方期望 ingest 返回时实体/关系已入库，如测试/管线）。
        # 兼容旧开关：TRINITY_LLM_EXTRACT_ASYNC=on 仍为异步（本就是默认）。
        llm_extract = os.environ.get(
            "TRINITY_LLM_EXTRACT", "").strip().lower() in ("1", "on", "true", "yes")
        llm_sync = os.environ.get(
            "TRINITY_LLM_EXTRACT_SYNC", "").strip().lower() in ("1", "on", "true", "yes")
        if postprocess and not isolated_test_write and memory_id:
            # 2026-09 (EXECUTION 165): wait_backfill 时同步执行（短进程场景）
            if wait_backfill:
                try:
                    self._postprocess_memory(memory_id, content, result)
                    result["postprocess"] = "done_sync"
                except Exception:
                    result["postprocess"] = "failed"
                return result
            result.setdefault("pushed_memories", [])
            result["extracted_entities"] = 0
            result["postprocess"] = "pending"
            if llm_extract and llm_sync:
                self._postprocess_memory(memory_id, content, result)
            else:
                threading.Thread(
                    target=self._postprocess_memory,
                    args=(memory_id, content, result),
                    daemon=True, name="ingest-postprocess",
                ).start()
        else:
            result["pushed_memories"] = []
            result["extracted_entities"] = 0
            result["postprocess"] = "pending" if memory_id else "skipped"

        # ── 2026-09-09 大脑化优化：程序性记忆抽取（TRINITY_PROCEDURE_EXTRACT=on）──
        # 从"怎么做"的文本里抽有序步骤 → category=procedural 记忆。
        # [procedure] 前缀做递归保护（procedural 记忆自身不再触发抽取）。
        try:
            _txt = str(content or "")
            if memory_id and not _txt.startswith("[procedure]"):
                from trinity.brain.procedure import (MIN_STEPS as _PS_MIN,
                                                     build_procedural_content as _ps_build,
                                                     extract_steps as _ps_steps,
                                                     is_enabled as _proc_on)
                if _proc_on():
                    _steps = _ps_steps(_txt)
                    if len(_steps) >= _PS_MIN:
                        self.ingest(content=_ps_build(_steps, str(memory_id)),
                                    category="procedural", importance=0.6,
                                    tags=["procedure", "auto"],
                                    agent_id="brain-procedure",
                                    metadata={"steps": _steps,
                                              "source_memory_id": str(memory_id),
                                              "extractor": "procedure_v1"},
                                    postprocess=False)
                        result["procedure_extracted"] = len(_steps)
        except Exception as _e:
            swallow(__name__, _e)

        # ── 2026-09-09 P0-④ fail-closed：写入未返回 memory_id = 静默失败 → 显式报错 ──
        # 读路径保持 fail-open（异常吞掉不影响检索），写路径必须 fail-closed（不静默写坏）。
        try:
            import os as _os_strict
            if _os_strict.environ.get("TRINITY_WRITE_STRICT", "on").lower() in ("on", "1", "true", "yes"):
                if not result.get("memory_id"):
                    raise RuntimeError("write fail-closed: adapter returned no memory_id")
        except RuntimeError:
            raise
        except Exception as _e:
            swallow(__name__, _e)

        return result
    _ISOLATED_TEST_AGENTS = {"stress-agent", "lock-test", "stress-test", "stress-db-writer",
                             "scale-mw", "scale-mwtest", "mw-stress"}
    # 2026-09-02: 补 test-stim（审计发现 test-stim-001~015 活跃污染，写入未隔离）
    # 2026-09-09: 补 scale-mw —— 多写压测脚本 36 秒直写 PG 10 万条 active（agent=scale-mw,
    #             category=benchmark, tag=mwtest），污染检索面并把 API 常驻内存顶到 13.3GB。
    # 2026-09-10（只读全景体检 659 发现的 D 面故障）：评测/消融流水线写入隔离。
    # 实证：ablate-loco 单日 2,263 条 LoCoMo 基准语料以 status=active 进入生产检索面
    # （当日 active 面 benchmark 类目 2,304 条），与 2026-09-09 scale-mw 事故同类，
    # 只是换了入口。评测脚本自身显式 TRINITY_ISOLATE_TEST_WRITES=off（评测期间需可检索），
    # 故本守卫为纵深防御的第一层；第二层是每日链的 benchmark-quarantine 自愈任务。
    # 2026-09-19（§914.2 事故 → §915.3 纵深）：补 "eval-"/"eval_" —— 此前客户端侧漏了这一族，
    # 而 §JEV-15 的 LongMemEval-S 入库（19,195 行）正是 eval-* 命名空间：因为只走了
    # 检索出口过滤（evidence_gate._NONPROD_TOKENS），这些行以 **status=active** 落库，
    # 把 active 面与所有以 active 为分母的读数抬高 74%。本前缀为纵深第一层
    # （评测脚本自身需要可检索时照旧显式 TRINITY_ISOLATE_TEST_WRITES=off）。
    _ISOLATED_TEST_AGENT_PREFIXES = ("eval-", "eval_", "ablate-", "ablate_", "bench-", "bench_",
                                     "benchmark-", "benchmark_", "locomo-", "lme-", "lmev2-",
                                     "stress-", "scale-")
    # benchmark/lme/locomo：评测语料类目——与 precision_tiers 的排除口径保持一致
    _ISOLATED_TEST_CATEGORIES = {"stress-test", "stress_test", "test-stim", "test_stim",
                                 "benchmark", "lme", "locomo", "ablation"}
    _ISOLATED_TEST_TAGS = {"locktest", "stress", "mwtest"}
    # 2026-09-24（§新，先量后判）：把**题材词**标签从"单独即隔离"里拆出来。
    # 实测依据：`audit_log action='ISOLATED_TEST_WRITE'` 全量 38 行中有 **5 行**是
    # 「正常评测总结被题材词标签误隔离」，其中 **4 行来自真实交互会话**
    # （agent=dsh-session-*，category ∈ trinity/session/research，tags 含 benchmark），
    # 跨 09-13 → 09-24 反复发生；内容里既没有评测语料锚点，category 也不在隔离集里。
    # 另注：`trinity-maintenance` 技能文档记的标签契约一直是 {locktest, stress, mwtest}，
    # 题材词是 2026-09-10 扩守卫时加进来的 —— 本次收窄也是**让代码回到已声明的契约**。
    # 新判据：题材词 + **非交互写入者** 才隔离（脚本/评测流水线照旧拦得住，
    # 交互会话里写「我在评测里发现了什么」不再被静默移出 active 面）。
    _ISOLATED_TOPICAL_TAGS = {"benchmark", "locomo", "ablation"}
    _INTERACTIVE_AGENT_PREFIXES = ("dsh-", "dsh_")
    # 评测语料的内容锚点（未带 agent/category 的历史写入也能拦住）
    _ISOLATED_CONTENT_MARKERS = ("[locomo:", "[lme:", "[longmemeval:", "[benchmark:",
                                 "[ablation:", "[stress:")
    def _is_isolated_test_write(
        self, agent_id: str, category: str, tags: Optional[List[str]],
        content: str,
    ) -> bool:
        """判断写入是否为压测/锁测试/自污染类,应隔离出 active 检索面。

        2026-08-16(基建夯实):历史 stress-agent(200)/lock-test(50)/auto-link
        噪音(576)已归档,此守卫防止同类写入再次进入 active 面(写入仍落库,
        测试脚本可正常验证,但检索不再被污染)。TRINITY_ISOLATE_TEST_WRITES=off 关闭。
        """
        if os.environ.get(
            "TRINITY_ISOLATE_TEST_WRITES", "on"
        ).lower() in ("off", "0", "false"):
            return False
        if agent_id in self._ISOLATED_TEST_AGENTS:
            return True
        # 2026-09-10: 前缀匹配——覆盖 ablate-loco / bench-* / lme-* 等评测流水线
        _aid = (agent_id or "").lower()
        if _aid and _aid.startswith(self._ISOLATED_TEST_AGENT_PREFIXES):
            return True
        if category in self._ISOLATED_TEST_CATEGORIES:
            return True
        if any((tg or "").lower() in self._ISOLATED_TEST_TAGS for tg in (tags or [])):
            return True
        # 2026-09-24：题材词标签只对**非交互写入者**生效（依据见 _ISOLATED_TOPICAL_TAGS 注释）
        if (not _aid.startswith(self._INTERACTIVE_AGENT_PREFIXES)
                and any((tg or "").lower() in self._ISOLATED_TOPICAL_TAGS for tg in (tags or []))):
            return True
        if content.startswith("[自动关联]") or "LONG-STRESS" in content:
            return True
        # 2026-09-10: 评测语料内容锚点
        if any(content.startswith(m) for m in self._ISOLATED_CONTENT_MARKERS):
            return True
        # 2026-09-09: 多写压测文案特征（未带 agent/tag 的历史脚本也能拦住）
        if content.startswith("multiwriter memory"):
            return True
        return False
    def _postprocess_memory(
        self, memory_id: str, content: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """新写入记忆的后台加工：语义关联 + 实体提取 + 主动推送。

        从 ingest 同步路径分离，供 memory_write 异步化调用（写入即时返回，
        加工后台完成）。幂等：内部各步骤均有异常保护，失败不抛出。

        2026-08-15（二轮压测修复）：全局 _postprocess_lock 串行化——
        加工是后台异步工作，无需并发（并发 sklearn fit + 抢 _write_lock
        会拖垮写入线程，实测响应 p95 3.7s）；串行后 embedding 引擎只
        fit 一次、写锁竞争收敛到单加工线程。

        Args:
            memory_id: 已写入的记忆 ID。
            content:   记忆内容。
            result:    可选，回填加工结果到已返回的 result 字典。

        Returns:
            Dict with linked_ids / extracted_entities / pushed_memories.
        """
        with self._postprocess_lock:
            linked_ids: List[str] = []
            if self._adapter and hasattr(self._adapter, "create_memory_link"):
                linked_ids = self._auto_link_semantic(memory_id, content)
            entity_ids = self._auto_extract_entities(memory_id, content)
            # 2026-08-24（R5 P1-③）：画像增量钩子接线——PersonaEngine
            # 此前完整实现（12 测试 + 4 端点）但写路径未调用，启用后不生效。
            # 保持 TRINITY_PERSONA/TRINITY_PROPOSITION_EXTRACT 双开关默认
            # off（成本/隐私取舍，网络 2026 画像记忆标配但需 LLM 提取素材），
            # 显式启用后此钩子才触发；失败静默不阻塞写路径。
            try:
                from trinity.memory.persona import persona_enabled, maybe_persona_after_store
                if persona_enabled() and self._adapter is not None:
                    _meta = {}
                    try:
                        _full = self._adapter.get_memory(memory_id) or {}
                        _meta = _full.get("metadata") or {}
                        if isinstance(_meta, str):
                            import json as _json
                            _meta = _json.loads(_meta)
                    except Exception:
                        _meta = {}
                    maybe_persona_after_store(
                        self._adapter,
                        {"content": content, "memory_id": memory_id,
                         "persona_id": _meta.get("persona_id") or "default",
                         "metadata": _meta},
                        {"memory_id": memory_id},
                    )
            except Exception as _e:
                swallow(__name__, _e)
            # ANN 增量维护（①落盘持久化，2026-08-15）：后台线程同步新记忆进索引
            if memory_id and self.use_ann:
                import threading as _th
                _th.Thread(
                    target=self._ann_incremental_add, args=(memory_id, content),
                    daemon=True,
                ).start()
            # 2026-09 (EXECUTION 131): PG 主存储向量+分词回填——新写入记忆
            # 必须可被向量/中文检索（此前仅 backfill 脚本一次性回填，API 写入
            # 的记忆 embedding=NULL 不可向量检索）。幂等 + 失败静默。
            try:
                _adp = self._adapter
                if _adp is not None and hasattr(_adp, "set_embedding"):
                    _tname = type(_adp).__name__.lower()
                    # 2026-09-29（外部审计修复，根因 A「存储层单一权威 + 嵌入冻结」）：
                    # 原实现把**整个块**（含嵌入回填）门控在 `if "postgres" in _tname:`
                    # 之下 ⇒ **SQLite 生效时写入路径根本不生成 embedding**。
                    # 实测后果：SQLite 的 embedding 写入停在 2026-08-26T03:33:52，
                    # 此后 33 天零写入；而服务读的正是 SQLite（pg-only id → 404）
                    # ⇒ 向量通道长期只覆盖 6.5% 语料的冻结索引。
                    #
                    # 现拆开：**嵌入回填对任何提供 set_embedding 的适配器都跑**
                    # （SQLiteAdapter 本轮已补上同契约实现，存原始小端 float32 BLOB）；
                    # **中文分词回填仍限 PG** —— 理由：该调用的入参是
                    # `jieba.cut(content)[:12]`（**只取 12 个词**），在 PG 上是给
                    # `content_tsv_zh` 建 tsvector 用的；而 SQLite 的关键词检索走
                    # `tokenized_content` → FTS5，那里存的是**完整** jieba 分词，
                    # 若让 12 词覆盖上去会**毁掉 SQLite 的 FTS 召回**（静默降质）。
                    _is_pg = "postgres" in _tname
                    # 2026-09-09（优化执行修复，**本轮重构后依然成立、故注释保留**）：
                    # **嵌入与 tsv 回填解耦** —— 此前 embedding 引擎初始化失败
                    # （如 Ollama 不可用）会跳到外层 except 从而跳过整个块，
                    # 中文 tsv 也不回填 → 新写记忆当日在关键词检索只剩 ILIKE 兜底
                    # （score 0.1 平权按 importance 排序，相关性失效）。
                    # 现在**各自独立 try**：嵌入失败不影响 tsv，tsv 失败不影响嵌入。
                    # ── 嵌入回填（后端无关）──────────────────────────────
                    _eng = None
                    try:
                        from trinity.core.client._helpers import _get_embedding_engine
                        _eng = _get_embedding_engine()
                    except Exception:
                        _eng = None
                    try:
                        if _eng is not None:
                            _v = _eng.embed(str(content)[:500])
                            _vec = [float(x) for x in _v]
                            _adp.set_embedding(memory_id, _vec)
                    except Exception as _e:
                        swallow(__name__, _e)
                    # ── 中文分词回填（**PG 专属**，见上）──────────────────
                    if _is_pg:
                        try:
                            import jieba as _jb
                            _jb.setLogLevel(60)
                            _words = [w.strip() for w in _jb.cut(str(content))
                                      if w.strip() and len(w.strip()) >= 2][:12]
                            if _words and hasattr(_adp, "set_content_tsv_zh"):
                                _adp.set_content_tsv_zh(memory_id, " | ".join(_words))
                        except Exception as _e:
                            swallow(__name__, _e)
            except Exception as _e:
                swallow(__name__, _e)

            all_ids = [memory_id] + linked_ids if memory_id else linked_ids
            pushed = self.proactive_push(all_ids)
            if result is not None:
                result["pushed_memories"] = pushed
                result["extracted_entities"] = len(entity_ids)
                result["linked_ids"] = linked_ids
                result["postprocess"] = "done"
            return {
                "linked_ids": linked_ids,
                "extracted_entities": len(entity_ids),
                "pushed_memories": pushed,
            }
    def _auto_link_semantic(
        self, memory_id: str, content: str,
    ) -> List[str]:
        """为新写入的记忆自动创建语义关联链接（向量相似度 > 0.85）。

        批量嵌入（单次引擎调用）+ numpy 向量化相似度计算，避免逐条
        embed 调用导致写入路径超时（11k 记忆库实测 94s → 秒级）。

        可通过环境变量控制：
          - TRINITY_AUTO_LINK=off  关闭自动关联（写入最快速路径）
          - TRINITY_AUTO_LINK_MAX=N  候选记忆上限（默认 100）

        Args:
            memory_id: 新记忆 ID。
            content: 记忆内容。

        Returns:
            成功创建链接的目标记忆 ID 列表。
        """
        if os.environ.get("TRINITY_AUTO_LINK", "on").lower() in ("off", "0", "false"):
            return []
        linked: List[str] = []
        try:
            if not self._embedding_engine:
                from trinity.embeddings import create_engine
                # 2026-08-15（二轮压测修复）：backend="sklearn"——auto 会先
                # 探测 Ollama（本机未开时每次 embed 等 ~300ms 超时，embed_batch
                # 100 条 → 30s+，导致后台加工线程长时间"卡住"）。与聚合器
                # _get_embedding_fn 的修复一致：sklearn TF-IDF 确定性毫秒级。
                self._embedding_engine = create_engine(backend="sklearn")
            import numpy as np

            # 获取已有记忆（候选上限可配置）
            existing = []
            if self._adapter and hasattr(self._adapter, "get_all_memories"):
                try:
                    max_candidates = int(os.environ.get("TRINITY_AUTO_LINK_MAX", "100"))
                except ValueError:
                    max_candidates = 100
                existing = self._adapter.get_all_memories(limit=max_candidates)
            if not existing:
                return linked

            # 候选对齐：仅保留有内容、非自身的记忆
            candidates = [
                (mem, mem.get("content", ""))
                for mem in existing
                if mem.get("memory_id") and mem.get("memory_id") != memory_id
                and mem.get("content")
            ]
            if not candidates:
                return linked

            # 批量嵌入：新内容 + 全部候选，单次引擎调用
            texts = [content] + [c[1] for c in candidates]
            if hasattr(self._embedding_engine, "embed_batch"):
                vecs = self._embedding_engine.embed_batch(texts)
            else:
                vecs = [self._embedding_engine.embed(t) for t in texts]

            new_vec = np.asarray(vecs[0], dtype=np.float32)
            new_norm = np.linalg.norm(new_vec)
            if new_norm > 1e-8:
                new_vec = new_vec / new_norm

            matrix = np.vstack(
                [np.asarray(v, dtype=np.float32) for v in vecs[1:]]
            )
            norms = np.linalg.norm(matrix, axis=1)
            norms[norms < 1e-8] = 1.0
            sims = (matrix @ new_vec) / norms

            for (mem, _), similarity in zip(candidates, sims):
                sim = float(similarity)
                if sim > 0.85:
                    self._adapter.create_memory_link(
                        memory_id, mem["memory_id"], link_type="semantic",
                        strength=round(sim, 3),
                    )
                    linked.append(mem["memory_id"])
        except Exception as _e:
            swallow(__name__, _e)
        return linked
    def _auto_extract_entities(
        self, memory_id: str, content: str,
    ) -> List[str]:
        """为新写入的记忆自动提取实体并创建 mentions 关系。

        LLM 驱动（2026-08-15, R2 优化）：TRINITY_LLM_EXTRACT=on 时改用
        EntityRelationExtractor（LLM 提取实体+关系谓词 → 写入 relations 表，
        对齐 Mem0/Zep 的写入即抽取）；未开启/失败时回退规则提取（原行为）。

        Args:
            memory_id: 新记忆 ID。
            content: 记忆内容。

        Returns:
            创建的实体 ID 列表。
        """
        entity_ids: List[str] = []
        if not self._adapter or not hasattr(self._adapter, "upsert_entity"):
            return entity_ids

        # ── LLM 驱动分支（env 开关，默认关）──────────────────────────
        if os.environ.get("TRINITY_LLM_EXTRACT", "").strip().lower() in ("1", "on", "true", "yes"):
            try:
                from trinity.daemon.memory_compressor import create_llm_compress_callable
                from trinity.memory.er_extractor import EntityRelationExtractor
                llm = create_llm_compress_callable()
                extractor = EntityRelationExtractor(self._adapter, llm_call=llm)
                summary = extractor.extract_from_memories([memory_id])
                for ent in summary.get("entities", []):
                    eid = ent.get("id", "")
                    if eid:
                        entity_ids.append(eid)
                return entity_ids
            except Exception as _e:
                # LLM 不可用/失败 → 静默回退规则提取
                swallow(__name__, _e)

        try:
            from trinity.core.entity_extractor import EntityExtractor
            extractor = EntityExtractor()
            entities = extractor.extract(content)
            for ent in entities:
                name = ent.get("name", "")
                etype = ent.get("type", "concept")
                if not name:
                    continue
                result = self._adapter.upsert_entity(name, etype, {})
                eid = result.get("id", "")
                if eid:
                    entity_ids.append(eid)
                    # 创建 mentions 关系
                    if hasattr(self._adapter, "create_relation"):
                        self._adapter.create_relation(
                            eid, "mentions", memory_id,
                            {"direction": "entity_to_memory"},
                        )
        except Exception as _e:
            swallow(__name__, _e)
        return entity_ids
