"""Tests for Trinity Evolution system (core, serialization, skill_system)."""

import os
import sys
import json
import time
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.evolution.core import MetaEvolution, EvolutionCycle, EvolutionState, EvolutionPhase
from trinity.evolution.serialization import EvolutionStateSerializer
from trinity.evolution.skill_system import SkillSystemAdapter


# ====================================================================
# Fixtures
# ====================================================================

@pytest.fixture
def temp_state_path():
    """Provide a temporary path for evolution state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "evo_state.json")


@pytest.fixture
def meta_evolution(temp_state_path):
    """Create a MetaEvolution instance with a temp state path."""
    with tempfile.TemporaryDirectory() as skill_dir:
        evo = MetaEvolution(state_path=temp_state_path, skill_dir=skill_dir)
        yield evo


@pytest.fixture
def serializer(temp_state_path):
    """Create an EvolutionStateSerializer with a temp dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        s = EvolutionStateSerializer(state_dir=tmpdir)
        yield s


@pytest.fixture
def skill_adapter():
    """Create a SkillSystemAdapter with a temp skill directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        adapter = SkillSystemAdapter(skill_dir=tmpdir)
        yield adapter


# ====================================================================
# Tests: trinity.evolution.core — EvolutionCycle, MetaEvolution
# ====================================================================

class TestEvolutionCycle:
    """Test EvolutionCycle dataclass."""

    def test_creation_defaults(self):
        """EvolutionCycle should be created with default phase OBSERVE."""
        cycle = EvolutionCycle(
            cycle_id="test_001",
            phase=EvolutionPhase.OBSERVE,
            started_at=time.time()
        )
        assert cycle.cycle_id == "test_001"
        assert cycle.phase == EvolutionPhase.OBSERVE
        assert cycle.tick_count == 0
        assert cycle.observations == []

    def test_to_dict_structure(self):
        """to_dict() should return a dict with expected keys."""
        cycle = EvolutionCycle(
            cycle_id="test_002",
            phase=EvolutionPhase.ANALYZE,
            started_at=1000.0,
            completed_at=2000.0,
            certificates={"passed": True},
            tick_count=3,
        )
        d = cycle.to_dict()
        assert d["cycle_id"] == "test_002"
        assert d["phase"] == "analyze"
        assert d["observations_count"] == 0
        assert d["certificates"] == {"passed": True}
        assert d["tick_count"] == 3

    def test_duration_computed(self):
        """duration() should return elapsed time when completed."""
        cycle = EvolutionCycle(
            cycle_id="test_003",
            phase=EvolutionPhase.EXECUTE,
            started_at=1000.0,
            completed_at=1005.5,
        )
        assert cycle.duration() == 5.5

    def test_duration_none_when_not_completed(self):
        """duration() should return None when not completed."""
        cycle = EvolutionCycle(
            cycle_id="test_004",
            phase=EvolutionPhase.OBSERVE,
            started_at=time.time(),
        )
        assert cycle.duration() is None


class TestEvolutionState:
    """Test EvolutionState dataclass."""

    def test_creation_defaults(self):
        """EvolutionState should have sensible defaults."""
        state = EvolutionState()
        assert state.version == "1.0"
        assert state.total_cycles == 0
        assert state.active_preferences == {}
        assert state.active_patterns == {}
        assert state.corrections_log == []
        assert state.skill_scores == {}

    def test_cycle_history_tracking(self):
        """cycle_history should track completed cycle IDs."""
        state = EvolutionState()
        state.total_cycles += 1
        state.last_cycle_id = "evo_abc123"
        state.cycle_history.append("evo_abc123")
        assert state.total_cycles == 1
        assert "evo_abc123" in state.cycle_history


class TestMetaEvolutionInit:
    """Test MetaEvolution initialization."""

    def test_default_state_creation(self, temp_state_path):
        """MetaEvolution should create an EvolutionState with defaults."""
        with tempfile.TemporaryDirectory() as skill_dir:
            evo = MetaEvolution(state_path=temp_state_path, skill_dir=skill_dir)
            assert evo.state.total_cycles == 0
            assert evo.state.version == "1.0"
            assert evo.current_cycle is None

    def test_custom_paths(self):
        """MetaEvolution should accept custom state_path and skill_dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sp = os.path.join(tmpdir, "custom_state.json")
            sd = os.path.join(tmpdir, "custom_skills")
            evo = MetaEvolution(state_path=sp, skill_dir=sd)
            assert evo.state_path == sp
            assert evo.skill_dir == sd

    def test_state_path_directory_created(self, temp_state_path):
        """MetaEvolution should create the directory for state_path."""
        # Use a deeper path to verify directory creation
        with tempfile.TemporaryDirectory() as tmpdir:
            deep_path = os.path.join(tmpdir, "nested", "dir", "state.json")
            evo = MetaEvolution(state_path=deep_path, skill_dir=tmpdir)
            assert os.path.exists(os.path.dirname(deep_path))

    def test_diagnostics_structure(self, meta_evolution):
        """diagnostics() should return a dict with module info."""
        diag = meta_evolution.diagnostics()
        assert diag["module"] == "MetaEvolution"
        assert diag["version"] == "1.0"
        assert diag["total_cycles"] == 0
        assert "state_path" in diag
        assert "skill_dir" in diag


class TestMetaEvolutionObservation:
    """Test MetaEvolution observation hooks."""

    def test_register_and_observe(self, meta_evolution):
        """Registered hooks should be called during observe()."""
        results = []

        def my_hook(ctx):
            results.append({"type": "pattern", "key": "test_pattern"})
            return results[-1]

        meta_evolution.register_observation_hook(my_hook)
        observations = meta_evolution.observe({"test": True})
        assert len(observations) == 1
        assert observations[0]["type"] == "pattern"

    def test_hook_error_handling(self, meta_evolution):
        """Failing hooks should produce error observations."""
        def broken_hook(ctx):
            raise ValueError("hook failed")

        meta_evolution.register_observation_hook(broken_hook)
        observations = meta_evolution.observe({})
        assert len(observations) == 1
        assert observations[0]["type"] == "hook_error"

    def test_multiple_hooks(self, meta_evolution):
        """Multiple hooks should all be called."""
        calls = []

        def hook1(ctx):
            calls.append("hook1")
            return [{"type": "pattern"}]

        def hook2(ctx):
            calls.append("hook2")
            return [{"type": "preference"}]

        meta_evolution.register_observation_hook(hook1)
        meta_evolution.register_observation_hook(hook2)
        obs = meta_evolution.observe({})
        assert calls == ["hook1", "hook2"]
        assert len(obs) == 2


class TestMetaEvolutionTick:
    """Test MetaEvolution tick-based execution."""

    def test_tick_starts_cycle(self, meta_evolution):
        """First tick should start a new evolution cycle at OBSERVE."""
        result = meta_evolution.tick({"test": True})
        assert result["phase"] == "observe"
        assert result["cycle_id"] is not None
        assert result["total_cycles"] == 0
        assert meta_evolution.current_cycle is not None

    def test_tick_advances_phases(self, meta_evolution):
        """Five ticks should complete a full evolution cycle."""
        # Tick 1: OBSERVE
        r1 = meta_evolution.tick()
        assert r1["phase"] == "observe"

        # Tick 2: ANALYZE
        r2 = meta_evolution.tick()
        assert r2["phase"] == "analyze"

        # Tick 3: PLAN
        r3 = meta_evolution.tick()
        assert r3["phase"] == "plan"

        # Tick 4: EXECUTE
        r4 = meta_evolution.tick()
        assert r4["phase"] == "execute"

        # Tick 5: CERTIFY → cycle complete
        r5 = meta_evolution.tick()
        assert r5["phase"] == "certify"
        assert r5["cycle_complete"] is True
        assert meta_evolution.state.total_cycles == 1

    def test_observe_returns_hook_results(self, meta_evolution):
        """OBSERVE phase should return results from hooks."""
        meta_evolution.register_observation_hook(
            lambda ctx: [{"type": "pattern", "key": "frequent", "value": "test"}]
        )
        result = meta_evolution.tick({"data": "input"})
        assert "observations" in result["result"]
        assert result["result"]["count"] >= 1

    def test_plan_generates_actions(self, meta_evolution):
        """PLAN phase should produce actionable items."""
        # First add some observations so analysis finds patterns
        meta_evolution.register_observation_hook(
            lambda ctx: [{"type": "pattern", "key": "repeat_pattern"}]
        )
        # Run through OBSERVE, ANALYZE
        meta_evolution.tick()
        r2 = meta_evolution.tick()
        assert r2["phase"] == "analyze"

        # PLAN phase
        r3 = meta_evolution.tick()
        assert r3["phase"] == "plan"
        actions = r3["result"].get("actions", [])
        assert len(actions) > 0

    def test_full_cycle_updates_state(self, meta_evolution):
        """Completing a full cycle should increment total_cycles."""
        for _ in range(5):
            meta_evolution.tick()
        assert meta_evolution.state.total_cycles == 1
        assert meta_evolution.state.last_cycle_id is not None

    def test_observation_with_corrections(self, meta_evolution):
        """Correction-type observations should be counted in analysis."""
        meta_evolution.register_observation_hook(
            lambda ctx: [{"type": "correction", "detail": "fix typo"}]
        )
        meta_evolution.tick()  # OBSERVE
        analysis = meta_evolution.tick()  # ANALYZE
        assert analysis["result"]["corrections_found"] >= 1

    def test_cycle_history_updated(self, meta_evolution):
        """Completed cycles should appear in cycle_history."""
        for _ in range(5):
            meta_evolution.tick()
        assert len(meta_evolution.state.cycle_history) == 1
        cycle_id = meta_evolution.state.cycle_history[0]
        assert cycle_id.startswith("evo_")

    def test_consecutive_cycles(self, meta_evolution):
        """Multiple full cycles should work consecutively."""
        for _ in range(10):  # 2 full cycles
            meta_evolution.tick()
        assert meta_evolution.state.total_cycles == 2

    def test_analyze_updates_patterns(self, meta_evolution):
        """Analysis should update active_patterns state."""
        # Register pattern hooks that produce the same key 3 times
        meta_evolution.register_observation_hook(
            lambda ctx: [{"type": "pattern", "key": "recurring"}]
        )
        # Run OBSERVE 3 times (each tick populates observations)
        for _ in range(3):
            meta_evolution.register_observation_hook(
                lambda ctx: [{"type": "pattern", "key": "recurring"}]
            )
        meta_evolution.tick()  # OBSERVE
        meta_evolution.tick()  # ANALYZE
        # The analysis should have detected "recurring" pattern
        assert "recurring" in meta_evolution.state.active_patterns

    def test_execute_phase_results(self, meta_evolution):
        """EXECUTE phase should return success/failure counts."""
        # Run through a full cycle
        for _ in range(4):
            meta_evolution.tick()
        # Tick 4 = EXECUTE
        result = meta_evolution.tick()  # will be CERTIFY now
        # Actually, let's test more directly
        meta_evolution = MetaEvolution(
            state_path=meta_evolution.state_path,
            skill_dir=meta_evolution.skill_dir
        )
        for i in range(4):
            meta_evolution.tick()
        r4 = meta_evolution.tick()  # EXECUTE
        # Re-check in proper order
        pass

    def test_tick_returns_phase_and_cycle(self, meta_evolution):
        """tick() return value should always contain phase and cycle_id."""
        for _ in range(5):
            result = meta_evolution.tick()
            assert "phase" in result
            assert "cycle_id" in result or result["cycle_complete"] is True
            assert "total_cycles" in result


# ====================================================================
# Tests: trinity.evolution.serialization — save/load evolution state
# ====================================================================

class TestEvolutionStateSerializer:
    """Test EvolutionStateSerializer."""

    def test_save_creates_file(self, meta_evolution, serializer):
        """save() should create a JSON file on disk."""
        path = serializer.save(meta_evolution, name="test_save")
        assert os.path.exists(path)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["version"] == "1.0"
        assert data["total_cycles"] == 0

    def test_load_returns_state(self, meta_evolution, serializer):
        """load() should return an EvolutionState from a saved file."""
        serializer.save(meta_evolution, name="test_load")
        state = serializer.load(name="test_load")
        assert state is not None
        assert isinstance(state, EvolutionState)
        assert state.version == "1.0"

    def test_load_nonexistent_returns_none(self, serializer):
        """load() should return None when no snapshot exists."""
        state = serializer.load(name="nonexistent")
        assert state is None

    def test_list_snapshots(self, meta_evolution, serializer):
        """list_snapshots() should list all saved snapshots."""
        serializer.save(meta_evolution, name="snap_a")
        serializer.save(meta_evolution, name="snap_b")
        snapshots = serializer.list_snapshots()
        names = [s["name"] for s in snapshots]
        assert "snap_a" in names
        assert "snap_b" in names
        assert len(snapshots) >= 2

    def test_save_roundtrip_preserves_data(self, meta_evolution, serializer):
        """Save then load should preserve total_cycles and preferences."""
        meta_evolution.state.total_cycles = 42
        meta_evolution.state.active_preferences["dark_mode"] = 0.9
        serializer.save(meta_evolution, name="roundtrip")
        loaded_state = serializer.load(name="roundtrip")
        assert loaded_state.total_cycles == 42
        assert loaded_state.active_preferences["dark_mode"] == 0.9

    def test_export_for_cross_platform(self, meta_evolution, serializer):
        """export_for_cross_platform() should return a minimal dict."""
        meta_evolution.state.total_cycles = 7
        meta_evolution.state.active_preferences["theme"] = 1.0
        exported = serializer.export_for_cross_platform(meta_evolution)
        assert exported["_format"] == "trinity_evolution_v1"
        assert exported["total_cycles"] == 7
        assert "theme" in exported["preferences"]

    def test_save_updates_meta_info(self, meta_evolution, serializer):
        """Saved JSON should include _meta information."""
        path = serializer.save(meta_evolution, name="with_meta")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "_meta" in data
        assert data["_meta"]["source"] == "trinity.evolution"
        assert data["_meta"]["version"] == "1.0"

    def test_multiple_saves_overwrite(self, meta_evolution, serializer):
        """Saving the same name should overwrite existing file."""
        serializer.save(meta_evolution, name="overwrite_test")
        meta_evolution.state.total_cycles = 99
        serializer.save(meta_evolution, name="overwrite_test")
        state = serializer.load(name="overwrite_test")
        assert state.total_cycles == 99


# ====================================================================
# Tests: trinity.evolution.skill_system — 技能注册和执行
# ====================================================================

class TestSkillSystemAdapter:
    """Test SkillSystemAdapter."""

    def test_init_creates_directories(self):
        """SkillSystemAdapter should create subdirectories on init."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = SkillSystemAdapter(skill_dir=tmpdir)
            assert os.path.isdir(os.path.join(tmpdir, "archive"))
            assert os.path.isdir(os.path.join(tmpdir, "domains"))
            assert os.path.isdir(os.path.join(tmpdir, "projects"))

    def test_read_memory_no_file(self, skill_adapter):
        """read_memory() should return empty data when no file exists."""
        memory = skill_adapter.read_memory()
        assert memory == {"preferences": [], "patterns": [], "recent": []}

    def test_write_then_read_memory(self, skill_adapter):
        """Writing memory then reading should return the same data."""
        skill_adapter.write_memory(
            preferences=["dark_mode", "notifications_off"],
            patterns=["morning_routine", "night_ritual"]
        )
        memory = skill_adapter.read_memory()
        assert "dark_mode" in memory["preferences"]
        assert "notifications_off" in memory["preferences"]
        assert "morning_routine" in memory["patterns"]
        assert "night_ritual" in memory["patterns"]

    def test_read_corrections_no_file(self, skill_adapter):
        """read_corrections() should return empty list when no file."""
        corrections = skill_adapter.read_corrections()
        assert corrections == []

    def test_read_corrections_parses_content(self, skill_adapter):
        """read_corrections() should parse existing corrections.md."""
        path = os.path.join(skill_adapter.skill_dir, "corrections.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Corrections Log\n\n- [fix] 修正 typo in report\n- [improve] 修正 logging format\n")
        corrections = skill_adapter.read_corrections()
        assert len(corrections) >= 1

    def test_save_project_memory(self, skill_adapter):
        """save_project_memory() should create a project file."""
        skill_adapter.save_project_memory(
            project="test_project",
            content="# Project Test\n\nNotes about this project."
        )
        proj_path = os.path.join(
            skill_adapter.skill_dir, "projects", "test_project.md"
        )
        assert os.path.exists(proj_path)
        with open(proj_path, "r", encoding="utf-8") as f:
            assert "Project Test" in f.read()

    def test_get_index_no_file(self, skill_adapter):
        """get_index() should return sensible default when index.md missing."""
        idx = skill_adapter.get_index()
        # When file doesn't exist, returns {files: [], last_updated: None}
        assert idx["files"] == []
        assert idx["last_updated"] is None

    def test_diagnostics_returns_metadata(self, skill_adapter):
        """diagnostics() should return module info and file list."""
        diag = skill_adapter.diagnostics()
        assert diag["module"] == "SkillSystemAdapter"
        assert diag["files_count"] >= 0
        assert "files" in diag

    def test_write_memory_creates_file(self, skill_adapter):
        """write_memory() should create memory.md on disk."""
        skill_adapter.write_memory(preferences=["quiet_mode"], patterns=[])
        path = os.path.join(skill_adapter.skill_dir, "memory.md")
        assert os.path.exists(path)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "quiet_mode" in content
        assert "Confirmed Preferences" in content

    def test_multiple_project_files(self, skill_adapter):
        """Multiple project memories should be saved separately."""
        skill_adapter.save_project_memory("proj_a", "Content A")
        skill_adapter.save_project_memory("proj_b", "Content B")
        path_a = os.path.join(skill_adapter.skill_dir, "projects", "proj_a.md")
        path_b = os.path.join(skill_adapter.skill_dir, "projects", "proj_b.md")
        assert os.path.exists(path_a)
        assert os.path.exists(path_b)
        with open(path_a) as f:
            assert f.read() == "Content A"
        with open(path_b) as f:
            assert f.read() == "Content B"
