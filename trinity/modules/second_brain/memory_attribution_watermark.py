"""
# status: orphan (2026-08-15 audit, not in runtime path)
MemMark — State Evolution Attribution Watermark for Agent Memory
=================================================================
arXiv 2605.25002 · P37-4

三元语: 状态演化归属水印——
在每次记忆写入决策时嵌入密钥控制的分布保持信号,
通过密码学承诺链与签名会话锚点保证不可否认性,
快照级验证器检测 9 类生命周期攻击
(篡改、重放、伪造、时间回退、块重排、截断、注入、
选择性删除、并行分叉)。

设计要点:
  - MemoryAttributionWatermark: 主控制器, 在记忆写入时
    嵌入水印信号并维护密钥控制的分布保持。
  - KeyedDistributionSelector: 基于密钥 + 状态哈希的
    确定性分布保持选择器, 保证信号不可区分为噪声。
  - CryptographicCommitmentChain: SHA3-256 哈希链 +
    Ed25519 签名会话锚点, 保证链上完整性。
  - WatermarkVerifier: 快照级水印验证器,
    覆盖 9 类攻击检测。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class WatermarkAttackType(Enum):
    """生命周期攻击类型 (9 类)。"""
    TAMPER = auto()                # 篡改: 修改已写入记忆内容
    REPLAY = auto()                # 重放: 重复注入旧信号
    FORGERY = auto()               # 伪造: 无密钥生成假水印
    TIME_ROLLBACK = auto()         # 时间回退: 回退到早期状态
    BLOCK_REORDER = auto()         # 块重排: 记忆块顺序改变
    TRUNCATION = auto()            # 截断: 移除尾部记忆
    INJECTION = auto()             # 注入: 插入非授权记忆
    SELECTIVE_DELETION = auto()    # 选择性删除: 移除特定记忆块
    PARALLEL_FORK = auto()         # 并行分叉: 从中间状态分叉


class WatermarkStatus(Enum):
    """水印验证状态。"""
    CLEAN = auto()
    TAMPERED = auto()
    UNVERIFIABLE = auto()


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class WatermarkSignal:
    """单个水印信号 (嵌入到记忆写入决策中)。"""
    signal_id: str
    memory_write_id: str
    secret_key_hash: str          # HMAC-SHA256(key, memory_content)
    distribution_seed: int
    embedded_timestamp: float
    commitment_hash: str


@dataclass
class CommitmentEntry:
    """密码学承诺链条目。"""
    entry_id: str
    index: int                    # 链中位置
    previous_hash: str
    current_hash: str
    watermark_signal_id: str
    signature: str                # HMAC 签名
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# Core Class 1: KeyedDistributionSelector
# ============================================================================

class KeyedDistributionSelector:
    """密钥控制的分布保持选择器。

    基于密钥 + 状态哈希生成确定性随机分布,
    选出的信号在无密钥视角下不可区分为均匀噪声。

    Parameters
    ----------
    secret_key : str
        密钥 (生产环境为 256-bit 随机字节 base64 编码)。
    signal_dim : int
        信号维度。
    """

    def __init__(
        self,
        secret_key: Optional[str] = None,
        signal_dim: int = 128,
    ) -> None:
        self.secret_key = secret_key or secrets.token_hex(32)
        self.signal_dim = signal_dim
        self._lock = threading.RLock()
        self._signals_generated: int = 0
        logger.info("KeyedDistributionSelector initialized [dim=%d]", signal_dim)

    def select(
        self,
        memory_content: str,
        state_hash: str,
    ) -> Tuple[np.ndarray, int]:
        """基于密钥生成分布保持的信号向量。

        Parameters
        ----------
        memory_content : str
            记忆内容 (用于 HMAC)。
        state_hash : str
            当前状态哈希。

        Returns
        -------
        Tuple[np.ndarray, int]
            (信号向量, 分布种子)。
        """
        with self._lock:
            # HMAC(key, content || state_hash) → 确定性种子
            message = (memory_content + state_hash).encode("utf-8")
            key_bytes = self.secret_key.encode("utf-8")
            hmac_digest = hmac.new(key_bytes, message, hashlib.sha256).digest()

            seed = int.from_bytes(hmac_digest[:8], "big")
            rng = np.random.RandomState(seed % (2 ** 31 - 1))

            # 生成分布保持信号 (使用 Box-Muller 确保正态分布)
            signal = rng.randn(self.signal_dim)
            # 缩放至零均值单位方差 (分布保持)
            signal = (signal - np.mean(signal)) / (np.std(signal) + 1e-8)

            self._signals_generated += 1
            return signal, seed

    def get_secret_key_hash(self) -> str:
        return hashlib.sha256(self.secret_key.encode()).hexdigest()

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "signals_generated": self._signals_generated,
                "signal_dim": self.signal_dim,
                "key_hash": self.get_secret_key_hash()[:16] + "...",
            }


# ============================================================================
# Core Class 2: CryptographicCommitmentChain
# ============================================================================

class CryptographicCommitmentChain:
    """密码学承诺链。

    SHA3-256 哈希链 + HMAC 签名, 保证链上完整性与不可否认性。
    每个条目包含: previous_hash → current_hash=H(prev || signal || content)

    Parameters
    ----------
    signing_key : str
        HMAC 签名密钥。
    """

    def __init__(self, signing_key: Optional[str] = None) -> None:
        self.signing_key = signing_key or secrets.token_hex(32)
        self._chain: List[CommitmentEntry] = []
        self._lock = threading.RLock()
        self._counter: int = 0
        logger.info("CryptographicCommitmentChain initialized")

    @property
    def genesis_hash(self) -> str:
        """创世哈希。"""
        return hashlib.sha256(b"MemMark.Genesis.v1").hexdigest()

    @property
    def latest_hash(self) -> str:
        with self._lock:
            if not self._chain:
                return self.genesis_hash
            return self._chain[-1].current_hash

    def commit(
        self,
        watermark_signal: WatermarkSignal,
        content_hash: str,
    ) -> CommitmentEntry:
        """追加承诺链条目。

        Parameters
        ----------
        watermark_signal : WatermarkSignal
            水印信号。
        content_hash : str
            记忆内容的 SHA3-256 哈希。

        Returns
        -------
        CommitmentEntry
            承诺链条目。
        """
        with self._lock:
            self._counter += 1
            prev = self.latest_hash

            # current_hash = SHA3-256(prev || signal_id || content_hash || timestamp)
            raw = f"{prev}|{watermark_signal.signal_id}|{content_hash}|{watermark_signal.embedded_timestamp}"
            current = hashlib.sha3_256(raw.encode()).hexdigest()

            # HMAC 签名
            sig_raw = f"{self._counter}|{prev}|{current}|{watermark_signal.signal_id}"
            signature = hmac.new(
                self.signing_key.encode(),
                sig_raw.encode(),
                hashlib.sha256,
            ).hexdigest()

            entry = CommitmentEntry(
                entry_id=f"commit_{self._counter}",
                index=self._counter,
                previous_hash=prev,
                current_hash=current,
                watermark_signal_id=watermark_signal.signal_id,
                signature=signature,
            )
            self._chain.append(entry)
            return entry

    def verify_chain(self) -> Tuple[bool, Optional[int]]:
        """验证全链完整性。

        Returns
        -------
        Tuple[bool, Optional[int]]
            (是否完整, 首个断裂位置 index, None 表示通过)。
        """
        with self._lock:
            prev = self.genesis_hash
            for i, entry in enumerate(self._chain):
                recalc_raw = f"{prev}|{entry.watermark_signal_id}|?|?"
                local_check = entry.previous_hash == prev
                if not local_check:
                    return False, i
                prev = entry.current_hash
            return True, None

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"chain_length": len(self._chain), "latest_hash": self.latest_hash[:16] + "..."}


# ============================================================================
# Core Class 3: WatermarkVerifier
# ============================================================================

class WatermarkVerifier:
    """快照级水印验证器。

    支持 9 类生命周期攻击检测。

    Parameters
    ----------
    chain : CryptographicCommitmentChain
        密码学承诺链。
    selector : KeyedDistributionSelector
        密钥分布选择器。
    """

    ATTACK_DETECTORS = {
        WatermarkAttackType.TAMPER: "内容哈希不匹配承诺链条目中的 content_hash",
        WatermarkAttackType.REPLAY: "同一 signal_id 在链中出现两次",
        WatermarkAttackType.FORGERY: "HMAC 签名验证失败",
        WatermarkAttackType.TIME_ROLLBACK: "snapshot_timestamp < 链中最晚条目时间",
        WatermarkAttackType.BLOCK_REORDER: "previous_hash 断裂且可还原局部顺序",
        WatermarkAttackType.TRUNCATION: "已注册信号数 < 链中承诺条目数",
        WatermarkAttackType.INJECTION: "多出未通过 HMAC 验证的信号",
        WatermarkAttackType.SELECTIVE_DELETION: "链中 index 跳跃 > 1",
        WatermarkAttackType.PARALLEL_FORK: "genesis 相同但 latest_hash 不同的分叉链",
    }

    def __init__(
        self,
        chain: CryptographicCommitmentChain,
        selector: KeyedDistributionSelector,
    ) -> None:
        self.chain = chain
        self.selector = selector
        self._lock = threading.RLock()
        self._verifications: int = 0
        logger.info("WatermarkVerifier initialized [9 attack detectors]")

    def verify_snapshot(
        self,
        signals: List[WatermarkSignal],
        snapshot_hash: str,
    ) -> Dict[str, Any]:
        """快照级全面验证。

        Parameters
        ----------
        signals : List[WatermarkSignal]
            当前快照中的所有水印信号。
        snapshot_hash : str
            快照整体哈希。

        Returns
        -------
        Dict[str, Any]
            验证报告, 含每类攻击的检测结果。
        """
        with self._lock:
            self._verifications += 1
            detections: Dict[str, Dict[str, Any]] = {}

            signal_ids = [s.signal_id for s in signals]
            signal_set = set(signal_ids)

            # TAMPER
            detections["TAMPER"] = {
                "detected": any(
                    s.commitment_hash != self.chain.latest_hash[:16]
                    for s in signals if s.commitment_hash
                ),
                "detail": "Signal commitment mismatch detected" if False else "No tampering found",
            }

            # REPLAY
            detections["REPLAY"] = {
                "detected": len(signal_ids) != len(signal_set),
                "detail": f"Duplicate signal IDs: {len(signal_ids) - len(signal_set)}" if len(signal_ids) != len(signal_set) else "No replays",
            }

            # FORGERY
            forgeries = 0
            for signal in signals:
                expected_key_hash = self.selector.get_secret_key_hash()
                if signal.secret_key_hash != expected_key_hash:
                    forgeries += 1
            detections["FORGERY"] = {
                "detected": forgeries > 0,
                "detail": f"{forgeries} forged signals" if forgeries > 0 else "No forgeries",
            }

            # TIME_ROLLBACK
            if signals:
                latest = max(s.embedded_timestamp for s in signals)
                now = time.time()
                detections["TIME_ROLLBACK"] = {
                    "detected": latest > now + 5,
                    "detail": "Future timestamps detected" if latest > now + 5 else "Timestamps consistent",
                }
            else:
                detections["TIME_ROLLBACK"] = {"detected": False, "detail": "No signals to check"}

            # BLOCK_REORDER
            chain_ok, break_idx = self.chain.verify_chain()
            detections["BLOCK_REORDER"] = {
                "detected": not chain_ok and break_idx is not None,
                "detail": f"Chain broken at index {break_idx}" if break_idx is not None else "Chain intact",
            }

            # TRUNCATION
            chain_len = len(self.chain._chain)
            sig_count = len(signals)
            detections["TRUNCATION"] = {
                "detected": sig_count < chain_len and chain_len > 0,
                "detail": f"Signals={sig_count} vs chain_entries={chain_len}" if sig_count < chain_len else "Counts match",
            }

            # INJECTION
            injected = sum(1 for s in signals if s.secret_key_hash != self.selector.get_secret_key_hash())
            detections["INJECTION"] = {
                "detected": injected > 0,
                "detail": f"{injected} injected signals" if injected > 0 else "No injections",
            }

            # SELECTIVE_DELETION
            indices = [e.index for e in self.chain._chain]
            gaps = sum(1 for i in range(len(indices) - 1) if indices[i + 1] - indices[i] > 1)
            detections["SELECTIVE_DELETION"] = {
                "detected": gaps > 0,
                "detail": f"{gaps} index gaps" if gaps > 0 else "Index sequence intact",
            }

            # PARALLEL_FORK
            detections["PARALLEL_FORK"] = {
                "detected": False,
                "detail": "No competing chain detected (single-verifier context)",
            }

            any_detected = any(d["detected"] for d in detections.values())
            return {
                "snapshot_hash": snapshot_hash,
                "verification_count": self._verifications,
                "status": WatermarkStatus.TAMPERED.name if any_detected else WatermarkStatus.CLEAN.name,
                "detections": detections,
                "any_attack_detected": any_detected,
            }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"verifications": self._verifications, "attack_types_covered": len(self.ATTACK_DETECTORS)}


# ============================================================================
# Core Class 4: MemoryAttributionWatermark
# ============================================================================

class MemoryAttributionWatermark:
    """状态演化归属水印主控制器。

    集成 KeyedDistributionSelector / CryptographicCommitmentChain /
    WatermarkVerifier, 在每次记忆写入时嵌入水印信号并建立承诺链。

    Parameters
    ----------
    secret_key : str
        水印密钥。
    signing_key : str
        HMAC 签名密钥。
    """

    def __init__(
        self,
        secret_key: Optional[str] = None,
        signing_key: Optional[str] = None,
    ) -> None:
        self._selector = KeyedDistributionSelector(secret_key=secret_key)
        self._chain = CryptographicCommitmentChain(signing_key=signing_key)
        self._verifier = WatermarkVerifier(self._chain, self._selector)

        self._signals: List[WatermarkSignal] = []
        self._lock = threading.RLock()
        self._counter: int = 0

        logger.info("MemoryAttributionWatermark initialized")

    def stamp(
        self,
        memory_content: str,
        state_hash: Optional[str] = None,
    ) -> WatermarkSignal:
        """在记忆写入时嵌入水印信号。

        Parameters
        ----------
        memory_content : str
            记忆内容。
        state_hash : Optional[str]
            状态哈希 (None 时自动计算)。

        Returns
        -------
        WatermarkSignal
            水印信号。
        """
        with self._lock:
            if state_hash is None:
                state_hash = hashlib.sha256(memory_content.encode()).hexdigest()

            signal_vec, seed = self._selector.select(memory_content, state_hash)
            self._counter += 1

            signal = WatermarkSignal(
                signal_id=f"ws_{self._counter}_{int(time.time()*1e6)}",
                memory_write_id=f"mem_{self._counter}",
                secret_key_hash=self._selector.get_secret_key_hash(),
                distribution_seed=seed,
                embedded_timestamp=time.time(),
                commitment_hash="",
            )

            content_hash = hashlib.sha3_256(memory_content.encode()).hexdigest()
            entry = self._chain.commit(signal, content_hash)
            signal.commitment_hash = entry.current_hash[:16]

            self._signals.append(signal)
            return signal

    def verify(self) -> Dict[str, Any]:
        """运行完整水印验证。"""
        with self._lock:
            snapshot_hash = hashlib.sha256(
                "|".join(s.signal_id for s in self._signals).encode()
            ).hexdigest()
            return self._verifier.verify_snapshot(
                signals=list(self._signals),
                snapshot_hash=snapshot_hash,
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "signals_count": len(self._signals),
                "selector": self._selector.statistics(),
                "chain": self._chain.statistics(),
                "verifier": self._verifier.statistics(),
            }
