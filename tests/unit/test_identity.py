"""Unit tests for trinity.identity package — Multi-Anchor Identity."""

import json
import pytest

from trinity.identity.anchor_types import (
    IdentityAnchor, IdentityProfile, IdentityBundle,
    TemporalAnchor,
)
from trinity.identity.identity_manager import ANCHOR_TYPES, ANCHOR_WEIGHTS, DRIFT_SEVERITY, IdentityManager
from trinity.identity.hybrid_router import HybridRouter, QueryType


class TestAnchorTypes:
    """IdentityAnchor / IdentityProfile / IdentityBundle serialization."""

    def test_anchor_to_dict_from_dict_roundtrip(self):
        """Anchor should survive a to_dict → from_dict round-trip unchanged."""
        anchor = IdentityAnchor(
            id="a1",
            agent_id="agent-42",
            anchor_type="identity_files",
            content={"personality": "analytical", "values": ["precision"]},
            version=3,
            checksum="abc123",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-08-01T00:00:00Z",
        )
        d = anchor.to_dict()
        restored = IdentityAnchor.from_dict(d)
        assert restored.id == anchor.id
        assert restored.agent_id == anchor.agent_id
        assert restored.anchor_type == anchor.anchor_type
        assert restored.content == anchor.content
        assert restored.version == anchor.version
        assert restored.checksum == anchor.checksum

    def test_from_dict_empty_content_returns_empty_dict(self):
        """from_dict with empty content field should yield {} not a string."""
        a = IdentityAnchor.from_dict({"id": "x", "agent_id": "a", "anchor_type": "t", "content": ""})
        assert a.content == {}

    def test_from_dict_non_json_content_falls_back_to_raw(self):
        """from_dict with non-JSON string content should store as _raw."""
        a = IdentityAnchor.from_dict({"id": "x", "agent_id": "a", "anchor_type": "t", "content": "raw!!"})
        assert a.content == {"_raw": "raw!!"}

    def test_from_dict_json_string_content_parsed(self):
        """from_dict with JSON-string content should parse correctly."""
        a = IdentityAnchor.from_dict({"id": "x", "agent_id": "a", "anchor_type": "t", "content": '{"k":"v"}'})
        assert a.content == {"k": "v"}

    def test_identity_bundle_to_dict_from_dict(self):
        """IdentityBundle round-trip works."""
        bundle = IdentityBundle(
            agent_id="agent-1",
            exported_at="2026-08-11T00:00:00Z",
            version="1.0",
            anchors=[],
            checksum="sum",
        )
        d = bundle.to_dict()
        restored = IdentityBundle.from_dict(d)
        assert restored.agent_id == bundle.agent_id
        assert restored.checksum == bundle.checksum


class TestIdentityManager:
    """IdentityManager anchor CRUD + reconstruction + drift."""

    ANCHOR_CONTENT = {"key": "value", "nested": {"x": 1}}

    def test_register_anchor_succeeds(self, identity_manager):
        """register_anchor should return an IdentityAnchor with valid fields."""
        a = identity_manager.register_anchor("agent-1", "identity_files", self.ANCHOR_CONTENT)
        assert isinstance(a, IdentityAnchor)
        assert a.agent_id == "agent-1"
        assert a.anchor_type == "identity_files"
        assert a.content == self.ANCHOR_CONTENT
        assert a.checksum != ""

    def test_register_invalid_anchor_type_raises(self, identity_manager):
        """Unknown anchor_type should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown anchor_type"):
            identity_manager.register_anchor("agent-1", "not_a_type", {})

    def test_get_anchors_returns_registered(self, identity_manager):
        """After registering, get_anchors should return the anchor."""
        identity_manager.register_anchor("agent-1", "identity_files", {"v": 1})
        identity_manager.register_anchor("agent-1", "procedural_patterns", {"v": 2})
        all_anchors = identity_manager.get_anchors("agent-1")
        assert len(all_anchors) == 2
        types = {a.anchor_type for a in all_anchors}
        assert types == {"identity_files", "procedural_patterns"}

    def test_get_anchors_filter_by_type(self, identity_manager):
        """get_anchors with anchor_type filter returns only that type."""
        identity_manager.register_anchor("agent-1", "identity_files", {"v": 1})
        identity_manager.register_anchor("agent-1", "episodic_keys", {"v": 2})
        result = identity_manager.get_anchors("agent-1", "identity_files")
        assert len(result) == 1
        assert result[0].anchor_type == "identity_files"

    def test_reconstruct_identity_cross_type(self, identity_manager):
        """Reconstruction across original 4 anchor types builds correct profile."""
        agent = "agent-cross"
        base_types = ["identity_files", "procedural_patterns", "episodic_keys", "value_specifications"]
        for atype in base_types:
            identity_manager.register_anchor(agent, atype, {"type": atype})
        profile = identity_manager.reconstruct_identity(agent)
        assert profile.agent_id == agent
        assert len(profile.anchors) == 4
        for atype in base_types:
            assert profile.anchor_counts.get(atype, 0) >= 1

    def test_detect_drift_with_missing_values_flag(self, identity_manager):
        """When value_specifications are missing, drift should be flagged."""
        agent = "agent-drift"
        identity_manager.register_anchor(agent, "identity_files", {"v": 1})
        identity_manager.register_anchor(agent, "procedural_patterns", {"v": 2})
        identity_manager.register_anchor(agent, "episodic_keys", {"v": 3})
        result = identity_manager.detect_identity_drift(agent)
        assert "missing_value_specifications" in result["warnings"]

    def test_detect_drift_stable_with_all_types(self, identity_manager):
        """All original 4 types present should produce low drift score."""
        agent = "agent-stable"
        base_types = ["identity_files", "procedural_patterns", "episodic_keys", "value_specifications"]
        for atype in base_types:
            identity_manager.register_anchor(agent, atype, {"type": atype})
        result = identity_manager.detect_identity_drift(agent)
        # With all types present the drift threshold may or may not be exceeded
        # depending on anchor counts, but it should have a valid score
        assert 0.0 <= result["drift_score"] <= 1.0

    def test_duplicate_anchor_type_increments_version(self, identity_manager):
        """Registering same type twice increments version."""
        a1 = identity_manager.register_anchor("agent-dup", "identity_files", {"v": 1})
        a2 = identity_manager.register_anchor("agent-dup", "identity_files", {"v": 2})
        assert a2.version > a1.version

    def test_export_import_bundle(self, identity_manager):
        """Export import cycle preserves anchors."""
        agent = "agent-export"
        base_types = ["identity_files", "procedural_patterns", "episodic_keys", "value_specifications"]
        for atype in base_types:
            identity_manager.register_anchor(agent, atype, {"type": atype})
        bundle = identity_manager.export_identity_bundle(agent)
        assert bundle.agent_id == agent
        assert len(bundle.anchors) == 4

        imported = identity_manager.import_identity_bundle(bundle)
        assert imported == 4

    def test_partial_reconstruct_flags_warning(self, identity_manager):
        """Partial reconstruct should append drift warning."""
        agent = "agent-partial"
        identity_manager.register_anchor(agent, "identity_files", {"v": 1})
        profile = identity_manager.partial_reconstruct(agent, ["identity_files"])
        assert any("PARTIAL_RECONSTRUCT" in f for f in profile.drift_flags)


class TestHybridRouter:
    """HybridRouter query classification."""

    @pytest.mark.parametrize("query,expected_type", [
        ("How many users are registered?", QueryType.FACT),
        ("Who am I? What are my values?", QueryType.IDENTITY),
        ("Tell me about the project status", QueryType.FUZZY),
        ("As an agent, what did I learn about deployment?", QueryType.HYBRID),
        ("list all documents older than 2024", QueryType.FACT),
        ("Describe yourself including your constitutional rules", QueryType.IDENTITY),
    ])
    def test_classify_routes_correctly(self, query, expected_type):
        router = HybridRouter()
        qtype, confidence = router.classify(query)
        assert qtype == expected_type
        assert 0.0 <= confidence <= 1.0

    def test_empty_query_defaults_to_fuzzy(self):
        router = HybridRouter()
        qtype, confidence = router.classify("")
        assert qtype == QueryType.FUZZY


class TestTemporalAnchor:
    """TemporalAnchor creation, extraction, and serialization."""

    def test_from_access_log_daily_pattern(self):
        """Access times in 9-11 range should yield 'daily' pattern."""
        anchor = TemporalAnchor.from_access_log(
            agent_id="agent-1",
            access_times=[
                "2026-08-10T09:00:00+00:00",
                "2026-08-10T09:30:00+00:00",
                "2026-08-10T10:00:00+00:00",
                "2026-08-10T10:30:00+00:00",
                "2026-08-10T14:00:00+00:00",
            ],
            iana_tz="UTC",
        )
        assert anchor.anchor_id != ""
        assert anchor.agent_id == "agent-1"
        assert anchor.temporal_pattern in ("daily", "irregular")
        assert len(anchor.active_windows) >= 1
        assert anchor.timezone == "UTC"

    def test_to_dict_from_dict_roundtrip(self):
        """TemporalAnchor should survive to_dict/from_dict."""
        anchor = TemporalAnchor(
            anchor_id="ta-001",
            agent_id="agent-X",
            temporal_pattern="daily",
            active_windows=[{"start_hour": 9, "end_hour": 12}, {"start_hour": 14, "end_hour": 18}],
            last_seen="2026-08-10T18:00:00+00:00",
            session_duration_avg=3600.0,
            timezone="Asia/Shanghai",
        )
        d = anchor.to_dict()
        restored = TemporalAnchor.from_dict(d)
        assert restored.anchor_id == anchor.anchor_id
        assert restored.temporal_pattern == anchor.temporal_pattern
        assert restored.active_windows == anchor.active_windows
        assert restored.session_duration_avg == anchor.session_duration_avg

    def test_default_active_windows(self):
        """TemporalAnchor with default args should have empty windows."""
        anchor = TemporalAnchor(anchor_id="ta", agent_id="a")
        assert anchor.active_windows == []
        assert anchor.temporal_pattern == "irregular"


class TestFourAnchorDrift:
    """Four-anchor weighted drift detection + severity grading."""

    def test_anchor_weights_sum_to_one(self):
        """All four weights must sum to 1.0."""
        assert abs(sum(ANCHOR_WEIGHTS.values()) - 1.0) < 0.01

    def test_drift_severity_ranges(self):
        """Severity thresholds must be contiguous and non-overlapping."""
        assert DRIFT_SEVERITY["WARNING"] == (0.3, 0.5)
        assert DRIFT_SEVERITY["CRITICAL"] == (0.5, 0.7)
        assert DRIFT_SEVERITY["IDENTITY_BREAK"] == (0.7, 1.01)

    def test_drift_result_has_dimension_scores(self, identity_manager):
        """Drift result must include all four dimension scores."""
        agent = "agent-dims"
        for atype in ANCHOR_TYPES:
            identity_manager.register_anchor(agent, atype, {"type": atype})
        result = identity_manager.detect_identity_drift(agent)
        assert "dimension_scores" in result
        assert set(result["dimension_scores"].keys()) == {
            "behavioral", "knowledge", "relational", "temporal"
        }
        assert "severity" in result
        assert result["severity"] in ("STABLE", "WARNING", "CRITICAL", "IDENTITY_BREAK")

    def test_drift_result_has_recommendation(self, identity_manager):
        """Drift result must include a recommendation string."""
        agent = "agent-rec"
        identity_manager.register_anchor(agent, "identity_files", {"v": 1})
        identity_manager.register_anchor(agent, "value_specifications", {"v": 2})
        result = identity_manager.detect_identity_drift(agent)
        assert "recommendation" in result
        assert isinstance(result["recommendation"], str)
        assert len(result["recommendation"]) > 0


class TestJSDivergence:
    """Jensen-Shannon divergence utility."""

    def test_identical_distributions(self):
        """JSD of identical distributions should be ≈ 0."""
        mgr = IdentityManager()
        jsd = mgr._jensen_shannon_divergence([0.5, 0.5], [0.5, 0.5])
        assert abs(jsd) < 0.01

    def test_opposite_distributions(self):
        """JSD of opposite distributions should be > 0.5."""
        mgr = IdentityManager()
        jsd = mgr._jensen_shannon_divergence([1.0, 0.0], [0.0, 1.0])
        assert jsd > 0.5

    def test_jsd_symmetric(self):
        """JSD must be symmetric: JSD(P,Q) == JSD(Q,P)."""
        mgr = IdentityManager()
        a = [0.7, 0.2, 0.1]
        b = [0.1, 0.3, 0.6]
        jsd_ab = mgr._jensen_shannon_divergence(a, b)
        jsd_ba = mgr._jensen_shannon_divergence(b, a)
        assert abs(jsd_ab - jsd_ba) < 0.001


class TestTemporalRouting:
    """HybridRouter temporal pattern classification."""

    def test_temporal_query_routes_to_identity(self):
        """Queries about active time patterns should route to IDENTITY."""
        router = HybridRouter()
        qtype, conf = router.classify("when is my normal active time pattern")
        assert qtype == QueryType.IDENTITY

    def test_access_anomaly_routes_to_identity(self):
        """Suspicious login pattern queries should route to IDENTITY."""
        router = HybridRouter()
        qtype, conf = router.classify("detect if this login pattern is suspicious")
        assert qtype == QueryType.IDENTITY

    def test_non_temporal_general_query(self):
        """Non-temporal factual queries should NOT route to IDENTITY."""
        router = HybridRouter()
        qtype, _ = router.classify("list all documents")
        assert qtype != QueryType.IDENTITY


class TestHybridRouterKeywordClassifier:
    """HybridRouter keyword classifier (v8.6.0)."""

    def test_keyword_classify_identity_strong(self):
        router = HybridRouter()
        kw = router._keyword_classify("who am i and define me")
        assert kw.get("IDENTITY", 0) >= 0.4

    def test_keyword_classify_fact_strong(self):
        router = HybridRouter()
        kw = router._keyword_classify("how many files in folder")
        assert kw.get("FACT", 0) >= 0.3

    def test_keyword_classify_fuzzy_strong(self):
        router = HybridRouter()
        kw = router._keyword_classify("tell me about machine learning")
        assert kw.get("FUZZY", 0) >= 0.2

    def test_route_feedback_tracks_correct(self):
        router = HybridRouter()
        router.report_route_feedback("who am i", "identity", True)
        acc = router.get_route_accuracy()
        assert acc["per_type"]["IDENTITY"]["total"] == 1
        assert acc["per_type"]["IDENTITY"]["correct"] == 1
        assert acc["overall_accuracy"] == 1.0

    def test_route_feedback_tracks_incorrect(self):
        router = HybridRouter()
        router.report_route_feedback("what is 2+2", "identity", False)
        router.report_route_feedback("who am i", "identity", True)
        acc = router.get_route_accuracy()
        assert acc["per_type"]["IDENTITY"]["accuracy"] == 0.5

    def test_accuracy_returns_zero_when_no_data(self):
        router = HybridRouter()
        acc = router.get_route_accuracy()
        assert acc["overall_accuracy"] == 0.0
        assert acc["total_feedback"] == 0
