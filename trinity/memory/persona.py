"""白盒 Persona 画像层 — 从引擎库聚合命题记忆生成 persona.md（2026-08-22）

在 proposition_v2 写入 `category='proposition'`、`metadata.proposition_type` 的
基础上，按 persona 聚合用户偏好/事实命题，白盒渲染成 Markdown 画像文件：

  - 读取:  引擎库 SQLite 中 `category='proposition'` 且
           `proposition_type='user_preference'`（可选包含 `user_fact`）的记忆
  - 输出:  `~/.trinity/personas/{persona_id}.md`（YAML 头 + 偏好条目列表）
  - 功能:  rebuild 全量重建 / read_persona 读取 / list_personas 枚举
  - 增删:  增量合并按命题原文去重（同一条命题只保留最新来源）
  - 开关:  TRINITY_PERSONA（默认 off）——只在 on 时写路径钩子才触发增量，
           默认不改变现有行为/基线（遵循"不改变基线"原则）；开关逻辑
           做成可测纯函数 persona_enabled()。

本模块不写运行时大库（只读引擎库），产物只落盘到 personas 目录。
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from trinity.adapters.sqlite import SQLiteAdapter

logger = logging.getLogger("trinity.memory.persona")

# 参与画像的命题类型（默认仅用户偏好；可经 include_user_fact 扩展）
PERSONA_PROPOSITION_TYPES = ("user_preference", "user_fact")

# content 前缀（与 proposition_extractor 的 extract_and_store 落盘格式一致）
_PROPOSITION_CONTENT_PREFIX = re.compile(r"^\[命题:[a-z_]+\]\s*")

# 默认 persona 产物目录（可经 personas_dir 覆盖，测试用临时目录）
DEFAULT_PERSONAS_DIR = os.path.join(
    str(Path.home() / ".trinity"), "personas"
)


def persona_enabled() -> bool:
    """TRINITY_PERSONA 开关（默认 off），做成可测纯函数。

    只在 on / 1 / true / yes 时为 True；否则 False。写路径增量钩子
    依赖此开关，默认 off 时行为不发生任何变化（不改变基线）。
    """
    return os.environ.get("TRINITY_PERSONA", "off").lower() in ("on", "1", "true", "yes")


class PersonaEngine:
    """从引擎库白盒聚合命题记忆，生成 / 读取 / 枚举 persona 画像。

    adapter 优先；未提供时按 db_path（或引擎权威大库路径）自建 SQLite 适配器。
    personas_dir 用于指定画像落盘目录（默认 ~/.trinity/personas）。
    """

    def __init__(
        self,
        adapter: Optional[SQLiteAdapter] = None,
        db_path: Optional[str] = None,
        personas_dir: Optional[str] = None,
        include_user_fact: bool = False,
    ):
        _owns_adapter = adapter is None
        if adapter is None:
            # 未给 adapter 时，用给定 db_path，或回退到引擎权威大库
            _p = db_path or self._default_db_path()
            adapter = SQLiteAdapter(db_path=_p)
            adapter.connect()
        elif db_path is not None:
            logger.warning("persona: adapter given, ignore db_path=%s", db_path)
        self._adapter = adapter
        self._owns_adapter = _owns_adapter
        self.personas_dir = personas_dir or DEFAULT_PERSONAS_DIR
        # 是否把 user_fact 命题也纳入画像（默认只含 user_preference）
        self.include_user_fact = include_user_fact

    # ── 路径解析 ────────────────────────────────────────────────
    @staticmethod
    def _default_db_path() -> str:
        """回退到引擎权威大库路径（~/.trinity/store/trinity_store.db）。"""
        from trinity.core.client._helpers import _find_trinity_store

        store_dir = _find_trinity_store()
        return os.path.join(store_dir, "trinity_store.db")

    def persona_path(self, persona_id: str) -> str:
        return os.path.join(self.personas_dir, f"{persona_id}.md")

    # ── 读取引擎库命题 ─────────────────────────────────────────
    def _accepted_types(self) -> Tuple[str, ...]:
        base = ("user_preference",)
        if self.include_user_fact:
            base = base + ("user_fact",)
        return base

    def _fetch_proposition_rows(self, persona_id: str) -> List[Dict[str, Any]]:
        """查询某人下所有命题记忆（category='proposition'，active）。"""
        conn = self._adapter._get_read_conn() or self._adapter._conn
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT memory_id, persona_id, content, importance, category, "
            "metadata, created_at, role FROM memories "
            "WHERE persona_id=? AND category='proposition' AND status='active'",
            (persona_id,),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            content = d.get("content") or ""
            if content:
                d["content"] = self._adapter._decrypt_content(content)
            d["metadata"] = self._parse_json(d.get("metadata"))
            out.append(d)
        return out

    @staticmethod
    def _parse_json(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        import json

        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", "ignore")
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _extract_proposition_text(content: str) -> str:
        """去掉 `[命题:<type>]` 前缀，还原命题原文。"""
        if not content:
            return ""
        return _PROPOSITION_CONTENT_PREFIX.sub("", content, count=1).strip()

    @staticmethod
    def _entry_time(meta: Dict[str, Any], created_at: Any) -> str:
        t = meta.get("temporal") or created_at
        return str(t) if t else ""

    # ── 聚合 → 条目 ────────────────────────────────────────────
    def collect_entries(self, persona_id: str) -> List[Dict[str, Any]]:
        """按 persona 聚合命题，返回去重后的条目列表。

        每条: {memory_id, proposition, time, importance, proposition_type}。
        同一原文只保留一条（最新创建的优先）。
        """
        accepted = self._accepted_types()
        by_text: Dict[str, Dict[str, Any]] = {}
        for d in self._fetch_proposition_rows(persona_id):
            meta = d.get("metadata") or {}
            ptype = str(meta.get("proposition_type") or "")
            if ptype not in accepted:
                continue
            text = self._extract_proposition_text(d.get("content") or "")
            if not text:
                continue
            existing = by_text.get(text)
            created = d.get("created_at") or ""
            if existing is None or created > (existing.get("created_at") or ""):
                by_text[text] = {
                    "memory_id": d.get("memory_id"),
                    "proposition": text,
                    "time": self._entry_time(meta, created),
                    "importance": float(d.get("importance") or 0.0),
                    "proposition_type": ptype,
                    "created_at": created,
                }
        # 按时间倒序、同时间按 importance 降序，稳定呈现
        return sorted(
            by_text.values(),
            key=lambda e: (e.get("time") or "", -e.get("importance", 0.0)),
            reverse=True,
        )

    # ── 渲染 Markdown ──────────────────────────────────────────
    @staticmethod
    def render_markdown(persona_id: str, entries: List[Dict[str, Any]]) -> str:
        """白盒渲染 persona.md：YAML 头 + 偏好条目列表。"""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines: List[str] = [
            "---",
            f"persona_id: {persona_id}",
            f"generated_at: {now}",
            f"entry_count: {len(entries)}",
            f"source: trinity.persona",
            "---",
            "",
            f"# Persona 画像：{persona_id}",
            "",
            f"共 {len(entries)} 条命题记忆。",
            "",
        ]
        if entries:
            lines.append("## 偏好条目")
            lines.append("")
            for i, e in enumerate(entries, 1):
                lines.append(f"### {i}. {e['proposition']}")
                lines.append("")
                lines.append(f"- memory_id: `{e['memory_id']}`")
                lines.append(f"- 时间: {e.get('time') or '未知'}")
                lines.append(f"- importance: {e.get('importance', 0.0):.2f}")
                lines.append(f"- 类型: {e.get('proposition_type')}")
                lines.append("")
        return "\n".join(lines)

    # ── 全量重建 ───────────────────────────────────────────────
    def rebuild(self, persona_id: str) -> int:
        """全量重建 persona 画像文件，返回条目数。"""
        entries = self.collect_entries(persona_id)
        os.makedirs(self.personas_dir, exist_ok=True)
        Path(self.persona_path(persona_id)).write_text(
            self.render_markdown(persona_id, entries), encoding="utf-8"
        )
        logger.info("persona: rebuilt %s (%d entries)", persona_id, len(entries))
        return len(entries)

    # ── 读取 / 枚举 ────────────────────────────────────────────
    def read_persona(self, persona_id: str) -> Tuple[str, int]:
        """读取 persona 画像文本与条目数；不存在时返回空文本与 0。"""
        path = self.persona_path(persona_id)
        if not os.path.exists(path):
            return "", 0
        text = Path(path).read_text(encoding="utf-8")
        # 条目数取 YAML 头 entry_count，读不到则回退计算
        count = 0
        m = re.search(r"^entry_count:\s*(\d+)\s*$", text, re.MULTILINE)
        if m:
            count = int(m.group(1))
        else:
            count = sum(1 for line in text.splitlines() if line.startswith("### "))
        return text, count

    def list_personas(self) -> List[str]:
        """枚举已有画像的 persona_id（按画像文件存在性）。"""
        if not os.path.isdir(self.personas_dir):
            return []
        ids = [
            p[:-3]
            for p in os.listdir(self.personas_dir)
            if p.endswith(".md")
        ]
        return sorted(ids)

    # ── 增量合并 ───────────────────────────────────────────────
    def merge_persona(self, persona_id: str, entry: Dict[str, Any]) -> int:
        """把一条新命题增量合并进 persona 画像（按原文去重）。

        已存在同一原文时不重复追加；否则先全量重建（保证与库状态一致）。
        返回当前条目数。
        """
        text = entry.get("proposition") or ""
        if not text:
            return self.rebuild(persona_id)
        existing = self.read_persona(persona_id)[0]
        if existing and text in existing:
            return self._count_entries(existing)
        return self.rebuild(persona_id)

    @staticmethod
    def _count_entries(markdown_text: str) -> int:
        m = re.search(r"^entry_count:\s*(\d+)\s*$", markdown_text, re.MULTILINE)
        if m:
            return int(m.group(1))
        return sum(1 for line in markdown_text.splitlines() if line.startswith("### "))

    def close(self) -> None:
        """断开自建的 adapter（避免后台线程残留）。"""
        if self._owns_adapter:
            try:
                self._adapter.disconnect()
            except Exception:  # noqa: BLE001
                pass


def maybe_persona_after_store(
    adapter: Any,
    store_kwargs: Dict[str, Any],
    result: Dict[str, Any],
    personas_dir: Optional[str] = None,
) -> int:
    """写路径增量钩子（由主代理在 ingest 写路径按需调用）。

    仅当 TRINITY_PERSONA=on 时触发：新写入的命题记忆（category='proposition' 且
    proposition_type='user_preference'/'user_fact'）增量合并进对应 persona 画像。
    默认 off 时什么都不做（返回 0，行为不变）。子代理不接线——本函数提供可测
    接口，由主代理决定是否/何时挂载。
    """
    if not persona_enabled():
        return 0
    mid = result.get("memory_id") if isinstance(result, dict) else None
    persona_id = store_kwargs.get("persona_id", "default")
    meta = store_kwargs.get("metadata") or {}
    ptype = meta.get("proposition_type")
    if adapter is None or ptype not in PERSONA_PROPOSITION_TYPES:
        # 非偏好/事实命题或无 adapter，不参与画像
        if mid:
            logger.debug("persona: skip store %s (type=%s)", mid, ptype)
        return 0
    try:
        engine = PersonaEngine(adapter=adapter, personas_dir=personas_dir)
        # 增量合并：以新命题按原文去重
        text = store_kwargs.get("content") or ""
        text = PersonaEngine._extract_proposition_text(text)
        count = engine.merge_persona(
            persona_id,
            {
                "memory_id": mid,
                "proposition": text,
                "time": meta.get("temporal") or store_kwargs.get("created_at") or "",
                "importance": float(store_kwargs.get("importance", 0.0) or 0.0),
                "proposition_type": ptype,
            },
        )
        return count
    except Exception as e:  # noqa: BLE001
        logger.warning("persona incremental hook failed (non-fatal): %s", e)
        return 0
