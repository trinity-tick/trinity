"""
P12-7: Intent-Aware Compression — 对标 SimpleMem (ICML 2026)

实现意图感知的交互记忆压缩:
  - HierarchicalClustering: 对交互片段层次聚类 (Agglomerative + dendrogram 剪枝)
  - IntentCentroidLearner: 从任务标签和工具调用信号学习意图中心向量
  - IntentAwareRetriever: 预测查询意图后检索对齐的压缩摘要
  - 意图空间低维嵌入 (default dim=128)

Reference:
    SimpleMem: Simple Yet Powerful Memory Management for LLM Agents (ICML 2026)
"""

import math
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence


# ══════════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════════

class ClusterStrategy(Enum):
    """聚类策略。"""
    AGGLOMERATIVE = "agglomerative"  # 自底向上凝聚
    DIVISIVE = "divisive"             # 自顶向下分裂
    SPECTRAL = "spectral"             # 谱聚类


class DistanceMetric(Enum):
    """距离度量。"""
    COSINE = "cosine"
    EUCLIDEAN = "euclidean"
    MANHATTAN = "manhattan"
    DOT_PRODUCT = "dot_product"


class IntentSpaceStatus(Enum):
    """意图空间状态。"""
    UNTRAINED = "untrained"
    PARTIALLY_TRAINED = "partially_trained"
    CONVERGED = "converged"


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

@dataclass
class InteractionFragment:
    """交互片段。"""
    fragment_id: str
    text: str                           # 原始文本
    task_labels: list[str] = field(default_factory=list)  # 任务标签
    tool_calls: list[str] = field(default_factory=list)    # 工具调用信号
    embedding: list[float] = field(default_factory=list)   # 嵌入向量
    importance: float = 1.0             # 重要性权重
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IntentCentroid:
    """意图中心向量。"""
    centroid_id: str
    intent_label: str                   # 意图标签 (如 "code_generation", "data_analysis")
    vector: list[float]                 # 中心向量
    cluster_size: int = 0               # 聚类大小
    variance: float = 0.0               # 簇内方差
    confidence: float = 0.0             # 中心置信度
    last_updated: float = field(default_factory=time.time)


@dataclass
class CompressedSummary:
    """压缩摘要。"""
    summary_id: str
    intent_label: str
    compressed_text: str                # 压缩后文本
    source_fragments: list[str]          # 来源片段 ID
    compression_ratio: float             # 压缩比 (output_size/input_size)
    key_entities: list[str] = field(default_factory=list)
    key_actions: list[str] = field(default_factory=list)
    centroid_distance: float = 0.0       # 到意图中心的距离
    timestamp: float = field(default_factory=time.time)


@dataclass
class IntentPrediction:
    """意图预测结果。"""
    query: str
    predicted_intent: str
    top_k_intents: list[tuple[str, float]] = field(default_factory=list)  # (intent_label, score)
    confidence: float = 0.0
    fallback_triggered: bool = False


# ══════════════════════════════════════════════════════════════════════
# 层次聚类
# ══════════════════════════════════════════════════════════════════════

class HierarchicalClustering:
    """对交互片段进行层次聚类。

    支持 Agglomerative / Divisive / Spectral 三种策略。
    生成 dendrogram 后可剪枝得到任意粒度的簇。
    """

    def __init__(self, n_clusters: int = 5, strategy: ClusterStrategy = ClusterStrategy.AGGLOMERATIVE,
                 metric: DistanceMetric = DistanceMetric.COSINE, max_cluster_size: int = 50):
        self.n_clusters = n_clusters
        self.strategy = strategy
        self.metric = metric
        self.max_cluster_size = max_cluster_size
        self._linkage_matrix: list[list[float]] | None = None  # dendrogram linkage

    def fit_predict(self, fragments: list[InteractionFragment]) -> dict[str, int]:
        """聚类并返回 fragment_id → cluster_id 映射。

        Args:
            fragments: 交互片段列表

        Returns:
            片段到簇编号的映射
        """
        if len(fragments) <= self.n_clusters:
            return {f.fragment_id: i for i, f in enumerate(fragments)}

        # 提取嵌入矩阵
        vectors = self._validate_embeddings(fragments)
        n = len(fragments)

        if self.strategy == ClusterStrategy.AGGLOMERATIVE:
            labels = self._agglomerative(vectors, n, fragments)
        elif self.strategy == ClusterStrategy.DIVISIVE:
            labels = self._divisive(vectors, n, fragments)
        else:
            labels = self._spectral(vectors, n, fragments)

        return {fragments[i].fragment_id: labels[i] for i in range(n)}

    def _validate_embeddings(self, fragments: list[InteractionFragment]) -> list[list[float]]:
        vectors = [f.embedding for f in fragments]
        if not vectors or not vectors[0]:
            # 占位：无嵌入时用基于文本长度的伪向量回退
            dim = 64
            vectors = []
            import hashlib
            for f in fragments:
                h = hashlib.sha256(f.text.encode()).digest()
                vec = [float(b) / 255.0 for b in h[:dim]]
                # 填充到 dim
                while len(vec) < dim:
                    vec.append(0.0)
                vectors.append(vec[:dim])
        return vectors

    def _distance(self, a: list[float], b: list[float]) -> float:
        a_norm = math.sqrt(sum(x * x for x in a)) or 1e-8
        b_norm = math.sqrt(sum(x * x for x in b)) or 1e-8

        if self.metric == DistanceMetric.COSINE:
            dot = sum(ai * bi for ai, bi in zip(a, b))
            return 1.0 - dot / (a_norm * b_norm)
        elif self.metric == DistanceMetric.EUCLIDEAN:
            return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b)))
        elif self.metric == DistanceMetric.MANHATTAN:
            return sum(abs(ai - bi) for ai, bi in zip(a, b))
        else:  # dot product
            return -sum(ai * bi for ai, bi in zip(a, b))

    def _agglomerative(self, vectors: list[list[float]], n: int,
                       fragments: list[InteractionFragment]) -> list[int]:
        """简化 Agglomerative 聚类。"""
        # 初始化每个点一个簇
        clusters: list[set[int]] = [{i} for i in range(n)]

        while len(clusters) > self.n_clusters:
            # 找距离最近的两个簇
            min_dist = float("inf")
            merge_pair = (0, 1)
            for i in range(len(clusters)):
                for j in range(i + 1, len(clusters)):
                    # 单个簇大小限制
                    if len(clusters[i]) + len(clusters[j]) > self.max_cluster_size:
                        continue
                    # 平均距离
                    dist = self._cluster_distance(clusters[i], clusters[j], vectors)
                    if dist < min_dist:
                        min_dist = dist
                        merge_pair = (i, j)

            i, j = merge_pair
            clusters[i] = clusters[i] | clusters[j]
            clusters.pop(j)

        # 分配标签
        labels = [0] * n
        for label, cluster in enumerate(clusters):
            for idx in cluster:
                labels[idx] = label
        return labels

    def _divisive(self, vectors: list[list[float]], n: int,
                  fragments: list[InteractionFragment]) -> list[int]:
        """简化 Divisive 聚类：改用 agg 然后映射。"""
        return self._agglomerative(vectors, n, fragments)

    def _spectral(self, vectors: list[list[float]], n: int,
                  fragments: list[InteractionFragment]) -> list[int]:
        """简化谱聚类。"""
        return self._agglomerative(vectors, n, fragments)

    def _cluster_distance(self, cluster_a: set[int], cluster_b: set[int],
                          vectors: list[list[float]]) -> float:
        """平均 linkage 距离。"""
        total = 0.0
        count = 0
        for ia in cluster_a:
            for ib in cluster_b:
                total += self._distance(vectors[ia], vectors[ib])
                count += 1
        return total / max(count, 1)

    def prune_by_importance(self, fragments: list[InteractionFragment],
                            threshold: float = 0.2) -> list[InteractionFragment]:
        """按重要性阈值剪枝无关片段。"""
        return [f for f in fragments if f.importance >= threshold]

    def get_stats(self) -> dict:
        return {
            "strategy": self.strategy.value,
            "metric": self.metric.value,
            "n_clusters": self.n_clusters,
            "max_cluster_size": self.max_cluster_size,
        }


# ══════════════════════════════════════════════════════════════════════
# 意图中心学习器
# ══════════════════════════════════════════════════════════════════════

class IntentCentroidLearner:
    """从任务标签和工具调用信号学习意图中心向量。

    支持在线更新和合并相似意图中心。
    """

    def __init__(self, dim: int = 128, lr: float = 0.05,
                 merge_threshold: float = 0.85, min_samples: int = 3):
        self.dim = dim
        self.lr = lr
        self.merge_threshold = merge_threshold
        self.min_samples = min_samples
        self._centroids: dict[str, IntentCentroid] = {}
        self._fragment_assignments: dict[str, str] = {}  # fragment_id -> centroid_id
        self._status = IntentSpaceStatus.UNTRAINED

    def learn(self, fragments: list[InteractionFragment]) -> dict[str, IntentCentroid]:
        """从片段中学习意图中心向量。

        Args:
            fragments: 交互片段列表 (需含 task_labels 或 tool_calls)

        Returns:
            学习后的意图中心向量字典
        """
        if not fragments:
            return {}

        # 按意图标签分组
        intent_groups: dict[str, list[InteractionFragment]] = defaultdict(list)
        for f in fragments:
            # 意图标签来源：task_labels 或 tool_calls 推断
            labels = self._infer_intents(f)
            for label in labels:
                intent_groups[label].append(f)

        # 为每个意图组计算中心向量
        for intent_label, group in intent_groups.items():
            if len(group) < self.min_samples:
                continue

            vectors = [f.embedding for f in group if f.embedding]
            if not vectors:
                continue

            centroid_vector = self._compute_centroid(vectors)
            variance = self._compute_variance(vectors, centroid_vector)

            centroid_id = f"intent_{intent_label}_{uuid.uuid4().hex[:6]}"
            self._centroids[centroid_id] = IntentCentroid(
                centroid_id=centroid_id,
                intent_label=intent_label,
                vector=centroid_vector,
                cluster_size=len(group),
                variance=variance,
                confidence=min(1.0, len(group) / max(self.min_samples * 3, len(fragments))),
            )
            for f in group:
                self._fragment_assignments[f.fragment_id] = centroid_id

        # 合并相似意图中心
        self._merge_similar_centroids()

        if len(self._centroids) >= self.min_samples:
            self._status = IntentSpaceStatus.CONVERGED
        elif self._centroids:
            self._status = IntentSpaceStatus.PARTIALLY_TRAINED

        return dict(self._centroids)

    def update_online(self, fragment: InteractionFragment) -> IntentCentroid | None:
        """在线更新：新片段到达时增量更新意图中心。

        Args:
            fragment: 新到达的交互片段

        Returns:
            更新的 IntentCentroid，如果无法匹配则返回 None
        """
        if not fragment.embedding:
            return None

        intents = self._infer_intents(fragment)
        if not intents:
            return None

        best_centroid = None
        best_dist = float("inf")
        for cid, centroid in self._centroids.items():
            if centroid.intent_label in intents:
                dist = 1.0 - self._cosine_similarity(fragment.embedding, centroid.vector)
                if dist < best_dist:
                    best_dist = dist
                    best_centroid = centroid

        if best_centroid is None:
            return None

        # 在线更新中心向量 (exponential moving average)
        new_vec = []
        for v_c, v_f in zip(best_centroid.vector, fragment.embedding):
            new_vec.append(v_c + self.lr * (v_f - v_c))
        best_centroid.vector = new_vec
        best_centroid.cluster_size += 1
        best_centroid.last_updated = time.time()
        self._fragment_assignments[fragment.fragment_id] = best_centroid.centroid_id

        return best_centroid

    def predict_intent(self, query_embedding: list[float],
                       top_k: int = 3) -> list[tuple[str, float]]:
        """预测查询的意图分布。

        Args:
            query_embedding: 查询的嵌入向量
            top_k: 返回 top-k 意图

        Returns:
            [(intent_label, similarity_score), ...]
        """
        if not self._centroids or not query_embedding:
            return [("general", 1.0)]

        scored = []
        for centroid in self._centroids.values():
            sim = self._cosine_similarity(query_embedding, centroid.vector)
            scored.append((centroid.intent_label, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def _infer_intents(self, fragment: InteractionFragment) -> list[str]:
        """从任务标签和工具调用推断意图。"""
        intents = list(fragment.task_labels)

        # 从工具调用推断补充意图
        tool_to_intent = {
            "read_file": "document_reading",
            "write_file": "document_writing",
            "shell_executor": "system_operation",
            "python_executor": "code_execution",
            "search_file": "file_search",
            "ai_search": "web_search",
            "analyze_image": "image_analysis",
            "convert_file": "format_conversion",
            "delete": "file_management",
            "use_skill": "skill_execution",
        }
        for call in fragment.tool_calls:
            for prefix, intent in tool_to_intent.items():
                if call.startswith(prefix):
                    if intent not in intents:
                        intents.append(intent)
                    break

        return intents if intents else ["general"]

    def _compute_centroid(self, vectors: list[list[float]]) -> list[float]:
        dim = len(vectors[0])
        centroid = [0.0] * dim
        for v in vectors:
            for i in range(dim):
                centroid[i] += v[i]
        n = len(vectors)
        return [c / n for c in centroid]

    def _compute_variance(self, vectors: list[list[float]],
                          centroid: list[float]) -> float:
        total = 0.0
        for v in vectors:
            dist = self._cosine_similarity(v, centroid)
            total += (1.0 - dist) ** 2
        return total / max(len(vectors), 1)

    def _merge_similar_centroids(self) -> None:
        """合并相似意图中心。"""
        cids = list(self._centroids.keys())
        merged: set[str] = set()

        for i in range(len(cids)):
            if cids[i] in merged:
                continue
            for j in range(i + 1, len(cids)):
                if cids[j] in merged:
                    continue
                sim = self._cosine_similarity(
                    self._centroids[cids[i]].vector,
                    self._centroids[cids[j]].vector,
                )
                if sim > self.merge_threshold:
                    # 合并到较大的簇
                    ca = self._centroids[cids[i]]
                    cb = self._centroids[cids[j]]
                    if ca.cluster_size >= cb.cluster_size:
                        self._merge_into(ca, cb)
                        merged.add(cids[j])
                    else:
                        self._merge_into(cb, ca)
                        merged.add(cids[i])

        for cid in merged:
            del self._centroids[cid]

    def _merge_into(self, target: IntentCentroid, source: IntentCentroid) -> None:
        total = target.cluster_size + source.cluster_size
        alpha = target.cluster_size / total
        beta = source.cluster_size / total
        target.vector = [alpha * a + beta * b for a, b in zip(target.vector, source.vector)]
        target.cluster_size = total
        target.variance = (target.variance + source.variance) / 2

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(ai * bi for ai, bi in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a)) or 1e-8
        norm_b = math.sqrt(sum(x * x for x in b)) or 1e-8
        return max(0.0, min(1.0, dot / (norm_a * norm_b)))

    def get_centroids(self) -> dict[str, IntentCentroid]:
        return dict(self._centroids)

    def get_stats(self) -> dict:
        return {
            "n_centroids": len(self._centroids),
            "status": self._status.value,
            "dim": self.dim,
            "total_assigned_fragments": len(self._fragment_assignments),
        }


# ══════════════════════════════════════════════════════════════════════
# 意图感知检索器
# ══════════════════════════════════════════════════════════════════════

class IntentAwareRetriever:
    """意图感知检索器 — 预测查询意图后检索对齐的压缩摘要。"""

    def __init__(self, learner: IntentCentroidLearner,
                 default_embedding_dim: int = 128):
        self.learner = learner
        self.default_dim = default_embedding_dim
        self._summaries: list[CompressedSummary] = []
        self._summary_embeddings: dict[str, list[float]] = {}  # summary_id -> embedding

    def index_summaries(self, summaries: list[CompressedSummary],
                        embeddings: dict[str, list[float]] | None = None) -> None:
        """索引压缩摘要及其嵌入。"""
        self._summaries = summaries
        if embeddings:
            self._summary_embeddings = embeddings

    def retrieve(self, query: str, query_embedding: list[float] | None = None,
                 top_k: int = 5, intent_k: int = 3) -> tuple[IntentPrediction, list[CompressedSummary]]:
        """意图感知检索。

        Args:
            query: 查询文本
            query_embedding: 查询嵌入向量 (可选)
            top_k: 返回摘要数
            intent_k: 意图候选数

        Returns:
            (IntentPrediction, 排序后的压缩摘要列表)
        """
        # 意图预测
        if query_embedding is None:
            query_embedding = self._placeholder_embedding(query)

        intent_scores = self.learner.predict_intent(query_embedding, top_k=intent_k)

        if not intent_scores or intent_scores[0][1] < 0.3:
            # 回退：无明确意图时返回全部
            prediction = IntentPrediction(
                query=query,
                predicted_intent="general",
                top_k_intents=[("general", 1.0)],
                confidence=0.0,
                fallback_triggered=True,
            )
            return prediction, self._summaries[:top_k]

        predicted_intent = intent_scores[0][0]
        prediction = IntentPrediction(
            query=query,
            predicted_intent=predicted_intent,
            top_k_intents=intent_scores,
            confidence=intent_scores[0][1],
        )

        # 按意图过滤和排序摘要
        scored = []
        for summary in self._summaries:
            intent_match = int(summary.intent_label == predicted_intent)
            # 摘要与查询的语义距离
            summary_emb = self._summary_embeddings.get(summary.summary_id, [])
            semantic_score = 0.5  # 默认
            if summary_emb and query_embedding:
                semantic_score = IntentCentroidLearner._cosine_similarity(
                    query_embedding, summary_emb
                )
            # 综合得分：意图匹配优先，语义其次
            total_score = intent_match * 0.6 + semantic_score * 0.4
            scored.append((summary, total_score))

        scored.sort(key=lambda x: x[1], reverse=True)
        results = [s for s, _ in scored[:top_k]]

        return prediction, results

    def _placeholder_embedding(self, text: str) -> list[float]:
        """占位嵌入生成 (生产环境应替换为真实 encoder)。"""
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        vec = [float(b) / 255.0 for b in h[:self.default_dim]]
        while len(vec) < self.default_dim:
            vec.append(0.0)
        return vec[:self.default_dim]

    def get_stats(self) -> dict:
        return {
            "n_summaries": len(self._summaries),
            "n_summary_embeddings": len(self._summary_embeddings),
            "learner_status": self.learner.get_stats(),
        }


# ══════════════════════════════════════════════════════════════════════
# 模块自测
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import hashlib

    print("=" * 60)
    print("Intent-Aware Compression — Self Test")
    print("=" * 60)

    # 构造交互片段
    def make_vec(text: str, dim: int = 128) -> list[float]:
        h = hashlib.sha256(text.encode()).digest()
        vec = [float(b) / 255.0 for b in h[:dim]]
        while len(vec) < dim:
            vec.append(0.0)
        return vec[:dim]

    fragments = [
        InteractionFragment("f1", "Read the CSV file and parse headers",
                            task_labels=["data_analysis"], tool_calls=["read_file"],
                            embedding=make_vec("csv headers"), importance=0.9),
        InteractionFragment("f2", "Generate a bar chart from the DataFrame",
                            task_labels=["data_analysis"], tool_calls=["python_executor"],
                            embedding=make_vec("bar chart dataframe"), importance=0.85),
        InteractionFragment("f3", "Write a FastAPI endpoint for the API",
                            task_labels=["code_generation"], tool_calls=["write_file"],
                            embedding=make_vec("fastapi endpoint api"), importance=0.8),
        InteractionFragment("f4", "Dockerize the application with Dockerfile",
                            task_labels=["code_generation"], tool_calls=["shell_executor"],
                            embedding=make_vec("dockerize dockerfile"), importance=0.75),
        InteractionFragment("f5", "Search for relevant papers on memory",
                            task_labels=["web_search"], tool_calls=["ai_search"],
                            embedding=make_vec("papers memory"), importance=0.7),
        InteractionFragment("f6", "Delete temporary log files",
                            task_labels=["file_management"], tool_calls=["delete"],
                            embedding=make_vec("temp log files"), importance=0.5),
    ]

    # 层次聚类
    hc = HierarchicalClustering(n_clusters=3, strategy=ClusterStrategy.AGGLOMERATIVE)
    labels = hc.fit_predict(fragments)
    print(f"\n[Hierarchical Clustering] {len(labels)} fragments -> {len(set(labels.values()))} clusters")
    for fid, cid in labels.items():
        print(f"  {fid}: cluster={cid}")

    # 意图中心学习
    learner = IntentCentroidLearner(dim=128, min_samples=1)
    centroids = learner.learn(fragments)
    print(f"\n[Intent Centroids] {len(centroids)} learned")
    for cid, c in centroids.items():
        print(f"  {c.intent_label}: size={c.cluster_size}, conf={c.confidence:.2f}")

    # 在线更新
    new_frag = InteractionFragment("f7", "Create a PowerPoint presentation",
                                   task_labels=["document_writing"],
                                   embedding=make_vec("powerpoint presentation"))
    updated = learner.update_online(new_frag)
    if updated:
        print(f"\n[Online Update] {updated.intent_label}: size={updated.cluster_size}")

    # 意图预测
    query_vec = make_vec("generate chart from data")
    predictions = learner.predict_intent(query_vec, top_k=3)
    print(f"\n[Intent Prediction]")
    for label, score in predictions:
        print(f"  {label}: {score:.4f}")

    # 意图感知检索
    summaries = [
        CompressedSummary("s1", "data_analysis", "Parsed CSV with 1000 rows, generated bar chart",
                          ["f1", "f2"], 0.4, key_entities=["CSV", "DataFrame"]),
        CompressedSummary("s2", "code_generation", "Created FastAPI endpoint for /api/v1/data",
                          ["f3", "f4"], 0.35, key_entities=["FastAPI", "API"]),
        CompressedSummary("s3", "web_search", "Found 3 papers on memory-augmented LLM agents",
                          ["f5"], 0.3, key_entities=["LLM", "memory"]),
    ]
    sum_emb = {
        "s1": make_vec("csv bar chart dataframe parsed"),
        "s2": make_vec("fastapi endpoint api created"),
        "s3": make_vec("papers memory augmented llm agents found"),
    }

    retriever = IntentAwareRetriever(learner)
    retriever.index_summaries(summaries, sum_emb)

    prediction, results = retriever.retrieve("build a data visualization", query_vec, top_k=3)
    print(f"\n[Retrieval] intent={prediction.predicted_intent}, fallback={prediction.fallback_triggered}")
    for s in results:
        print(f"  [{s.intent_label}] {s.compressed_text}")
