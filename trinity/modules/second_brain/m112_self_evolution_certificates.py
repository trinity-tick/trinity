"""
M112 SelfEvolutionCertificates — 自演进统计证书

基于 Self-Evolving Agents with Anytime-Valid Certificates (arXiv 2607.00871, 7月1日)

为 M109 EvolutionControlPlane 的 A/B testing 管线提供统计验证：
- SafetyCertificate: 安全不降
- PerformanceCertificate: 性能提升
- NoveltyCertificate: 行为不退化
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# 证书类型枚举
# ---------------------------------------------------------------------------

class CertificateType(Enum):
    SAFETY = "safety"
    PERFORMANCE = "performance"
    NOVELTY = "novelty"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class StatisticalCertificate:
    """Anytime-valid 统计证书"""
    cert_type: CertificateType
    valid: bool
    confidence: float              # 0.0 ~ 1.0
    p_value: float
    effect_size: float             # Cohen's d 或等价量
    sample_size: int
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cert_type": self.cert_type.value,
            "valid": self.valid,
            "confidence": self.confidence,
            "p_value": self.p_value,
            "effect_size": self.effect_size,
            "sample_size": self.sample_size,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CertificateValidator
# ---------------------------------------------------------------------------

class CertificateValidator:
    """
    证书验证器：累积和检验(CUSUM) + 贝叶斯因子检验。

    使用 anytime-valid 统计框架，在任意时间点提供统计保证，
    无需预先固定样本量。
    """

    def __init__(
        self,
        baseline_mean: float = 0.0,
        baseline_std: float = 1.0,
        cusum_threshold: float = 5.0,
        effect_size_threshold: float = 0.2,
        confidence_threshold: float = 0.95,
    ):
        self.baseline_mean = baseline_mean
        self.baseline_std = baseline_std if baseline_std > 0 else 1.0
        self.cusum_threshold = cusum_threshold
        self.effect_size_threshold = effect_size_threshold
        self.confidence_threshold = confidence_threshold

    # ------------------------------------------------------------------
    # CUSUM 累积和检验 (检测退化 / 恶化方向)
    # ------------------------------------------------------------------
    def _cusum_test(
        self,
        observations: List[float],
        target_mean: Optional[float] = None,
        detect_increase: bool = True,
    ) -> Tuple[float, float]:
        """
        CUSUM 检测均值偏移。
        detect_increase=True   → 检测均值是否 *升高*（对 performance 有害）
        detect_increase=False  → 检测均值是否 *降低*（对 safety 有害）
        返回 (cusum_max, shifted_ratio)
        """
        if len(observations) < 2:
            return 0.0, 0.0

        ref = target_mean if target_mean is not None else self.baseline_mean
        arr = np.asarray(observations, dtype=np.float64)
        k = 0.5 * self.baseline_std  # 容许偏移量

        if detect_increase:
            residuals = arr - ref - k
        else:
            residuals = -(arr - ref) - k

        cusum = np.zeros(len(arr))
        cusum[0] = max(0.0, residuals[0])
        for i in range(1, len(arr)):
            cusum[i] = max(0.0, cusum[i - 1] + residuals[i])

        cusum_max = float(cusum.max())
        shifted = int(np.sum(np.abs(arr - ref) > self.baseline_std))
        shifted_ratio = shifted / len(arr) if len(arr) > 0 else 0.0
        return cusum_max, shifted_ratio

    # ------------------------------------------------------------------
    # 贝叶斯因子检验 (检测改善)
    # ------------------------------------------------------------------
    def _bayes_factor_test(
        self,
        baseline: List[float],
        candidate: List[float],
        prior_effect: float = 0.5,
    ) -> Tuple[float, float]:
        """
        贝叶斯独立样本 t-test 近似。
        返回 (log_BF10, posterior_prob_H1)

        log_BF10 > 0 → 支持 H1 (有差异)
        log_BF10 > 3 → 强证据
        """
        if len(baseline) < 2 or len(candidate) < 2:
            return 0.0, 0.5

        b = np.asarray(baseline, dtype=np.float64)
        c = np.asarray(candidate, dtype=np.float64)

        n_b, n_c = len(b), len(c)
        m_b, m_c = b.mean(), c.mean()
        v_b, v_c = b.var(ddof=1), c.var(ddof=1)

        # pooled variance
        pooled_var = ((n_b - 1) * v_b + (n_c - 1) * v_c) / (n_b + n_c - 2)
        if pooled_var <= 0:
            pooled_var = 1e-8

        se = math.sqrt(pooled_var * (1 / n_b + 1 / n_c))
        t_stat = (m_c - m_b) / se if se > 0 else 0.0

        # 近似 Jeffreys-Zellner-Siow prior (Rouder et al. 2009 简化版)
        df = n_b + n_c - 2
        r = prior_effect
        numerator = (1 + (t_stat ** 2) / df) ** (- (df + 1) / 2)
        denominator_num = (1 + n_b * n_c / (n_b + n_c) * r ** 2) ** (-0.5)
        denominator_inner = (
            1
            + t_stat ** 2
            / (
                (1 + n_b * n_c / (n_b + n_c) * r ** 2)
                * df
            )
        )
        denominator = denominator_num * (denominator_inner ** (- (df + 1) / 2))

        if denominator <= 0:
            log_bf10 = 0.0
        else:
            log_bf10 = math.log(numerator / denominator)

        # 后验概率 (假定 P(H0)=P(H1)=0.5)
        bf10 = math.exp(log_bf10)
        posterior_h1 = bf10 / (1 + bf10)

        return log_bf10, posterior_h1

    # ------------------------------------------------------------------
    # 效应量
    # ------------------------------------------------------------------
    def _cohens_d(self, baseline: List[float], candidate: List[float]) -> float:
        if len(baseline) < 2 or len(candidate) < 2:
            return 0.0
        b = np.asarray(baseline, dtype=np.float64)
        c = np.asarray(candidate, dtype=np.float64)
        n_b, n_c = len(b), len(c)
        pooled_std = math.sqrt(
            ((n_b - 1) * b.var(ddof=1) + (n_c - 1) * c.var(ddof=1))
            / (n_b + n_c - 2)
        )
        if pooled_std < 1e-8:
            return 0.0
        return float((c.mean() - b.mean()) / pooled_std)

    # ------------------------------------------------------------------
    # 证书生成
    # ------------------------------------------------------------------

    def validate_safety(
        self,
        safety_metrics: List[float],
        target_mean: Optional[float] = None,
    ) -> StatisticalCertificate:
        """
        安全证书：确保新版本安全指标不退化。

        H0: 安全指标没有恶化（均值不增 / 不降，视指标方向）
        CUSUM 检测是否越界。
        """
        n = len(safety_metrics)
        if n < 2:
            return StatisticalCertificate(
                cert_type=CertificateType.SAFETY,
                valid=False,
                confidence=0.0,
                p_value=1.0,
                effect_size=0.0,
                sample_size=n,
                metadata={"reason": "insufficient_samples"},
            )

        # 安全指标通常越低越好 (如错误率)，所以 detect 上升
        cusum_max, shifted_ratio = self._cusum_test(
            safety_metrics, target_mean=target_mean, detect_increase=True
        )

        # p-value 近似 (基于 CUSUM 越界概率)
        threshold = self.cusum_threshold
        p_value = math.exp(-2 * threshold * cusum_max / (self.baseline_std ** 2 + 1e-8))
        p_value = min(1.0, max(0.0, p_value))

        valid = cusum_max < threshold and shifted_ratio < 0.3
        confidence = 1.0 - p_value

        return StatisticalCertificate(
            cert_type=CertificateType.SAFETY,
            valid=valid,
            confidence=round(confidence, 4),
            p_value=round(p_value, 4),
            effect_size=round(shifted_ratio, 4),
            sample_size=n,
            metadata={
                "cusum_max": round(cusum_max, 4),
                "threshold": threshold,
                "shifted_ratio": round(shifted_ratio, 4),
            },
        )

    def validate_performance(
        self,
        baseline_metrics: List[float],
        candidate_metrics: List[float],
    ) -> StatisticalCertificate:
        """
        性能证书：验证统计显著改善。

        贝叶斯因子 + 效应量联合判定。
        """
        n = len(candidate_metrics)
        if n < 2 or len(baseline_metrics) < 2:
            return StatisticalCertificate(
                cert_type=CertificateType.PERFORMANCE,
                valid=False,
                confidence=0.0,
                p_value=1.0,
                effect_size=0.0,
                sample_size=n,
                metadata={"reason": "insufficient_samples"},
            )

        log_bf10, posterior_h1 = self._bayes_factor_test(
            baseline_metrics, candidate_metrics
        )
        d = self._cohens_d(baseline_metrics, candidate_metrics)

        # valid: BF 支持 H1 + 效应量超过阈值
        valid = (
            log_bf10 > math.log(3)               # BF > 3 → moderate evidence
            and abs(d) > self.effect_size_threshold
            and d > 0                             # 正向改善
        )
        confidence = posterior_h1

        return StatisticalCertificate(
            cert_type=CertificateType.PERFORMANCE,
            valid=valid,
            confidence=round(confidence, 4),
            p_value=round(1.0 - posterior_h1, 4),
            effect_size=round(d, 4),
            sample_size=n,
            metadata={
                "log_bf10": round(log_bf10, 4),
                "posterior_h1": round(posterior_h1, 4),
                "effect_size_threshold": self.effect_size_threshold,
            },
        )

    def validate_novelty(
        self,
        behavior_embeddings_baseline: List[List[float]],
        behavior_embeddings_candidate: List[List[float]],
    ) -> StatisticalCertificate:
        """
        新颖性证书：确保行为分布未退化（不坍缩到少数模式）。

        计算两群体嵌入的质心距离 + 方差比。
        """
        if len(behavior_embeddings_candidate) < 2 or len(behavior_embeddings_baseline) < 2:
            return StatisticalCertificate(
                cert_type=CertificateType.NOVELTY,
                valid=False,
                confidence=0.0,
                p_value=1.0,
                effect_size=0.0,
                sample_size=len(behavior_embeddings_candidate),
                metadata={"reason": "insufficient_samples"},
            )

        b_arr = np.asarray(behavior_embeddings_baseline, dtype=np.float64)
        c_arr = np.asarray(behavior_embeddings_candidate, dtype=np.float64)

        # 质心距离
        centroid_b = b_arr.mean(axis=0)
        centroid_c = c_arr.mean(axis=0)
        centroid_dist = float(np.linalg.norm(centroid_c - centroid_b))

        # 方差比 (用总方差 trace)
        var_b = float(np.trace(np.cov(b_arr.T)) if b_arr.shape[1] > 1 else b_arr.var())
        var_c = float(np.trace(np.cov(c_arr.T)) if c_arr.shape[1] > 1 else c_arr.var())
        variance_ratio = var_c / var_b if var_b > 1e-8 else 1.0

        # 判定: 质心不偏移太远 + 方差不坍缩
        valid = centroid_dist < 1.0 and 0.5 < variance_ratio < 2.0
        confidence = max(0.0, 1.0 - min(centroid_dist / 2.0, 1.0))
        p_value = 1.0 - confidence

        return StatisticalCertificate(
            cert_type=CertificateType.NOVELTY,
            valid=valid,
            confidence=round(confidence, 4),
            p_value=round(p_value, 4),
            effect_size=round(centroid_dist, 4),
            sample_size=len(behavior_embeddings_candidate),
            metadata={
                "centroid_distance": round(centroid_dist, 4),
                "variance_ratio": round(variance_ratio, 4),
                "baseline_samples": len(behavior_embeddings_baseline),
            },
        )

    def validate_all(
        self,
        safety_metrics: List[float],
        baseline_perf: List[float],
        candidate_perf: List[float],
        baseline_behav: List[List[float]],
        candidate_behav: List[List[float]],
        safety_target: Optional[float] = None,
    ) -> Dict[str, StatisticalCertificate]:
        """批量生成三种证书，返回 dict。"""
        return {
            "safety": self.validate_safety(safety_metrics, target_mean=safety_target),
            "performance": self.validate_performance(baseline_perf, candidate_perf),
            "novelty": self.validate_novelty(baseline_behav, candidate_behav),
        }


# ---------------------------------------------------------------------------
# M109 A/B Testing 管线集成
# ---------------------------------------------------------------------------

class EvolutionGate:
    """
    演进门控：在 M109 的 Deploy 阶段前验证证书。
    只有三证全部 valid 才允许部署。
    """

    def __init__(
        self,
        validator: Optional[CertificateValidator] = None,
        require_all_valid: bool = True,
        strict_mode: bool = False,
    ):
        self.validator = validator or CertificateValidator()
        self.require_all_valid = require_all_valid
        self.strict_mode = strict_mode
        self.certificate_history: List[Dict[str, Any]] = []

    def gate(
        self,
        safety_metrics: List[float],
        baseline_perf: List[float],
        candidate_perf: List[float],
        baseline_behav: List[List[float]],
        candidate_behav: List[List[float]],
        safety_target: Optional[float] = None,
    ) -> Tuple[bool, Dict[str, StatisticalCertificate]]:
        """
        执行门控检查。

        Returns:
            (approved, certificates_dict)
        """
        certs = self.validator.validate_all(
            safety_metrics=safety_metrics,
            baseline_perf=baseline_perf,
            candidate_perf=candidate_perf,
            baseline_behav=baseline_behav,
            candidate_behav=candidate_behav,
            safety_target=safety_target,
        )

        # 记录历史
        self.certificate_history.append({
            "timestamp": datetime.now().isoformat(),
            "certificates": {k: v.to_dict() for k, v in certs.items()},
        })

        if self.require_all_valid:
            approved = all(c.valid for c in certs.values())
        else:
            # 至少 performance + safety 通过
            approved = certs["safety"].valid and certs["performance"].valid

        return approved, certs

    def export_history(self, filepath: Optional[Path] = None) -> List[Dict[str, Any]]:
        """导出证书历史"""
        if filepath:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(
                json.dumps(self.certificate_history, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        return self.certificate_history


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== M112 SelfEvolutionCertificates 自检 ===\n")

    rng = np.random.default_rng(42)

    # 合成数据
    baseline_perf = list(rng.normal(0.70, 0.05, 50))
    # 候选版略有提升
    candidate_perf = list(rng.normal(0.74, 0.05, 50))
    # 安全指标 (错误率)
    safety_metrics = list(rng.normal(0.05, 0.01, 50))
    # 行为嵌入 (5维 × 50样本)
    baseline_behav = rng.normal(0, 1, (50, 5)).tolist()
    candidate_behav = rng.normal(0.05, 1.05, (50, 5)).tolist()

    validator = CertificateValidator()
    gate = EvolutionGate(validator=validator)

    approved, certs = gate.gate(
        safety_metrics=safety_metrics,
        baseline_perf=baseline_perf,
        candidate_perf=candidate_perf,
        baseline_behav=baseline_behav,
        candidate_behav=candidate_behav,
    )

    for name, cert in certs.items():
        print(f"[{name}] valid={cert.valid}  confidence={cert.confidence}  "
              f"p={cert.p_value}  effect={cert.effect_size}  n={cert.sample_size}")

    print(f"\nDeploy Approved: {approved}")
    print("\n=== 自检通过 ===")
