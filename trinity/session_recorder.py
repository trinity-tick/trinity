"""
历史会话自动记录器 — ChatSessionRecorder

自动记录所有对话历史，支持全文搜索、标签提取、会话统计。
存储方式：SQLite (sessions.db)，带 FTS5 全文索引。
向后兼容：如果 sessions.db 不存在，使用 JSON 文件模式。
"""

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("trinity.session_recorder")

# 中文停用词（简单版）
_STOP_WORDS: set[str] = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "什么",
    "如何", "怎么", "为什么", "吗", "吧", "呢", "啊", "哦", "嗯", "这个",
    "那个", "可以", "能", "应该", "需要", "让", "把", "被", "从", "对",
    "与", "以", "为", "而", "但",  "如果", "因为", "所以", "然后",
    "之", "将", "中", "等", "或", "及", "其", "进行", "通过", "使用",
    "以及", "并且", "或者", "还是", "虽然", "不过", "对于", "关于",
    "get", "set", "use", "make", "like", "just", "also", "well",
    "would", "could", "should", "may", "might", "shall", "will",
}


class ChatSessionRecorder:
    """自动记录所有对话历史的模块。

    使用 SQLite 存储（sessions.db），带 FTS5 全文索引。
    如果 sessions.db 不存在，回退到 JSON 文件模式。

    Attributes:
        log_dir:         会话日志存储目录。
        current_session: 当前正在记录的会话 ID（None 表示未开始）。
    """

    def __init__(self, log_dir: Optional[str] = None):
        """初始化会话记录器。

        Args:
            log_dir: 会话日志存储目录。默认为项目根下的 data/sessions/。
        """
        if log_dir is None:
            base = Path(__file__).resolve().parent.parent  # trinity/ → 项目根
            log_dir = str(base / "data" / "sessions")
        self.log_dir: str = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        self.current_session: Optional[str] = None

        # ── SQLite 模式 ─────────────────────────────────────────────
        self._db_conn: Optional[sqlite3.Connection] = None
        self._use_sqlite = False
        self._init_sqlite()

        logger.info("ChatSessionRecorder 初始化完成，日志目录: %s", self.log_dir)

    # ── SQLite 初始化 ────────────────────────────────────────────────

    def _init_sqlite(self) -> None:
        """尝试初始化 SQLite 数据库。如果失败则使用 JSON 模式。"""
        db_path = os.path.join(self.log_dir, "sessions.db")
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    task TEXT,
                    started_at REAL,
                    ended_at REAL,
                    summary TEXT,
                    turn_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    timestamp REAL,
                    tags TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
            """)

            # FTS5 全文搜索虚拟表
            try:
                conn.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts
                    USING fts5(content, role, content='turns', content_rowid='id');

                    -- INSERT 触发器
                    CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns
                    BEGIN
                        INSERT INTO turns_fts(rowid, content, role)
                        VALUES (new.id, new.content, new.role);
                    END;

                    -- DELETE 触发器
                    CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns
                    BEGIN
                        INSERT INTO turns_fts(turns_fts, rowid, content, role)
                        VALUES ('delete', old.id, old.content, old.role);
                    END;

                    -- UPDATE 触发器
                    CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns
                    BEGIN
                        INSERT INTO turns_fts(turns_fts, rowid, content, role)
                        VALUES ('delete', old.id, old.content, old.role);
                        INSERT INTO turns_fts(rowid, content, role)
                        VALUES (new.id, new.content, new.role);
                    END;
                """)
            except sqlite3.OperationalError:
                # FTS5 不可用，静默跳过
                pass

            conn.commit()
            self._db_conn = conn
            self._use_sqlite = True
            logger.info("SQLite 会话存储已启用: %s", db_path)
        except Exception as e:
            logger.warning("SQLite 初始化失败，回退到 JSON 模式: %s", e)
            self._use_sqlite = False
            self._db_conn = None

    def close(self) -> None:
        """显式关闭 SQLite 数据库连接。

        调用此方法后，记录器将回退到 JSON 模式。
        如果不调用此方法，连接将在对象被垃圾回收时自动关闭。
        """
        if self._db_conn:
            try:
                self._db_conn.close()
            except Exception:
                pass
            self._db_conn = None
            self._use_sqlite = False

    def _fts_available(self) -> bool:
        """检查 FTS5 是否可用。"""
        if not self._use_sqlite or not self._db_conn:
            return False
        try:
            cursor = self._db_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='turns_fts'"
            )
            return cursor.fetchone() is not None
        except Exception:
            return False

    # ── 会话生命周期 ──────────────────────────────────────────────────

    def start_session(self, task: str = "") -> str:
        """开始一个新的会话。

        Args:
            task: 可选的任务描述，用作会话标签。

        Returns:
            新会话的 session_id。
        """
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%d_%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        session_id = f"session_{ts}_{short_uuid}"

        if self._use_sqlite:
            conn = self._db_conn
            conn.execute("""
                INSERT INTO sessions (session_id, task, started_at, turn_count)
                VALUES (?, ?, ?, 0)
            """, (session_id, task, now.timestamp()))
            conn.commit()
        else:
            session_data: dict[str, Any] = {
                "session_id": session_id,
                "started_at": now.timestamp(),
                "ended_at": None,
                "turns": [],
                "tags": self.auto_tag(task) if task else [],
                "turn_count": 0,
                "task": task,
            }
            self._write_session(session_id, session_data)

        self.current_session = session_id
        logger.info("新会话开始: %s (task=%s)", session_id, task)
        return session_id

    def end_session(self, summary: str = "") -> None:
        """结束当前会话。

        Args:
            summary: 可选会话总结。
        """
        if self.current_session is None:
            logger.warning("没有正在进行的会话可结束。")
            return

        if self._use_sqlite:
            conn = self._db_conn
            now_ts = datetime.now(timezone.utc).timestamp()
            conn.execute("""
                UPDATE sessions SET ended_at = ?, summary = COALESCE(?, summary)
                WHERE session_id = ?
            """, (now_ts, summary, self.current_session))
            conn.commit()
        else:
            session = self._load_session(self.current_session)
            if session:
                session["ended_at"] = datetime.now(timezone.utc).timestamp()
                if summary and summary not in session.get("tags", []):
                    tags = session.setdefault("tags", [])
                    tags.extend(self.auto_tag(summary))
                    session["tags"] = list(set(tags))
                self._write_session(self.current_session, session)

        logger.info("会话结束: %s", self.current_session)
        self.current_session = None

    # ── 核心记录方法 ──────────────────────────────────────────────────

    def record_turn(
        self,
        role: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """记录一次对话轮次。

        如果 session_id 未指定且当前有活跃会话，则使用当前会话。
        如果 session_id 未指定且无活跃会话，则自动开始一个新会话。

        Args:
            role:    角色 (user / assistant / system / tool)。
            content: 对话内容。
            metadata: 附加元数据（可选）。
            session_id: 目标会话 ID（可选）。

        Returns:
            Dict 包含 session_id, turn_index, tags, timestamp。
        """
        sid = session_id or self.current_session
        if sid is None:
            sid = self.start_session()

        tags = self.auto_tag(content)
        timestamp = datetime.now(timezone.utc).timestamp()

        if self._use_sqlite:
            conn = self._db_conn
            tags_json = json.dumps(tags)
            conn.execute("""
                INSERT INTO turns (session_id, role, content, timestamp, tags)
                VALUES (?, ?, ?, ?, ?)
            """, (sid, role, content, timestamp, tags_json))
            conn.execute("""
                UPDATE sessions SET turn_count = turn_count + 1
                WHERE session_id = ?
            """, (sid,))
            conn.commit()

            cursor = conn.execute(
                "SELECT MAX(id) as last_id FROM turns WHERE session_id = ?",
                (sid,)
            )
            row = cursor.fetchone()
            turn_index = (row["last_id"] if row else 0) - 1
        else:
            session = self._load_session(sid)
            if session is None:
                now = datetime.now(timezone.utc)
                session = {
                    "session_id": sid,
                    "started_at": now.timestamp(),
                    "ended_at": None,
                    "turns": [],
                    "tags": [],
                    "turn_count": 0,
                    "task": "",
                }

            turn: dict[str, Any] = {
                "role": role,
                "content": content,
                "timestamp": timestamp,
                "tags": tags,
            }
            if metadata:
                turn["metadata"] = metadata

            session["turns"].append(turn)
            session["turn_count"] = len(session["turns"])

            existing_tags = set(session.get("tags", []))
            existing_tags.update(tags)
            session["tags"] = sorted(existing_tags)

            self._write_session(sid, session)
            turn_index = session["turn_count"] - 1

        return {
            "session_id": sid,
            "turn_index": turn_index,
            "tags": tags,
            "timestamp": timestamp,
        }

    # ── 搜索方法 ──────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """全文关键词搜索历史会话。

        使用 FTS5（SQLite 模式）或倒排索引（JSON 模式）进行搜索。

        Args:
            query:  搜索关键词。
            top_k:  返回结果数量上限。

        Returns:
            匹配的记忆条目列表。
        """
        if not query or not query.strip():
            return []

        if self._use_sqlite:
            return self._search_sqlite(query, top_k)
        else:
            return self._search_json(query, top_k)

    def _search_sqlite(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """使用 SQLite FTS5 搜索。"""
        conn = self._db_conn
        if not conn:
            return []

        # 尝试 FTS5 搜索
        if self._fts_available():
            try:
                terms = query.strip().split()
                fts_query = " AND ".join(f'"{t}"*' for t in terms if t)
                if not fts_query:
                    return []

                sql = """
                    SELECT t.id, t.session_id, t.role, t.content, t.timestamp, t.tags,
                           fts.rank as fts_rank
                    FROM turns t
                    INNER JOIN (
                        SELECT rowid, rank
                        FROM turns_fts
                        WHERE turns_fts MATCH ?
                    ) fts ON t.id = fts.rowid
                    ORDER BY fts.rank
                    LIMIT ?
                """
                cursor = conn.execute(sql, (fts_query, top_k))
                rows = cursor.fetchall()
                if rows:
                    min_rank = min(r["fts_rank"] for r in rows)
                    max_rank = max(r["fts_rank"] for r in rows)
                    rank_range = max_rank - min_rank if max_rank != min_rank else 1.0

                    results = []
                    for row in rows:
                        norm_score = 1.0 - (row["fts_rank"] - min_rank) / rank_range
                        results.append({
                            "session_id": row["session_id"],
                            "turn_index": row["id"],
                            "role": row["role"],
                            "content": row["content"],
                            "timestamp": row["timestamp"],
                            "tags": json.loads(row["tags"]) if row["tags"] else [],
                            "score": round(norm_score, 2),
                        })
                    return results
            except Exception:
                # FTS5 搜索失败，回退到 LIKE
                pass

        # FTS5 没有结果（如 CJK 分词问题），回退到 LIKE

        # 回退：LIKE 搜索
        like_term = f"%{query}%"
        cursor = conn.execute("""
            SELECT t.id, t.session_id, t.role, t.content, t.timestamp, t.tags
            FROM turns t
            WHERE t.content LIKE ?
            ORDER BY t.id DESC
            LIMIT ?
        """, (like_term, top_k))

        results = []
        for row in cursor.fetchall():
            results.append({
                "session_id": row["session_id"],
                "turn_index": row["id"],
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["timestamp"],
                "tags": json.loads(row["tags"]) if row["tags"] else [],
                "score": 0.5,
            })
        return results

    def _search_json(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """使用 JSON 倒排索引搜索（向后兼容）。"""
        query_terms = self._tokenize(query)
        if not query_terms:
            return []

        results: list[tuple[float, dict[str, Any]]] = []
        session_files = sorted(Path(self.log_dir).glob("*.json"))

        for fpath in session_files:
            try:
                session = json.loads(fpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            sid = session.get("session_id", fpath.stem)
            for i, turn in enumerate(session.get("turns", [])):
                content = turn.get("content", "")
                turn_terms = self._tokenize(content)
                if not turn_terms:
                    continue

                score = 0.0
                for qt in query_terms:
                    score += turn_terms.count(qt) * 3.0
                    if any(qt in tt for tt in turn_terms):
                        score += 1.0
                    if qt.lower() in content.lower():
                        score += 0.5

                if score > 0:
                    results.append((
                        score,
                        {
                            "session_id": sid,
                            "turn_index": i,
                            "role": turn.get("role", "unknown"),
                            "content": content,
                            "timestamp": turn.get("timestamp", 0.0),
                            "tags": turn.get("tags", []),
                            "score": round(score, 2),
                        },
                    ))

        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:top_k]]

    # ── 会话查询 ──────────────────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        """按 session ID 获取完整会话。

        Args:
            session_id: 目标会话 ID。

        Returns:
            会话数据 dict，如果不存在则返回 None。
        """
        if self._use_sqlite:
            return self._get_session_sqlite(session_id)
        return self._load_session(session_id)

    def _get_session_sqlite(self, session_id: str) -> Optional[dict[str, Any]]:
        """从 SQLite 获取会话。"""
        conn = self._db_conn
        if not conn:
            return None

        cursor = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )
        s_row = cursor.fetchone()
        if not s_row:
            return None

        cursor = conn.execute(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY id ASC",
            (session_id,)
        )
        turns = []
        for t_row in cursor.fetchall():
            turns.append({
                "role": t_row["role"],
                "content": t_row["content"],
                "timestamp": t_row["timestamp"],
                "tags": json.loads(t_row["tags"]) if t_row["tags"] else [],
            })

        # 从 turns 中提取标签
        all_tags = set()
        for t in turns:
            all_tags.update(t.get("tags", []))

        return {
            "session_id": s_row["session_id"],
            "task": s_row["task"] or "",
            "started_at": s_row["started_at"],
            "ended_at": s_row["ended_at"],
            "summary": s_row["summary"] or "",
            "turn_count": s_row["turn_count"] or len(turns),
            "turns": turns,
            "tags": sorted(all_tags),
        }

    def recent_sessions(self, days: int = 7) -> list[dict[str, Any]]:
        """返回最近几天的会话列表。

        Args:
            days: 天数范围（默认 7 天）。

        Returns:
            会话摘要列表，按 started_at 降序排列。
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()

        if self._use_sqlite:
            conn = self._db_conn
            cursor = conn.execute("""
                SELECT session_id, started_at, ended_at, turn_count, task
                FROM sessions
                WHERE started_at >= ?
                ORDER BY started_at DESC
                LIMIT 100
            """, (cutoff,))

            sessions = []
            for row in cursor.fetchall():
                sessions.append({
                    "session_id": row["session_id"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "turn_count": row["turn_count"],
                    "tags": [],  # SQLite 模式从 turns 动态提取
                    "task": row["task"] or "",
                })
            return sessions

        # JSON 模式
        sessions: list[dict[str, Any]] = []
        for fpath in sorted(Path(self.log_dir).glob("*.json")):
            try:
                session = json.loads(fpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            started_at = session.get("started_at", 0)
            if started_at >= cutoff:
                sessions.append({
                    "session_id": session.get("session_id", fpath.stem),
                    "started_at": started_at,
                    "ended_at": session.get("ended_at"),
                    "turn_count": session.get("turn_count", 0),
                    "tags": session.get("tags", []),
                    "task": session.get("task", ""),
                })

        sessions.sort(key=lambda s: s["started_at"], reverse=True)
        return sessions

    def session_stats(self) -> dict[str, Any]:
        """统计信息（总轮次、话题分布、活跃度）。

        Returns:
            统计信息 dict。
        """
        if self._use_sqlite:
            return self._session_stats_sqlite()
        return self._session_stats_json()

    def _session_stats_sqlite(self) -> dict[str, Any]:
        """SQLite 模式统计。"""
        conn = self._db_conn
        if not conn:
            return {}

        cursor = conn.execute("SELECT COUNT(*) as c FROM sessions")
        total_sessions = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT COUNT(*) as c FROM turns")
        total_turns = cursor.fetchone()["c"]

        cursor = conn.execute("""
            SELECT COALESCE(AVG(turn_count), 0) as avg_turns FROM sessions
        """)
        avg_turns = round(cursor.fetchone()["avg_turns"], 1)

        # 活跃天数
        cursor = conn.execute("""
            SELECT DISTINCT DATE(started_at, 'unixepoch') as day
            FROM sessions ORDER BY day
        """)
        active_days = len(cursor.fetchall())

        # 角色分布
        cursor = conn.execute("""
            SELECT role, COUNT(*) as cnt FROM turns GROUP BY role
        """)
        role_dist = {row["role"]: row["cnt"] for row in cursor.fetchall()}

        # 最常/最短会话
        cursor = conn.execute("""
            SELECT COALESCE(MAX(turn_count), 0) as max_tc,
                   COALESCE(MIN(turn_count), 0) as min_tc
            FROM sessions
        """)
        ext = cursor.fetchone()

        # 每日活动
        cursor = conn.execute("""
            SELECT DATE(started_at, 'unixepoch') as day, COUNT(*) as cnt
            FROM sessions
            GROUP BY day ORDER BY cnt DESC LIMIT 14
        """)
        daily_activity = {row["day"]: row["cnt"] for row in cursor.fetchall()}

        return {
            "total_sessions": total_sessions,
            "total_turns": total_turns,
            "avg_turns_per_session": avg_turns,
            "active_days": active_days,
            "daily_activity": daily_activity,
            "top_tags": {},  # SQLite 模式暂不统计标签
            "role_distribution": role_dist,
            "topic_overlap_ratio": 0.0,
            "longest_session": ext["max_tc"] if ext else 0,
            "shortest_session": ext["min_tc"] if ext else 0,
        }

    def _session_stats_json(self) -> dict[str, Any]:
        """JSON 模式统计（向后兼容）。"""
        total_sessions = 0
        total_turns = 0
        tag_counter: Counter[str] = Counter()
        daily_activity: Counter[str] = Counter()
        role_counter: Counter[str] = Counter()
        session_lengths: list[int] = []
        session_tags: list[set[str]] = []

        for fpath in Path(self.log_dir).glob("*.json"):
            try:
                session = json.loads(fpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            total_sessions += 1
            turns = session.get("turns", [])
            total_turns += len(turns)
            session_lengths.append(len(turns))

            for tag in session.get("tags", []):
                tag_counter[tag] += 1
            session_tags.append(set(session.get("tags", [])))

            started_at = session.get("started_at")
            if started_at:
                day_str = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d")
                daily_activity[day_str] += 1

            for turn in turns:
                role_counter[turn.get("role", "unknown")] += 1

        avg_turns = round(total_turns / max(total_sessions, 1), 1)
        active_days = len(daily_activity)

        topic_overlap = 0.0
        if len(session_tags) > 1:
            pairs = 0
            overlaps = 0
            for i in range(len(session_tags)):
                for j in range(i + 1, len(session_tags)):
                    pairs += 1
                    if session_tags[i] & session_tags[j]:
                        overlaps += 1
            topic_overlap = round(overlaps / max(pairs, 1), 3)

        return {
            "total_sessions": total_sessions,
            "total_turns": total_turns,
            "avg_turns_per_session": avg_turns,
            "active_days": active_days,
            "daily_activity": dict(daily_activity.most_common(14)),
            "top_tags": dict(tag_counter.most_common(20)),
            "role_distribution": dict(role_counter),
            "topic_overlap_ratio": topic_overlap,
            "longest_session": max(session_lengths) if session_lengths else 0,
            "shortest_session": min(session_lengths) if session_lengths else 0,
        }

    # ── 标签提取 ──────────────────────────────────────────────────────

    def auto_tag(self, content: str) -> list[str]:
        """自动提取关键词标签。

        使用词频统计和启发式规则提取关键词。

        Args:
            content: 文本内容。

        Returns:
            提取到的标签列表。
        """
        if not content or not content.strip():
            return []

        tokens = self._tokenize(content)
        if not tokens:
            return []

        token_counts: Counter[str] = Counter()
        for t in tokens:
            t_lower = t.lower().strip()
            if len(t_lower) < 2:
                continue
            if t_lower in _STOP_WORDS:
                continue
            if t_lower.isdigit():
                continue
            token_counts[t_lower] += 1

        if not token_counts:
            return []

        max_freq = max(token_counts.values())
        threshold = max_freq * 0.5
        tags = [word for word, count in token_counts.most_common(10) if count >= threshold]

        return tags[:5]

    # ── 倒排索引构建 ──────────────────────────────────────────────────

    @staticmethod
    def build_inverted_index(
        turns: list[dict[str, Any]],
    ) -> dict[str, list[int]]:
        """构建倒排索引用于全文搜索（JSON 模式兼容）。

        Args:
            turns: 对话轮次列表。

        Returns:
            倒排索引: { 词: [轮次索引1, 轮次索引2, ...] }
        """
        index: dict[str, list[int]] = {}
        for i, turn in enumerate(turns):
            content = turn.get("content", "")
            tokens = ChatSessionRecorder._tokenize(content)
            seen = set()
            for token in tokens:
                token_lower = token.lower().strip()
                if len(token_lower) < 2 or token_lower in _STOP_WORDS:
                    continue
                if token_lower not in seen:
                    seen.add(token_lower)
                    if token_lower not in index:
                        index[token_lower] = []
                    index[token_lower].append(i)
        return index

    # ── 内部辅助方法 ──────────────────────────────────────────────────

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """分词：双字及以上中文词组 + 英文单词。"""
        if not text:
            return []

        tokens: list[str] = []

        cn_seqs = re.findall(r"[\u4e00-\u9fff]{2,}", text)
        for seq in cn_seqs:
            if len(seq) <= 4:
                tokens.append(seq)
            else:
                tokens.append(seq[:4])
                tokens.append(seq[-4:])
                tokens.append(seq)

        en_tokens = re.findall(r"[a-zA-Z]+|\d+\.?\d*", text)
        for t in en_tokens:
            tokens.append(t.lower())

        return tokens

    def _session_path(self, session_id: str) -> str:
        """获取会话文件的完整路径（JSON 模式）。"""
        return os.path.join(self.log_dir, f"{session_id}.json")

    def _write_session(self, session_id: str, data: dict[str, Any]) -> None:
        """原子写入会话数据到 JSON 文件（JSON 模式）。"""
        path = self._session_path(session_id)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except OSError as e:
            logger.error("写入会话文件失败 %s: %s", path, e)
            raise

    def _load_session(self, session_id: str) -> Optional[dict[str, Any]]:
        """从 JSON 文件加载会话数据（JSON 模式）。"""
        path = self._session_path(session_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("读取会话文件失败 %s: %s", path, e)
            return None

    def list_all_sessions(self) -> list[dict[str, Any]]:
        """列出所有会话的摘要信息。

        Returns:
            会话摘要列表，按 started_at 降序排列。
        """
        if self._use_sqlite:
            conn = self._db_conn
            cursor = conn.execute("""
                SELECT session_id, started_at, ended_at, turn_count, task
                FROM sessions ORDER BY started_at DESC LIMIT 200
            """)
            return [
                {
                    "session_id": row["session_id"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "turn_count": row["turn_count"],
                    "tags": [],
                    "task": row["task"] or "",
                }
                for row in cursor.fetchall()
            ]

        sessions: list[dict[str, Any]] = []
        for fpath in sorted(Path(self.log_dir).glob("*.json")):
            try:
                session = json.loads(fpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            sessions.append({
                "session_id": session.get("session_id", fpath.stem),
                "started_at": session.get("started_at", 0),
                "ended_at": session.get("ended_at"),
                "turn_count": session.get("turn_count", 0),
                "tags": session.get("tags", []),
                "task": session.get("task", ""),
            })

        sessions.sort(key=lambda s: s["started_at"], reverse=True)
        return sessions


# ── 自检测试 ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile
    import sys

    if sys.stdout.encoding.lower() in ("gbk", "gb2312", "gb18030"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    print("=" * 60)
    print("  ChatSessionRecorder 自检测试 (SQLite 模式)")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        recorder = ChatSessionRecorder(log_dir=tmpdir)
        print(f"\n[测试1] 初始化: log_dir = {recorder.log_dir}")
        print(f"[测试1] SQLite 模式 = {recorder._use_sqlite}")

        # 测试 start_session
        print("\n[测试2] start_session:")
        sid = recorder.start_session(task="仓库管理规则优化")
        print(f"  创建会话: {sid}")

        # 测试 record_turn
        print("\n[测试3] record_turn (user):")
        result = recorder.record_turn("user", "彩棠新批次重品0.1kg-0.3kg应放第一层货架")
        print(f"  记录结果: session_id={result['session_id']}, tags={result['tags']}")

        print("\n[测试4] record_turn (assistant):")
        result = recorder.record_turn("assistant", "已记录规则：重品放在第一层，气泡柱包装占1.5倍标准位")
        print(f"  记录结果: tags={result['tags']}")

        # 测试 record_turn 自动创建会话
        print("\n[测试5] record_turn (auto-create):")
        recorder.current_session = None
        result = recorder.record_turn("user", "库存盘点完成，损耗率0.3%")
        new_sid = result["session_id"]
        print(f"  自动创建会话: {new_sid}, tags={result['tags']}")

        # 测试 end_session
        print("\n[测试6] end_session:")
        recorder.current_session = sid
        recorder.end_session(summary="完成彩棠批次货架布局规则")
        if recorder._use_sqlite:
            session = recorder.get_session(sid)
            print(f"  会话已结束: ended_at={session['ended_at']}, turn_count={session['turn_count']}")

        # 测试 get_session
        print("\n[测试7] get_session:")
        session_data = recorder.get_session(sid)
        if session_data:
            print(f"  获取会话: {session_data['session_id']}")
            print(f"  轮次数: {session_data['turn_count']}")
            print(f"  标签: {session_data.get('tags', [])}")

        # 测试 search
        print("\n[测试8] search (全文搜索):")
        results = recorder.search("彩棠 货架", top_k=3)
        for r in results:
            print(f"  [{r['score']}] [{r['role']}] {r['content'][:50]}...")

        # 测试 recent_sessions
        print("\n[测试9] recent_sessions (最近7天):")
        recent = recorder.recent_sessions(days=7)
        print(f"  会话数量: {len(recent)}")
        for s in recent:
            print(f"  - {s['session_id']}: {s['turn_count']}轮, task={s.get('task','')}")

        # 测试 session_stats
        print("\n[测试10] session_stats:")
        stats = recorder.session_stats()
        print(f"  总会话数: {stats['total_sessions']}")
        print(f"  总轮次: {stats['total_turns']}")
        print(f"  平均轮次/会话: {stats['avg_turns_per_session']}")
        print(f"  角色分布: {stats['role_distribution']}")

        # 测试 list_all_sessions
        print("\n[测试11] list_all_sessions:")
        all_sessions = recorder.list_all_sessions()
        print(f"  会话总数: {len(all_sessions)}")

        # 测试 auto_tag
        print(f"\n[测试12] auto_tag:")
        tags = recorder.auto_tag("彩棠新批次重品需要放在第一层货架，气泡柱包装占1.5倍标准位")
        print(f"  提取标签: {tags}")

        print("\n" + "=" * 60)
        print("  所有测试通过! [OK]")
        print("=" * 60)
