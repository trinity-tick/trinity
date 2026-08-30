"""Trinity client - ingestion & write pipeline mixin (split from client.py, 2026-08-17).

Part of the Trinity client package decomposition. Behavior identical to
the pre-split single-file implementation.
"""

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced
class _IngestionMixin:
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
        tags = tags or []
        # 2026-09（EXECUTION 105.9）写入时实时价值编码：importance 为默认值
        # 0.5 时用 quick_value 规则启发式填充（毫秒级、零 LLM 依赖、失败静默）
        # ——写入瞬间即有价值信号；深度五因素 LLM 评估由每日 value-recalib 补标。
        if importance == 0.5:
            try:
                from trinity.brain.value_encoder import quick_value
                importance = quick_value(str(content or ""), str(category or ""))
            except Exception:
                pass
        # 2026-09 (EXECUTION 132): 情感层——写入即情感标记（杏仁核通路扩展，零 LLM）
        try:
            from trinity.brain.affect import assess as _affect_assess
            _aff = _affect_assess(str(content or ""))
            if _aff.get("polarity") != "neu":
                metadata = dict(metadata or {})
                metadata["affect"] = {"valence": _aff["valence"],
                                       "arousal": _aff["arousal"],
                                       "polarity": _aff["polarity"]}
        except Exception:
            pass

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
        except Exception:
            injection_report = None  # 扫描失败不阻断写入

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
        if memory_id and not isolated_test_write:
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
                        except Exception:
                            pass
                    _th.Thread(target=_graph_ingest, args=(_gcontent,),
                               daemon=True, name="sage-ingest").start()
            except Exception:
                pass

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
                        import psycopg2
                        conn = psycopg2.connect(
                            host="127.0.0.1", port=5432, dbname="trinity",
                            user="trinity", password="trinity")
                        conn.autocommit = True
                        cur = conn.cursor()
                        import json as _json
                        meta = _json.dumps(
                            {"value_model": ev["version"],
                             "value_factors": ev["factors"],
                             "value_reason": ev["reason"]},
                            ensure_ascii=False)
                        cur.execute("""
                            UPDATE memories
                            SET importance = %s,
                                importance_score = %s,
                                metadata = CASE
                                    WHEN jsonb_typeof(metadata) = 'object' THEN metadata || %s::jsonb
                                    ELSE '{}'::jsonb || %s::jsonb
                                END,
                                updated_at = NOW()
                            WHERE memory_id = %s
                        """, (ev["value"], ev["value"], meta, meta, _mid))
                        conn.close()
                    except Exception:
                        pass

                _th.Thread(target=_deep_value, daemon=True,
                           name="ingest-deep-value").start()
            except Exception:
                pass

        # 2026-09（EXECUTION 105.19）写入缓存失效：语义缓存无写入失效——
        # 写入后同 query 300s 内返回旧结果（一致性缺陷，实测 gap_fill 场景
        # 被缓存遮蔽）；写入后清空语义缓存（写入低频，命中损失可接受；
        # TRINITY_CACHE_BACKEND=off 时 invalidate 为 no-op）。
        try:
            from trinity.core.cache import get_cache
            get_cache().invalidate(pattern="*")
        except Exception:
            pass

        # 隔离写入:立即归档(不进入 active 检索面),并留审计痕迹
        if isolated_test_write and memory_id and self._adapter:
            try:
                self._adapter.archive_memories([memory_id])
                action = "ISOLATED_TEST_WRITE"
                details: Dict[str, Any] = {"category": category, "tags": tags}
                if injection_report is not None and injection_report.get("flagged"):
                    # 2026-08-24（R8 P1-6）：投毒注入隔离单独记审计
                    action = "INJECTION_ISOLATED"
                    details = {
                        "severity": injection_report.get("severity"),
                        "patterns": [h["pattern"] for h in injection_report.get("hits", [])],
                    }
                if hasattr(self._adapter, "write_audit_log"):
                    self._adapter.write_audit_log(
                        memory_id=memory_id, action=action,
                        agent_id=agent_id, persona_id=persona_id,
                        details=details,
                    )
            except Exception:
                pass

        # 自动审计日志（同步：核心写入 + 审计链即时落账，保证可信链完整）
        if self._adapter and hasattr(self._adapter, "write_audit_log"):
            try:
                self._adapter.write_audit_log(
                    memory_id=memory_id, action="create", agent_id=agent_id,
                    persona_id=persona_id,
                    details={"importance": importance, "tags": tags,
                             "category": category, "modality": modality},
                )
            except Exception:
                pass

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
            except Exception:
                pass

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

        return result
    _ISOLATED_TEST_AGENTS = {"stress-agent", "lock-test", "stress-test", "stress-db-writer"}
    _ISOLATED_TEST_CATEGORIES = {"stress-test", "stress_test"}
    _ISOLATED_TEST_TAGS = {"locktest", "stress"}
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
        if category in self._ISOLATED_TEST_CATEGORIES:
            return True
        if any((tg or "").lower() in self._ISOLATED_TEST_TAGS for tg in (tags or [])):
            return True
        if content.startswith("[自动关联]") or "LONG-STRESS" in content:
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
            except Exception:
                pass
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
                    if "postgres" in _tname:
                        _eng = None
                        try:
                            from trinity.core.client._helpers import _get_embedding_engine
                            _eng = _get_embedding_engine()
                        except Exception:
                            _eng = None
                        if _eng is not None:
                            _v = _eng.embed(str(content)[:500])
                            _vec = [float(x) for x in _v]
                            try:
                                _adp.set_embedding(memory_id, _vec)
                            except Exception:
                                pass
                        # 中文分词回填
                        try:
                            import jieba as _jb
                            _jb.setLogLevel(60)
                            _words = [w.strip() for w in _jb.cut(str(content))
                                      if w.strip() and len(w.strip()) >= 2][:12]
                            if _words:
                                import psycopg2 as _pg
                                _conn = _pg.connect(host="127.0.0.1", port=5432,
                                                    dbname="trinity", user="trinity", password="trinity")
                                _conn.autocommit = True
                                _cur = _conn.cursor()
                                _cur.execute(
                                    "UPDATE memories SET content_tsv_zh = to_tsvector('simple', %s) WHERE memory_id = %s",
                                    (" | ".join(_words), memory_id))
                                _conn.close()
                        except Exception:
                            pass
            except Exception:
                pass

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
        except Exception:
            pass
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
            except Exception:
                # LLM 不可用/失败 → 静默回退规则提取
                pass

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
        except Exception:
            pass
        return entity_ids
