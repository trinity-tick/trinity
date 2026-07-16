"""Tests for trinity.core.bridge — bridge() legacy compatibility layer."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


class TestBridge:
    """Test the refactored bridge() routing to Trinity()."""

    def test_bridge_search(self):
        from trinity.core.bridge import trinity
        result = trinity("search", query="test", top_k=3)
        assert "results" in result
        assert "count" in result

    @pytest.mark.skipif(True, reason="bridge need trinity_call from Marvis workspace")
    def test_bridge_contradiction(self):
        pass

    @pytest.mark.skipif(True, reason="bridge need trinity_call from Marvis workspace")
    def test_bridge_ingest(self):
        pass

    @pytest.mark.skipif(True, reason="bridge need trinity_call from Marvis workspace")
    def test_bridge_diagnostics(self):
        pass

    @pytest.mark.skipif(True, reason="bridge need trinity_call from Marvis workspace")
    def test_bridge_unknown_action(self):
        pass

    @pytest.mark.skipif(True, reason="bridge need trinity_call from Marvis workspace")
    def test_bridge_strategy(self):
        pass

    @pytest.mark.skipif(True, reason="bridge need trinity_call from Marvis workspace")
    def test_bridge_hopfield(self):
        pass
