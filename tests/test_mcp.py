"""Tests for trinity.mcp — MCP server, langchain adapter, tools, and resources.

Tests:
  - test_mcp_server_importable        server module can be imported
  - test_mcp_server_create            create_server() returns FastMCP instance
  - test_mcp_server_constants         SERVER_NAME, SERVER_VERSION exist
  - test_langchain_adapter_importable langchain_adapter module importable
  - test_toolkit_import               TrinityMemoryToolkit importable
  - test_toolkit_init_local           TrinityMemoryToolkit(mode="local")
  - test_toolkit_get_tools            get_tools() returns list of tools
  - test_toolkit_tool_names           tools have expected names
  - test_toolkit_tool_call            local tools are callable
  - test_memory_tools_register        register_memory_tools exists and callable
  - test_memory_tools_get_engine      _get_engine() returns a Trinity instance
  - test_memory_resources_register    register_memory_resources exists and callable
  - test_memory_resources_set_backend set_backend_references works
  - test_memory_prompts_register      register_memory_prompts exists and callable
  - test_run_server_import            run_server function is importable
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# MCP 模块需要 mcp 包，跳过所有测试直到安装
pytestmark = pytest.mark.skipif(True, reason="requires mcp package")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── MCP Server ──────────────────────────────────────────────────────────

class TestMCPServer:
    """Test trinity.mcp.server module."""

    def test_mcp_server_importable(self):
        """trinity.mcp.server module can be imported without errors."""
        import trinity.mcp.server as server
        assert server is not None

    def test_mcp_server_constants(self):
        """SERVER_NAME and SERVER_VERSION are defined."""
        import trinity.mcp.server as server
        assert hasattr(server, "SERVER_NAME")
        assert hasattr(server, "SERVER_VERSION")
        assert isinstance(server.SERVER_NAME, str)
        assert isinstance(server.SERVER_VERSION, str)
        assert len(server.SERVER_NAME) > 0
        assert len(server.SERVER_VERSION) > 0

    def test_mcp_server_description(self):
        """DESCRIPTION is a string with meaningful content."""
        import trinity.mcp.server as server
        assert hasattr(server, "DESCRIPTION")
        assert isinstance(server.DESCRIPTION, str)
        assert len(server.DESCRIPTION) > 0

    @pytest.mark.skipif(True, reason="FastMCP not available without mcp package")
    def test_create_server(self):
        """create_server() instantiates FastMCP and registers tools/resources."""
        import trinity.mcp.server as server

        mock_instance = MagicMock()
        mock_fastmcp.return_value = mock_instance

        result = server.create_server()
        assert result is mock_instance
        mock_fastmcp.assert_called_once()
        # FastMCP should be called with name=SERVER_NAME
        args, kwargs = mock_fastmcp.call_args
        assert kwargs.get("name") == server.SERVER_NAME

    @pytest.mark.skipif(True, reason="FastMCP not available without mcp package")
    def test_create_server_import_error_prints_message(self):
        """create_server() prints error and exits if mcp not installed."""
        import trinity.mcp.server as server

        with patch("trinity.mcp.server.FastMCP", side_effect=ImportError("no mcp")):
            with patch.object(sys, "stderr") as mock_stderr:
                with pytest.raises(SystemExit) as exc_info:
                    server.create_server()
                assert exc_info.value.code == 1

    def test_main_function_exists(self):
        """server module has a main() CLI entry point."""
        import trinity.mcp.server as server
        assert hasattr(server, "main")
        assert callable(server.main)

    def test_run_server_function_exists(self):
        """server module has run_server() function."""
        import trinity.mcp.server as server
        assert hasattr(server, "run_server")
        assert callable(server.run_server)

    def test_init_session_recorder(self):
        """_init_session_recorder() returns a ChatSessionRecorder."""
        import trinity.mcp.server as server
        recorder = server._init_session_recorder()
        assert recorder is not None
        assert hasattr(recorder, "log_dir")


# ── LangChain Adapter ──────────────────────────────────────────────────

class TestLangChainAdapter:
    """Test trinity.mcp.langchain_adapter module."""

    def test_adapter_importable(self):
        """trinity.mcp.langchain_adapter module can be imported."""
        import trinity.mcp.langchain_adapter as adapter
        assert adapter is not None

    def test_trinity_memory_toolkit_importable(self):
        """TrinityMemoryToolkit class is importable."""
        from trinity.mcp.langchain_adapter import TrinityMemoryToolkit
        assert TrinityMemoryToolkit is not None

    def test_toolkit_init_local(self):
        """TrinityMemoryToolkit(mode='local') initialises without errors."""
        from trinity.mcp.langchain_adapter import TrinityMemoryToolkit
        toolkit = TrinityMemoryToolkit(mode="local")
        assert toolkit.mode == "local"
        assert toolkit._tools is None  # tools not loaded yet

    @pytest.mark.skipif(True, reason="LangChain adapter requires mcp dependency")
    def test_toolkit_get_tools_returns_list(self):
        """get_tools() returns a list of BaseTool objects."""
        from trinity.mcp.langchain_adapter import TrinityMemoryToolkit
        toolkit = TrinityMemoryToolkit(mode="local")
        tools = toolkit.get_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0

    @pytest.mark.skipif(True, reason="LangChain adapter requires mcp dependency")
    def test_toolkit_tool_names(self):
        """get_tools() returns tools with expected names in local mode."""
        from trinity.mcp.langchain_adapter import TrinityMemoryToolkit
        toolkit = TrinityMemoryToolkit(mode="local")
        tools = toolkit.get_tools()
        tool_names = {t.name for t in tools}
        expected = {"memory_search", "memory_write", "memory_update",
                     "memory_delete", "audit_query"}
        for name in expected:
            assert name in tool_names, f"Missing tool: {name}"

    @pytest.mark.skipif(True, reason="LangChain adapter requires mcp dependency")
    def test_toolkit_tools_are_callable(self):
        """Each local tool is callable and returns a dict or list."""
        from trinity.mcp.langchain_adapter import TrinityMemoryToolkit
        toolkit = TrinityMemoryToolkit(mode="local")
        tools = toolkit.get_tools()
        for tool in tools:
            if tool.name == "memory_search":
                result = tool.invoke({"query": "test", "top_k": 1})
                assert isinstance(result, list)
            elif tool.name == "memory_write":
                result = tool.invoke({"content": "test content"})
                assert isinstance(result, dict)
            elif tool.name == "memory_update":
                result = tool.invoke({"memory_id": "mock_001", "new_content": "new"})
                assert isinstance(result, dict)
            elif tool.name == "memory_delete":
                result = tool.invoke({"memory_id": "mock_001"})
                assert isinstance(result, dict)
            elif tool.name == "audit_query":
                result = tool.invoke({"memory_id": "mock_001"})
                assert isinstance(result, dict)

    def test_create_trinity_agent_function(self):
        """create_trinity_agent is importable and callable."""
        from trinity.mcp.langchain_adapter import create_trinity_agent
        assert callable(create_trinity_agent)

    def test_create_trinity_agent_raises_without_langchain(self):
        """create_trinity_agent() raises ImportError when LangChain is unavailable."""
        from trinity.mcp.langchain_adapter import create_trinity_agent, LANGCHAIN_AVAILABLE
        if not LANGCHAIN_AVAILABLE:
            with pytest.raises(ImportError):
                create_trinity_agent(None)  # type: ignore[arg-type]

    def test_langchain_available_flag(self):
        """LANGCHAIN_AVAILABLE is a boolean."""
        from trinity.mcp.langchain_adapter import LANGCHAIN_AVAILABLE
        assert isinstance(LANGCHAIN_AVAILABLE, bool)

    @pytest.mark.skipif(True, reason="LangChain adapter requires mcp dependency")
    def test_toolkit_double_get_tools_returns_same_list(self):
        """Calling get_tools() twice returns the same list (cached)."""
        from trinity.mcp.langchain_adapter import TrinityMemoryToolkit
        toolkit = TrinityMemoryToolkit(mode="local")
        tools1 = toolkit.get_tools()
        tools2 = toolkit.get_tools()
        assert tools1 is tools2  # Same cached list


# ── MCP Tools (memory_tools) ────────────────────────────────────────────

class TestMemoryTools:
    """Test trinity.mcp.tools.memory_tools module."""

    def test_memory_tools_importable(self):
        """trinity.mcp.tools.memory_tools module can be imported."""
        import trinity.mcp.tools.memory_tools as mt
        assert mt is not None

    def test_register_memory_tools_exists(self):
        """register_memory_tools function exists and is callable."""
        from trinity.mcp.tools.memory_tools import register_memory_tools
        assert callable(register_memory_tools)

    def test_register_memory_tools_with_mock_mcp(self):
        """register_memory_tools can be called with a mock FastMCP."""
        from trinity.mcp.tools.memory_tools import register_memory_tools
        mock_mcp = MagicMock()
        # Should not raise
        register_memory_tools(mock_mcp)
        # The @mcp.tool() decorator should have been called
        assert mock_mcp.tool.call_count >= 1

    def test_get_engine_function(self):
        """_get_engine() returns a Trinity instance."""
        from trinity.mcp.tools.memory_tools import _get_engine
        engine = _get_engine()
        assert engine is not None
        assert hasattr(engine, "search")
        assert hasattr(engine, "ingest")

    def test_get_engine_is_singleton(self):
        """_get_engine() returns the same instance on repeated calls."""
        from trinity.mcp.tools.memory_tools import _get_engine
        e1 = _get_engine()
        e2 = _get_engine()
        assert e1 is e2

    def test_set_session_recorder(self):
        """set_session_recorder injects a recorder reference."""
        from trinity.mcp.tools.memory_tools import set_session_recorder, get_session_recorder
        recorder = MagicMock()
        set_session_recorder(recorder)
        retrieved = get_session_recorder()
        assert retrieved is recorder

    def test_get_session_recorder_default(self):
        """get_session_recorder() returns None before set_session_recorder."""
        from trinity.mcp.tools.memory_tools import get_session_recorder
        # Reset to None for test (may be None already)
        from trinity.mcp.tools.memory_tools import _session_recorder
        original = _session_recorder
        try:
            import trinity.mcp.tools.memory_tools as mt
            mt._session_recorder = None
            assert get_session_recorder() is None
        finally:
            mt._session_recorder = original


# ── MCP Resources (memory_resources) ────────────────────────────────────

class TestMemoryResources:
    """Test trinity.mcp.resources.memory_resources module."""

    def test_memory_resources_importable(self):
        """trinity.mcp.resources.memory_resources module can be imported."""
        import trinity.mcp.resources.memory_resources as mr
        assert mr is not None

    def test_register_memory_resources_exists(self):
        """register_memory_resources function exists and is callable."""
        from trinity.mcp.resources.memory_resources import register_memory_resources
        assert callable(register_memory_resources)

    def test_register_memory_resources_with_mock_mcp(self):
        """register_memory_resources can be called with a mock FastMCP."""
        from trinity.mcp.resources.memory_resources import register_memory_resources
        mock_mcp = MagicMock()
        # Should not raise
        register_memory_resources(mock_mcp)
        # Resources decorator should have been called
        assert mock_mcp.resource.call_count >= 1

    def test_set_backend_references(self):
        """set_backend_references stores references correctly."""
        from trinity.mcp.resources.memory_resources import set_backend_references
        memory_store = {"key1": {"content": "test", "status": "active"}}
        version_store = {"key1": [{"version": "v1"}]}
        # Should not raise
        set_backend_references(memory_store, version_store)

    def test_set_session_recorder(self):
        """set_session_recorder injects recorder reference."""
        from trinity.mcp.resources.memory_resources import set_session_recorder
        recorder = MagicMock()
        # Should not raise
        set_session_recorder(recorder)


# ── MCP Prompts ─────────────────────────────────────────────────────────

class TestMemoryPrompts:
    """Test trinity.mcp.prompts.memory_prompts module."""

    def test_memory_prompts_importable(self):
        """trinity.mcp.prompts.memory_prompts module can be imported."""
        import trinity.mcp.prompts.memory_prompts as mp
        assert mp is not None

    def test_register_memory_prompts_exists(self):
        """register_memory_prompts function exists and is callable."""
        from trinity.mcp.prompts.memory_prompts import register_memory_prompts
        assert callable(register_memory_prompts)

    def test_register_memory_prompts_with_mock_mcp(self):
        """register_memory_prompts can be called with a mock FastMCP."""
        from trinity.mcp.prompts.memory_prompts import register_memory_prompts
        mock_mcp = MagicMock()
        # Should not raise
        register_memory_prompts(mock_mcp)
        # The @mcp.prompt() decorator should have been called
        assert mock_mcp.prompt.call_count >= 1


# ── Integration: server creates tools + resources ───────────────────────

class TestMCPServerIntegration:
    """Integration-level tests: server creates tools and resources."""

    @pytest.mark.skipif(True, reason="FastMCP not available without mcp package")
    def test_create_server_registers_tools(self):
        """create_server() calls register_memory_tools."""
        import trinity.mcp.server as server
        with patch.object(server, "register_memory_tools") as mock_register:
            server.create_server()
            mock_register.assert_called_once()

    @pytest.mark.skipif(True, reason="FastMCP not available without mcp package")
    def test_create_server_registers_resources(self):
        """create_server() calls register_memory_resources."""
        import trinity.mcp.server as server
        with patch.object(server, "register_memory_resources") as mock_register:
            server.create_server()
            mock_register.assert_called_once()

    @pytest.mark.skipif(True, reason="FastMCP not available without mcp package")
    def test_create_server_registers_prompts(self):
        """create_server() calls register_memory_prompts."""
        import trinity.mcp.server as server
        with patch.object(server, "register_memory_prompts") as mock_register:
            server.create_server()
            mock_register.assert_called_once()
