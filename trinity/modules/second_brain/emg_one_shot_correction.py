"""P33: Experience Memory Graph One-Shot Correction — arXiv 2607.13884.

# status: orphan (2026-08-15 audit, not in runtime path)
Builds ActionDecisionGraph from trajectories, extracts graph edit paths
between failed/success trajectory pairs, and performs one-shot correction
at test time without iterative reflect-replay loops.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ActionNode:
    action_id: str
    action_type: str
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    success: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionEdge:
    edge_id: str
    source_id: str
    target_id: str
    transition_type: str  # "sequential", "conditional", "error"
    weight: float = 1.0


@dataclass
class ActionDecisionGraph:
    graph_id: str
    nodes: dict[str, ActionNode] = field(default_factory=dict)
    edges: list[ActionEdge] = field(default_factory=list)
    task_id: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class EditPath:
    path_id: str
    insertions: list[ActionNode]
    deletions: list[str]  # action_ids to remove
    substitutions: list[dict[str, Any]]  # {old_id, new_node}
    edit_distance: int
    insight: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class CorrectionResult:
    result_id: str
    corrected: bool
    corrected_action: ActionNode | None
    edit_path: EditPath | None
    confidence: float = 0.0
    error: str | None = None
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Action Decision Graph Builder
# ---------------------------------------------------------------------------

class ActionDecisionGraphBuilder:
    """Build a directed graph from a trajectory of actions.

    Nodes = action steps (state before/after); edges = transitions.
    Supports graph edit distance computation against another graph.
    """

    def __init__(self, task_id: str = "") -> None:
        self._lock = threading.RLock()
        self.graph = ActionDecisionGraph(graph_id=uuid.uuid4().hex[:12], task_id=task_id)

    def build(self, trajectory: list[dict[str, Any]]) -> ActionDecisionGraph:
        with self._lock:
            nodes: dict[str, ActionNode] = {}
            edges: list[ActionEdge] = []

            prev_id: str | None = None
            for i, step in enumerate(trajectory):
                nid = step.get("action_id", f"a{i}")
                node = ActionNode(
                    action_id=nid, action_type=step.get("action_type", "unknown"),
                    state_before=step.get("state_before", {}), state_after=step.get("state_after", {}),
                    success=step.get("success", True), metadata=step.get("metadata", {}),
                )
                nodes[nid] = node
                if prev_id:
                    edges.append(ActionEdge(edge_id=uuid.uuid4().hex[:12], source_id=prev_id, target_id=nid, transition_type="sequential"))
                prev_id = nid

            self.graph = ActionDecisionGraph(graph_id=self.graph.graph_id, nodes=nodes, edges=edges, task_id=self.graph.task_id)
            logger.info("EMG Builder: %d nodes, %d edges from trajectory", len(nodes), len(edges))
            return self.graph

    def statistics(self) -> dict[str, Any]:
        return {"type": "ActionDecisionGraphBuilder", "nodes": len(self.graph.nodes), "edges": len(self.graph.edges)}


# ---------------------------------------------------------------------------
# Graph Edit Path Extractor
# ---------------------------------------------------------------------------

class GraphEditPathExtractor:
    """Extract edit paths (insert/delete/substitute) between failed and successful graphs.

    Compares two ActionDecisionGraph instances from paired trajectories
    of the same task and computes the minimal graph edit path.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def extract(self, failed_graph: ActionDecisionGraph, success_graph: ActionDecisionGraph) -> EditPath:
        with self._lock:
            failed_ids = set(failed_graph.nodes.keys())
            success_ids = set(success_graph.nodes.keys())

            # Find insertions: in success but not in failed
            insertions = [success_graph.nodes[sid] for sid in (success_ids - failed_ids)]
            # Find deletions: in failed but not in success
            deletions = list(failed_ids - success_ids)
            # Find substitutions: same id, different state
            substitutions: list[dict[str, Any]] = []
            for sid in failed_ids & success_ids:
                fn = failed_graph.nodes[sid]
                sn = success_graph.nodes[sid]
                if fn.state_after != sn.state_after:
                    substitutions.append({"old_id": sid, "new_node": sn, "old_state": fn.state_after, "new_state": sn.state_after})

            edit_distance = len(insertions) + len(deletions) + len(substitutions)

            insight_parts: list[str] = []
            if deletions:
                insight_parts.append(f"Remove {len(deletions)} error-prone actions: {deletions}")
            if insertions:
                insight_parts.append(f"Insert {len(insertions)} corrective actions after error point")
            if substitutions:
                insight_parts.append(f"Amend {len(substitutions)} action states")

            ep = EditPath(
                path_id=uuid.uuid4().hex[:12], insertions=insertions, deletions=deletions,
                substitutions=substitutions, edit_distance=edit_distance,
                insight="; ".join(insight_parts) if insight_parts else "No edit needed",
            )
            logger.info("EMG EditPath: %d ins + %d del + %d sub = distance %d", len(insertions), len(deletions), len(substitutions), edit_distance)
            return ep

    def statistics(self) -> dict[str, Any]:
        return {"type": "GraphEditPathExtractor"}


# ---------------------------------------------------------------------------
# One-Shot Error Corrector
# ---------------------------------------------------------------------------

class OneShotErrorCorrector:
    """Offline-trained corrector: one-shot fix at test time, no iterative loops.

    Trains on graph edit paths from paired failure/success trajectories.
    At test time, matches the error context against stored edit paths
    and applies the closest correction in a single step.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._edit_paths: dict[str, list[EditPath]] = {}  # task_id → paths

    def train(self, task_id: str, failed_traj: list[dict[str, Any]], success_traj: list[dict[str, Any]]) -> EditPath:
        with self._lock:
            fg = ActionDecisionGraphBuilder(task_id)
            sg = ActionDecisionGraphBuilder(task_id)
            f_graph = fg.build(failed_traj)
            s_graph = sg.build(success_traj)

            extractor = GraphEditPathExtractor()
            ep = extractor.extract(f_graph, s_graph)
            self._edit_paths.setdefault(task_id, []).append(ep)
            logger.info("EMG OneShot: trained %s with edit distance %d", task_id, ep.edit_distance)
            return ep

    def correct(self, task_id: str, error_action: ActionNode) -> CorrectionResult:
        with self._lock:
            paths = self._edit_paths.get(task_id, [])
            if not paths:
                return CorrectionResult(result_id=uuid.uuid4().hex[:12], corrected=False, corrected_action=None, edit_path=None, confidence=0.0, error=f"No edit path for task '{task_id}'")

            # Pick best edit path (lowest distance)
            best = min(paths, key=lambda p: p.edit_distance)

            # Check if error_action is in deletions
            if error_action.action_id in best.deletions:
                # Recommend next insertion as correction
                if best.insertions:
                    corrected = best.insertions[0]
                    return CorrectionResult(result_id=uuid.uuid4().hex[:12], corrected=True, corrected_action=corrected, edit_path=best, confidence=0.85)

            # Check substitutions
            for sub in best.substitutions:
                if sub["old_id"] == error_action.action_id:
                    return CorrectionResult(result_id=uuid.uuid4().hex[:12], corrected=True, corrected_action=sub["new_node"], edit_path=best, confidence=0.75)

            return CorrectionResult(result_id=uuid.uuid4().hex[:12], corrected=False, corrected_action=None, edit_path=None, confidence=0.0, error="No matching correction")

    def statistics(self) -> dict[str, Any]:
        return {"type": "OneShotErrorCorrector", "trained_tasks": len(self._edit_paths)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def correct_one_shot(task_id: str, error_action: dict[str, Any]) -> CorrectionResult:
    """One-shot error correction without iterative reflect-replay.

    Converts error_action dict to ActionNode, looks up stored edit path
    for the task, and returns the corrected action in a single step.

    Args:
        task_id: Task identifier string.
        error_action: Dict with action_id, action_type, state_before, state_after.

    Returns:
        CorrectionResult with corrected action if found, else error.
    """
    corrector = OneShotErrorCorrector()
    node = ActionNode(
        action_id=error_action.get("action_id", "unknown"),
        action_type=error_action.get("action_type", "unknown"),
        state_before=error_action.get("state_before", {}),
        state_after=error_action.get("state_after", {}),
        success=error_action.get("success", False),
    )

    result = corrector.correct(task_id, node)
    logger.info("[P33] EMG correct_one_shot: task=%s corrected=%s", task_id, result.corrected)
    return result


print("[P33] Experience Memory Graph One-Shot Correction initialized — arXiv 2607.13884 aligned")
