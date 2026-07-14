"""
LangChain 集成适配器

封装 Trinity MCP Server 的全部 tools 为 LangChain Tool 格式，
支持一键创建带 Trinity 记忆的 LangChain Agent。

使用方式:
    from langchain_adapter import create_trinity_agent
    agent = create_trinity_agent(llm)  # 一键创建 Agent
"""

import logging
from typing import Any, Optional

# LangChain / LangGraph 依赖（运行时动态导入，避免未安装时阻塞）
try:
    from langchain_core.tools import BaseTool, tool
    from langchain.agents import create_react_agent  # type: ignore[import-untyped]
    from langchain_core.language_models import BaseChatModel
    from langchain.memory import ConversationBufferMemory  # type: ignore[import-untyped]

    LANGCHAIN_AVAILABLE: bool = True
except ImportError:
    LANGCHAIN_AVAILABLE = False

logger = logging.getLogger("trinity_mcp.langchain_adapter")

# ---------------------------------------------------------------------------
# 模拟 Trinity MCP 工具函数（生产环境通过 MCPClient 调用）
# ---------------------------------------------------------------------------
def _mock_memory_search(query: str, top_k: int = 5, mode: str = "hybrid") -> list[dict[str, Any]]:
    """模拟 memory_search。生产环境通过 MCPClient 调用。"""
    return [{"memory_id": "mock_001", "content": f"Result for: {query}", "score": 0.95}]


def _mock_memory_write(content: str, metadata: Optional[dict[str, Any]] = None, category: str = "general") -> dict[str, Any]:
    """模拟 memory_write。生产环境通过 MCPClient 调用。"""
    return {"memory_id": "mock_001", "version_id": "ver_abc123", "sha256_hash": "a" * 64}


def _mock_memory_update(memory_id: str, new_content: str) -> dict[str, Any]:
    """模拟 memory_update。生产环境通过 MCPClient 调用。"""
    return {"memory_id": memory_id, "old_version": "ver_old", "new_version": "ver_new"}


def _mock_memory_delete(memory_id: str) -> dict[str, Any]:
    """模拟 memory_delete。生产环境通过 MCPClient 调用。"""
    return {"memory_id": memory_id, "deleted_version": "ver_del"}


def _mock_audit_query(memory_id: str) -> dict[str, Any]:
    """模拟 audit_query。生产环境通过 MCPClient 调用。"""
    return {"memory_id": memory_id, "version_chain": [], "current_status": "active"}


# ---------------------------------------------------------------------------
# TrinityMemoryToolkit
# ---------------------------------------------------------------------------
class TrinityMemoryToolkit:
    """Trinity 记忆工具包 — 封装所有 MCP Memory Tools 为 LangChain Tool 格式。

    支持两种后端模式：
    - MCP 模式：通过 MCPClient 连接远程 Trinity MCP Server（推荐生产）。
    - 本地模拟模式：不连接 MCP Server，使用本地内存模拟（开发/测试）。

    使用示例::

        from langchain_openai import ChatOpenAI
        from langchain_adapter import TrinityMemoryToolkit, create_trinity_agent

        llm = ChatOpenAI(model="gpt-4o", temperature=0)
        toolkit = TrinityMemoryToolkit(mode="local")
        agent = create_trinity_agent(llm, toolkit)
        result = agent.invoke({"input": "回顾一下关于项目 Alpha 的所有记录"})
    """

    def __init__(self, mode: str = "local", mcp_server_script: str = "server.py"):
        """初始化工具包。

        Args:
            mode:               后端模式，"local" 或 "mcp"。默认 "local"。
            mcp_server_script:  MCP Server 启动脚本路径（仅 mode="mcp" 时使用）。
        """
        self.mode: str = mode
        self.mcp_server_script: str = mcp_server_script
        self._tools: Optional[list[BaseTool]] = None

        if mode == "mcp":
            if not LANGCHAIN_AVAILABLE:
                raise ImportError(
                    "LangChain packages required for MCP mode. "
                    "Install: pip install langchain langchain-mcp-adapters"
                )
            logger.info(
                "TrinityMemoryToolkit initialized in MCP mode (server: %s).",
                mcp_server_script,
            )
        else:
            logger.info("TrinityMemoryToolkit initialized in local mode.")

    # ------------------------------------------------------------------
    # Tool 定义（本地模式）
    # ------------------------------------------------------------------
    def get_tools(self) -> list[BaseTool]:
        """获取所有 LangChain Tool。

        Returns:
            LangChain Tool 列表，可直接传递给 Agent。
        """
        if self._tools is not None:
            return self._tools

        if self.mode == "local":
            self._tools = self._build_local_tools()
        else:
            self._tools = self._build_mcp_tools_sync()
        return self._tools

    def _build_local_tools(self) -> list[BaseTool]:
        """构建本地模拟工具集。"""

        @tool
        def memory_search(query: str, top_k: int = 5, mode: str = "hybrid") -> list[dict[str, Any]]:
            """三模记忆检索。模式: semantic / graph / exact / hybrid（RRF 融合）。

            Args:
                query:  检索查询字符串。
                top_k:  返回数量上限。
                mode:   检索模式。
            """
            return _mock_memory_search(query, top_k, mode)

        @tool
        def memory_write(content: str, metadata: Optional[dict[str, Any]] = None, category: str = "general") -> dict[str, Any]:
            """写入记忆，返回 version_id + SHA-256。

            Args:
                content:  记忆内容。
                metadata: 附加元数据。
                category: 分类标签。
            """
            return _mock_memory_write(content, metadata, category)

        @tool
        def memory_update(memory_id: str, new_content: str) -> dict[str, Any]:
            """更新记忆（冲突保留）。

            Args:
                memory_id:   目标记忆 ID。
                new_content: 新内容。
            """
            return _mock_memory_update(memory_id, new_content)

        @tool
        def memory_delete(memory_id: str) -> dict[str, Any]:
            """软删除记忆（保留审计链）。

            Args:
                memory_id: 目标记忆 ID。
            """
            return _mock_memory_delete(memory_id)

        @tool
        def audit_query(memory_id: str) -> dict[str, Any]:
            """SHA-256 溯源查询版本链。

            Args:
                memory_id: 目标记忆 ID。
            """
            return _mock_audit_query(memory_id)

        tools: list[BaseTool] = [
            memory_search,
            memory_write,
            memory_update,
            memory_delete,
            audit_query,
        ]
        logger.info("Built %d local LangChain tools.", len(tools))
        return tools

    def _build_mcp_tools_sync(self) -> list[BaseTool]:
        """从 MCP Server 同步导入工具（简化版，生产环境应异步加载）。"""
        try:
            from langchain_mcp_adapters.tools import load_mcp_tools  # type: ignore[import-untyped]
            import asyncio

            async def _load() -> list[BaseTool]:
                return await load_mcp_tools(self.mcp_server_script)

            tools: list[BaseTool] = asyncio.run(_load())
            logger.info("Loaded %d tools from MCP Server.", len(tools))
            return tools
        except ImportError:
            logger.warning(
                "langchain-mcp-adapters not installed; falling back to local mock tools."
            )
            return self._build_local_tools()
        except Exception:
            logger.exception("Failed to load MCP tools; falling back to local mock tools.")
            return self._build_local_tools()


# ---------------------------------------------------------------------------
# 一键创建 Agent
# ---------------------------------------------------------------------------
def create_trinity_agent(
    llm: "BaseChatModel",
    toolkit: Optional[TrinityMemoryToolkit] = None,
    verbose: bool = False,
) -> Any:
    """一键创建带 Trinity 记忆的 LangChain Agent。

    Args:
        llm:     LangChain 聊天模型实例（如 ChatOpenAI）。
        toolkit: TrinityMemoryToolkit 实例。不传则自动创建 local 模式。
        verbose: 是否打印详细日志。

    Returns:
        可调用的 LangChain Agent（AgentExecutor 兼容）。
    """
    if not LANGCHAIN_AVAILABLE:
        raise ImportError(
            "LangChain is required. Install: pip install langchain langchain-openai"
        )

    if toolkit is None:
        toolkit = TrinityMemoryToolkit(mode="local")

    tools: list[BaseTool] = toolkit.get_tools()
    memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)

    agent = create_react_agent(
        llm=llm,
        tools=tools,
        prompt=None,  # 使用默认 ReAct prompt
    )

    # 包装为 AgentExecutor 兼容接口
    from langchain.agents import AgentExecutor
    executor = AgentExecutor(
        agent=agent,
        tools=tools,
        memory=memory,
        verbose=verbose,
        handle_parsing_errors=True,
    )

    logger.info("Trinity Agent created with %d tools.", len(tools))
    return executor


# ---------------------------------------------------------------------------
# 使用示例
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    """
    运行方式:
        python langchain_adapter.py

    注意：需要设置 OPENAI_API_KEY 环境变量。
    """
    import os

    logging.basicConfig(level=logging.INFO)

    # 检查是否有 API Key
    api_key: str = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print(
            "请设置 OPENAI_API_KEY 环境变量后重试。\n"
            "    $env:OPENAI_API_KEY='sk-...'   # PowerShell\n"
            "    export OPENAI_API_KEY='sk-...'  # Bash"
        )
    else:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(model="gpt-4o", temperature=0)
        agent = create_trinity_agent(llm, verbose=True)

        print("=" * 60)
        print("Trinity MCP Agent 已就绪，输入 'exit' 退出。")
        print("=" * 60)

        while True:
            user_input: str = input("\nYou: ")
            if user_input.lower() in ("exit", "quit", "q"):
                print("Bye.")
                break
            try:
                result = agent.invoke({"input": user_input})
                print(f"\nAgent: {result['output']}")
            except Exception as exc:
                print(f"\n[Error] {exc}")
