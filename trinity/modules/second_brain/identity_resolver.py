"""
# status: orphan (2026-08-15 audit, not in runtime path)
P5-4: Cross-Session Identity Resolver (对标 Mem0 2026 年报)
=============================================================

支持匿名会话指纹匹配、多设备用户关联、混合认证流下的身份统一。
基于行为特征 + 设备指纹 + 时序模式做概率匹配，不依赖稳定 user_id。

Mem0 2026 年报指出的关键挑战：
  - 匿名会话：无 user_id 时如何关联同一用户的多次交互
  - 多设备用户：同一用户在不同设备（手机/PC/浏览器）上的身份统一
  - 混合认证流：部分会话有登录态、部分匿名，需统一身份图谱
  - 基础假设动摇：记忆模型假设 stable user_id 不再成立

设计策略：
  - 设备指纹（Device Fingerprint）：OS / 浏览器 / 屏幕 / 硬件特征
  - 行为特征（Behavioral Fingerprint）：语言风格 / 常用词 / 作息模式 / 主题偏好
  - 时序模式（Temporal Pattern）：活跃时间分布 / 会话间隔模式
  - 概率匹配：多特征融合 + 贝叶斯更新，输出置信度

Reference: Mem0 Blog, "AI Agent Memory 2026: Progress Benchmark Report",
           Cross-Session Identity Resolution section.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────────

class AuthMode(Enum):
    """认证模式。"""
    LOGGED_IN = "logged_in"
    ANONYMOUS = "anonymous"
    MIXED = "mixed"
    OAUTH_GUEST = "oauth_guest"
    DEVICE_ONLY = "device_only"


class MatchConfidence(Enum):
    """匹配置信度等级。"""
    DEFINITE = "definite"
    HIGH = "high"
    PROBABLE = "probable"
    POSSIBLE = "possible"
    UNCERTAIN = "uncertain"
    UNLIKELY = "unlikely"


# ── 数据结构 ─────────────────────────────────────────────────────────

@dataclass
class DeviceFingerprint:
    fingerprint_id: str
    os_name: str = "unknown"; os_version: str = ""
    browser: str = "unknown"; browser_version: str = ""
    screen_resolution: str = ""; device_type: str = "unknown"
    cpu_cores: int = 0; memory_gb: float = 0.0
    timezone: str = ""; language: str = ""
    canvas_hash: str = ""; plugin_hash: str = ""; ip_prefix: str = ""


@dataclass
class BehavioralProfile:
    profile_id: str
    language_style: Dict[str, float] = field(default_factory=dict)
    active_hours: List[float] = field(default_factory=lambda: [0.0] * 24)
    session_duration_avg: float = 0.0; session_gap_avg: float = 0.0
    topic_preferences: Dict[str, int] = field(default_factory=dict)
    typing_pattern: Dict[str, float] = field(default_factory=dict)
    avg_message_length: float = 0.0; emoji_usage_rate: float = 0.0
    code_usage_rate: float = 0.0; question_ratio: float = 0.0


@dataclass
class SessionRecord:
    session_id: str
    user_id: Optional[str] = None
    device_fingerprint_id: str = ""; behavioral_profile_id: str = ""
    auth_mode: AuthMode = AuthMode.ANONYMOUS
    start_time: float = field(default_factory=time.time)
    end_time: float = field(default_factory=time.time)
    ip_address: str = ""; resolved_identity_id: str = ""


@dataclass
class IdentityCluster:
    identity_id: str
    user_ids: List[str] = field(default_factory=list)
    session_ids: List[str] = field(default_factory=list)
    device_ids: List[str] = field(default_factory=list)
    confidence: float = 0.5
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IdentityMatchResult:
    source_session_id: str; target_identity_id: str
    confidence: float; match_level: MatchConfidence
    device_similarity: float; behavioral_similarity: float
    temporal_coherence: float; factors: List[str]; recommendation: str


# ── _EntityLinker：特征提取与相似度计算 ────────────────────────────────

class _EntityLinker:
    """实体链接：设备指纹创建、行为画像创建、多维度相似度计算。"""

    def __init__(self, parent: "IdentityResolver") -> None:
        self._p = parent

    def create_or_update_device_fingerprint(self, raw: Dict[str, Any]) -> DeviceFingerprint:
        fp_str = json.dumps(raw, sort_keys=True, default=str)
        fp_hash = hashlib.sha256(fp_str.encode()).hexdigest()[:16]
        for existing_fp in self._p._device_fingerprints.values():
            existing_str = json.dumps(
                {"os": existing_fp.os_name, "os_ver": existing_fp.os_version,
                 "browser": existing_fp.browser, "device": existing_fp.device_type,
                 "screen": existing_fp.screen_resolution, "canvas": existing_fp.canvas_hash},
                sort_keys=True)
            existing_hash = hashlib.sha256(existing_str.encode()).hexdigest()[:16]
            if fp_hash == existing_hash:
                return existing_fp
        fp = DeviceFingerprint(
            fingerprint_id=fp_hash, os_name=raw.get("os_name", "unknown"),
            os_version=raw.get("os_version", ""), browser=raw.get("browser", "unknown"),
            browser_version=raw.get("browser_version", ""),
            screen_resolution=raw.get("screen_resolution", ""),
            device_type=raw.get("device_type", "unknown"), cpu_cores=raw.get("cpu_cores", 0),
            memory_gb=raw.get("memory_gb", 0.0), timezone=raw.get("timezone", ""),
            language=raw.get("language", ""), canvas_hash=raw.get("canvas_hash", ""),
            plugin_hash=raw.get("plugin_hash", ""), ip_prefix=raw.get("ip_prefix", ""))
        self._p._device_fingerprints[fp.fingerprint_id] = fp
        return fp

    def create_behavioral_profile(self, raw: Dict[str, Any]) -> BehavioralProfile:
        profile_id = f"bp_{uuid.uuid4().hex[:12]}"
        profile = BehavioralProfile(
            profile_id=profile_id, language_style=raw.get("language_style", {}),
            active_hours=raw.get("active_hours", [0.0] * 24),
            session_duration_avg=raw.get("session_duration_avg", 0.0),
            session_gap_avg=raw.get("session_gap_avg", 0.0),
            topic_preferences=raw.get("topic_preferences", {}),
            typing_pattern=raw.get("typing_pattern", {}),
            avg_message_length=raw.get("avg_message_length", 0.0),
            emoji_usage_rate=raw.get("emoji_usage_rate", 0.0),
            code_usage_rate=raw.get("code_usage_rate", 0.0),
            question_ratio=raw.get("question_ratio", 0.0))
        self._p._behavioral_profiles[profile_id] = profile
        return profile

    def compute_match_scores(self, session: SessionRecord,
                              dev_fp: Optional[DeviceFingerprint],
                              beh_prof: Optional[BehavioralProfile],
                              cluster: IdentityCluster) -> Dict[str, float]:
        scores: Dict[str, float] = {}
        # device
        ds = 0.0
        for device_id in cluster.device_ids:
            if dev_fp and device_id == dev_fp.fingerprint_id: ds = 1.0; break
            elif dev_fp:
                cfp = self._p._device_fingerprints.get(device_id)
                if cfp: ds = max(ds, self._device_similarity(dev_fp, cfp))
        scores["device_fingerprint"] = ds
        # behavioral
        bs = 0.0
        if beh_prof:
            for sid in cluster.session_ids:
                cs = self._p._sessions.get(sid)
                if cs:
                    cp = self._p._behavioral_profiles.get(cs.behavioral_profile_id)
                    if cp: bs = max(bs, self._behavior_similarity(beh_prof, cp))
        scores["behavioral_profile"] = bs
        # temporal
        scores["temporal_pattern"] = self._temporal_coherence(session, cluster)
        # IP
        ips = 0.0
        if session.ip_address:
            for sid in cluster.session_ids:
                cs = self._p._sessions.get(sid)
                if cs and cs.ip_address:
                    ips = max(ips, self._ip_similarity(session.ip_address, cs.ip_address))
        scores["ip_proximity"] = ips
        # user_id
        uid = 1.0 if (session.user_id and session.user_id in cluster.user_ids) else (0.6 if session.user_id else 0.0)
        scores["user_id_match"] = uid
        return scores

    def _device_similarity(self, a: DeviceFingerprint, b: DeviceFingerprint) -> float:
        c = []
        if a.os_name == b.os_name:
            c.append(0.3)
            if a.os_version == b.os_version: c.append(0.1)
        if a.browser == b.browser: c.append(0.15)
        if a.device_type == b.device_type: c.append(0.15)
        if a.screen_resolution == b.screen_resolution: c.append(0.1)
        if a.timezone == b.timezone: c.append(0.05)
        if a.language == b.language: c.append(0.05)
        if a.canvas_hash and a.canvas_hash == b.canvas_hash: c.append(0.1)
        return sum(c)

    def _behavior_similarity(self, a: BehavioralProfile, b: BehavioralProfile) -> float:
        score = 0.0; n = 0
        if a.avg_message_length > 0 and b.avg_message_length > 0:
            ratio = min(a.avg_message_length, b.avg_message_length) / max(a.avg_message_length, b.avg_message_length)
            score += ratio * 0.2; n += 1
        if a.emoji_usage_rate > 0 or b.emoji_usage_rate > 0:
            diff = abs(a.emoji_usage_rate - b.emoji_usage_rate); score += max(0, 1 - diff * 5) * 0.15; n += 1
        if a.code_usage_rate > 0 or b.code_usage_rate > 0:
            diff = abs(a.code_usage_rate - b.code_usage_rate); score += max(0, 1 - diff * 5) * 0.15; n += 1
        if a.question_ratio > 0 or b.question_ratio > 0:
            diff = abs(a.question_ratio - b.question_ratio); score += max(0, 1 - diff * 3) * 0.15; n += 1
        all_t = set(a.topic_preferences.keys()) | set(b.topic_preferences.keys())
        if all_t:
            overlap = set(a.topic_preferences.keys()) & set(b.topic_preferences.keys())
            score += len(overlap) / len(all_t) * 0.2; n += 1
        if any(a.active_hours) and any(b.active_hours):
            ah_a, ah_b = [1 if v > 0 else 0 for v in a.active_hours], [1 if v > 0 else 0 for v in b.active_hours]
            inter = sum(x & y for x, y in zip(ah_a, ah_b)); union = sum(x | y for x, y in zip(ah_a, ah_b))
            if union > 0: score += (inter / union) * 0.15; n += 1
        return score / max(n, 1)

    def _temporal_coherence(self, session: SessionRecord, cluster: IdentityCluster) -> float:
        if not cluster.session_ids: return 0.5
        cluster_sessions = [self._p._sessions[sid] for sid in cluster.session_ids if sid in self._p._sessions]
        if not cluster_sessions: return 0.5
        gaps = []
        sorted_s = sorted(cluster_sessions, key=lambda s: s.start_time)
        for i in range(1, len(sorted_s)):
            gaps.append(sorted_s[i].start_time - sorted_s[i - 1].end_time)
        if not gaps: return 0.8
        avg_gap = sum(gaps) / len(gaps)
        current_gap = session.start_time - sorted_s[-1].end_time
        if avg_gap > 0:
            ratio = current_gap / avg_gap
            if 0.5 <= ratio <= 2.0: return 0.9
            elif 0.2 <= ratio <= 5.0: return 0.7
            else: return max(0.2, 1.0 - ratio / 10.0)
        return 0.5

    @staticmethod
    def _ip_similarity(ip_a: str, ip_b: str) -> float:
        if ip_a == ip_b: return 1.0
        pa, pb = ip_a.split("."), ip_b.split(".")
        if len(pa) >= 3 and len(pb) >= 3:
            if pa[:2] == pb[:2]: return 0.7
            if pa[:1] == pb[:1]: return 0.4
        return 0.1

    @staticmethod
    def _score_to_confidence(score: float) -> MatchConfidence:
        if score >= 0.99: return MatchConfidence.DEFINITE
        elif score >= 0.9: return MatchConfidence.HIGH
        elif score >= 0.7: return MatchConfidence.PROBABLE
        elif score >= 0.5: return MatchConfidence.POSSIBLE
        elif score >= 0.3: return MatchConfidence.UNCERTAIN
        return MatchConfidence.UNLIKELY


# ── _DisambiguationEngine：身份解析与聚类管理 ─────────────────────────

class _DisambiguationEngine:
    """消歧引擎：概率匹配解析、身份簇创建与合并。"""

    def __init__(self, parent: "IdentityResolver") -> None:
        self._p = parent

    def resolve_identity(self, session_id: str) -> Optional[IdentityMatchResult]:
        with self._p._lock:
            sess = self._p._sessions.get(session_id)
            if not sess: return None
            self._p._total_resolutions += 1
            # 快速路径：user_id 精确匹配
            if sess.user_id and sess.user_id in self._p._user_id_index:
                eids = self._p._user_id_index[sess.user_id]
                if eids:
                    cluster = self._p._identity_clusters.get(eids[0])
                    if cluster:
                        sess.resolved_identity_id = cluster.identity_id
                        return IdentityMatchResult(
                            source_session_id=session_id, target_identity_id=cluster.identity_id,
                            confidence=0.99, match_level=MatchConfidence.DEFINITE,
                            device_similarity=1.0, behavioral_similarity=1.0,
                            temporal_coherence=1.0, factors=["user_id_exact_match"],
                            recommendation="直接通过 user_id 匹配")
            # 快速路径：设备指纹精确匹配
            if sess.device_fingerprint_id in self._p._device_index:
                eid = self._p._device_index[sess.device_fingerprint_id]
                return IdentityMatchResult(
                    source_session_id=session_id, target_identity_id=eid,
                    confidence=0.95, match_level=MatchConfidence.HIGH,
                    device_similarity=1.0, behavioral_similarity=0.5, temporal_coherence=0.5,
                    factors=["device_fingerprint_exact_match"], recommendation="设备指纹精确匹配")
            # 全面多特征匹配
            dev_fp = self._p._device_fingerprints.get(sess.device_fingerprint_id)
            beh_prof = self._p._behavioral_profiles.get(sess.behavioral_profile_id)
            best_match = None; best_score = 0.0
            for iid, cluster in self._p._identity_clusters.items():
                scores = self._p._linker.compute_match_scores(sess, dev_fp, beh_prof, cluster)
                total = sum(scores[k] * self._p._weights[k] for k in self._p._weights)
                if total > best_score:
                    best_score = total
                    level = self._p._linker._score_to_confidence(total)
                    best_match = IdentityMatchResult(
                        source_session_id=session_id, target_identity_id=iid,
                        confidence=total, match_level=level,
                        device_similarity=scores.get("device_fingerprint", 0),
                        behavioral_similarity=scores.get("behavioral_profile", 0),
                        temporal_coherence=scores.get("temporal_pattern", 0),
                        factors=[f"{k}={v:.2f}" for k, v in scores.items() if v > 0.3],
                        recommendation=(f"建议合并到 identity={iid}" if total >= self._p._match_threshold
                                        else "建议创建新身份"))
            if best_match and best_match.confidence >= self._p._match_threshold:
                self._merge_session_to_cluster(session_id, best_match.target_identity_id)
                sess.resolved_identity_id = best_match.target_identity_id
                self._p._total_matches += 1
            else:
                new_id = self._create_identity_cluster(sess)
                sess.resolved_identity_id = new_id
                best_match = IdentityMatchResult(
                    source_session_id=session_id, target_identity_id=new_id,
                    confidence=1.0, match_level=MatchConfidence.DEFINITE,
                    device_similarity=1.0, behavioral_similarity=1.0,
                    temporal_coherence=1.0, factors=["new_identity_created"],
                    recommendation="新身份已创建")
            self._p._match_history.append(best_match)
            return best_match

    def _create_identity_cluster(self, session: SessionRecord) -> str:
        iid = f"id_{uuid.uuid4().hex[:12]}"
        cluster = IdentityCluster(
            identity_id=iid,
            user_ids=[session.user_id] if session.user_id else [],
            session_ids=[session.session_id],
            device_ids=[session.device_fingerprint_id], confidence=1.0)
        self._p._identity_clusters[iid] = cluster
        if session.user_id:
            self._p._user_id_index[session.user_id].append(iid)
        self._p._device_index[session.device_fingerprint_id] = iid
        return iid

    def _merge_session_to_cluster(self, session_id: str, target_identity_id: str) -> None:
        cluster = self._p._identity_clusters.get(target_identity_id)
        session = self._p._sessions.get(session_id)
        if not cluster or not session: return
        if session_id not in cluster.session_ids:
            cluster.session_ids.append(session_id)
        if session.user_id and session.user_id not in cluster.user_ids:
            cluster.user_ids.append(session.user_id)
            self._p._user_id_index[session.user_id].append(target_identity_id)
        if session.device_fingerprint_id not in cluster.device_ids:
            cluster.device_ids.append(session.device_fingerprint_id)
            self._p._device_index[session.device_fingerprint_id] = target_identity_id
        cluster.updated_at = time.time()


# ── IdentityResolver (Facade) ──────────────────────────────────────────

class IdentityResolver:
    """跨会话身份解析器。不依赖稳定 user_id，基于多模态特征做概率匹配。

    Usage:
        resolver = IdentityResolver()
        sid = resolver.register_session(session_id="sess_1",
            device_fp={"os_name": "Windows 10"}, behaviors={"avg_message_length": 120})
        match = resolver.resolve_identity("sess_1")
    """

    DEFAULT_WEIGHTS = {"device_fingerprint": 0.35, "behavioral_profile": 0.30,
                       "temporal_pattern": 0.15, "ip_proximity": 0.10, "user_id_match": 0.10}

    def __init__(self, weights: Optional[Dict[str, float]] = None, match_threshold: float = 0.5):
        self._lock = threading.RLock()
        self._weights = weights or self.DEFAULT_WEIGHTS.copy()
        self._match_threshold = match_threshold
        self._sessions: Dict[str, SessionRecord] = {}
        self._device_fingerprints: Dict[str, DeviceFingerprint] = {}
        self._behavioral_profiles: Dict[str, BehavioralProfile] = {}
        self._identity_clusters: Dict[str, IdentityCluster] = {}
        self._user_id_index: Dict[str, List[str]] = defaultdict(list)
        self._device_index: Dict[str, str] = {}
        self._ip_index: Dict[str, List[str]] = defaultdict(list)
        self._total_sessions = 0; self._total_resolutions = 0
        self._total_matches = 0; self._match_history: List[IdentityMatchResult] = []
        self._linker = _EntityLinker(self); self._engine = _DisambiguationEngine(self)

    def register_session(self, session_id: str, device_fingerprint: Optional[Dict[str, Any]] = None,
                         behavioral_data: Optional[Dict[str, Any]] = None,
                         user_id: Optional[str] = None, auth_mode: AuthMode = AuthMode.ANONYMOUS,
                         ip_address: str = "") -> str:
        with self._lock:
            dev_fp = self._linker.create_or_update_device_fingerprint(device_fingerprint or {})
            beh_prof = self._linker.create_behavioral_profile(behavioral_data or {})
            session = SessionRecord(
                session_id=session_id, user_id=user_id,
                device_fingerprint_id=dev_fp.fingerprint_id,
                behavioral_profile_id=beh_prof.profile_id,
                auth_mode=auth_mode, start_time=time.time(), ip_address=ip_address)
            self._sessions[session_id] = session; self._total_sessions += 1
            if ip_address:
                ip_prefix = ".".join(ip_address.split(".")[:2])
                self._ip_index[ip_prefix].append(session_id)
            self._engine.resolve_identity(session_id)
            return session_id

    def resolve_identity(self, session_id: str) -> Optional[IdentityMatchResult]:
        return self._engine.resolve_identity(session_id)

    def get_identity(self, identity_id: str) -> Optional[IdentityCluster]:
        return self._identity_clusters.get(identity_id)

    def get_session_identity(self, session_id: str) -> Optional[str]:
        session = self._sessions.get(session_id)
        if session and session.resolved_identity_id:
            return session.resolved_identity_id
        result = self.resolve_identity(session_id)
        return result.target_identity_id if result else None

    def list_identities(self) -> List[IdentityCluster]:
        return list(self._identity_clusters.values())

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            match_levels = defaultdict(int)
            for m in self._match_history[-100:]: match_levels[m.match_level.value] += 1
            return {
                "total_sessions": self._total_sessions,
                "total_resolutions": self._total_resolutions,
                "total_matches": self._total_matches,
                "active_identities": len(self._identity_clusters),
                "active_sessions": len(self._sessions),
                "device_fingerprints": len(self._device_fingerprints),
                "behavioral_profiles": len(self._behavioral_profiles),
                "resolution_rate": self._total_resolutions / max(self._total_sessions, 1),
                "match_distribution": dict(match_levels),
                "ip_prefixes_tracked": len(self._ip_index)}
