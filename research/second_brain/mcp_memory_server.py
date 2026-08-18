"""
# status: orphan (2026-08-15 audit, not in runtime path)
P10-2: MCP Stateless Memory Service Interface (对标 MCP 2026-07-28 GA)

将 Trinity 记忆系统包装为无状态 MCP Server，实现 Header 路由、可缓存工具目录、
MRTR 多轮请求、Tasks 异步记忆操作扩展。

Reference: MCP 2026-07-28 Specification
           https://blog.modelcontextprotocol.io/posts/2026-07-28
"""

import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("trinity.mcp_memory_server")


# ─── Enums ───────────────────────────────────────────────────────────────────

class McpMethod(Enum):
    """MCP 2026-07-28 协议方法（Header Mcp-Method 路由）"""
    TOOLS_LIST = "tools/list"
    TOOLS_CALL = "tools/call"
    RESOURCES_LIST = "resources/list"
    RESOURCES_READ = "resources/read"
    PROMPTS_LIST = "prompts/list"
    PROMPTS_GET = "prompts/get"
    TASKS_CREATE = "tasks/create"
    TASKS_GET = "tasks/get"
    TASKS_CANCEL = "tasks/cancel"
    TASKS_LIST = "tasks/list"
    INITIALIZE = "initialize"
    PING = "ping"


class TaskState(Enum):
    """异步 Task 状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CacheDirective(Enum):
    """缓存指令"""
    NO_CACHE = "no-cache"
    CACHE_1H = "max-age=3600"
    CACHE_24H = "max-age=86400"
    ETAG_ONLY = "etag-only"


# ─── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class McpRequest:
    """MCP 无状态请求。

    对标 2026-07-28 规范：每个请求自描述，无需 session/handshake。
    """
    method: str                     # Mcp-Method header
    tool_name: Optional[str] = None # Mcp-Name header（tools/call 时必填）
    params: dict = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    headers: dict = field(default_factory=dict)
    mrt_round: int = 0             # MRTR 往返计数


@dataclass
class McpResponse:
    """MCP 无状态响应。"""
    request_id: str
    success: bool
    data: Any = None
    error: Optional[str] = None
    cache_hint: Optional[str] = None       # Cache-Control 提示
    etag: Optional[str] = None
    mrt_continue: bool = False             # MRTR：是否需要客户端继续
    mrt_next_prompt: Optional[str] = None  # MRTR：下一轮提示


@dataclass
class ToolDescriptor:
    """MCP Tool 描述符。"""
    name: str
    description: str
    input_schema: dict
    handler: Callable
    cacheable: bool = False
    cache_ttl: int = 3600    # 秒
    category: str = "memory"


@dataclass
class TaskRecord:
    """异步记忆操作 Task 记录。"""
    task_id: str
    method: str
    params: dict
    state: TaskState = TaskState.PENDING
    result: Any = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    progress: float = 0.0     # 0.0 ~ 1.0


# ─── Tool Registry ───────────────────────────────────────────────────────────

class ToolRegistry:
    """MCP Tool 注册表。

    支持确定性排序（按 name），以保持跨重连的提示缓存稳定。
    """

    def __init__(self):
        self._tools: OrderedDict[str, ToolDescriptor] = OrderedDict()
        self._etag: Optional[str] = None

    def register(self, tool: ToolDescriptor):
        """注册一个 Tool。"""
        self._tools[tool.name] = tool
        self._recompute_etag()

    def unregister(self, name: str):
        self._tools.pop(name, None)
        self._recompute_etag()

    def list_tools(self, category: Optional[str] = None) -> list[dict]:
        """列出所有 Tool（确定性排序）。

        Returns:
            Tool 列表，按 name 字母序排序，每个 Tool 含 cache hints。
        """
        tools = sorted(self._tools.values(), key=lambda t: t.name)
        if category:
            tools = [t for t in tools if t.category == category]

        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
                "cacheable": t.cacheable,
                "cacheTTL": t.cache_ttl,
                "category": t.category,
            }
            for t in tools
        ]

    def get_tool(self, name: str) -> Optional[ToolDescriptor]:
        return self._tools.get(name)

    def get_etag(self) -> str:
        return self._etag or ""

    def _recompute_etag(self):
        """根据工具列表计算 ETag，用于缓存失效。"""
        payload = json.dumps(
            sorted([t.name for t in self._tools.values()]), sort_keys=True
        )
        self._etag = hashlib.sha256(payload.encode()).hexdigest()[:16]


# ─── Task Manager ────────────────────────────────────────────────────────────

class TaskManager:
    """MCP Tasks 扩展：异步记忆操作管理。"""

    def __init__(self):
        self._tasks: dict[str, TaskRecord] = {}
        self._max_tasks = 1000

    def create_task(self, method: str, params: dict) -> TaskRecord:
        task_id = str(uuid.uuid4())[:8]
        task = TaskRecord(task_id=task_id, method=method, params=params)
        self._tasks[task_id] = task

        # 清理旧任务
        if len(self._tasks) > self._max_tasks:
            completed = [tid for tid, t in self._tasks.items()
                         if t.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED)]
            oldest = sorted(completed,
                            key=lambda tid: self._tasks[tid].created_at)[:abs(self._max_tasks - len(self._tasks))]
            for tid in oldest:
                del self._tasks[tid]

        return task

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        return self._tasks.get(task_id)

    def update_task(self, task_id: str, state: Optional[TaskState] = None,
                    result: Any = None, error: Optional[str] = None,
                    progress: Optional[float] = None):
        task = self._tasks.get(task_id)
        if not task:
            return
        if state is not None:
            task.state = state
        if result is not None:
            task.result = result
        if error is not None:
            task.error = error
        if progress is not None:
            task.progress = progress
        if state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            task.completed_at = time.time()

    def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.state not in (TaskState.PENDING, TaskState.RUNNING):
            return False
        task.state = TaskState.CANCELLED
        task.completed_at = time.time()
        return True

    def list_tasks(self, state: Optional[TaskState] = None) -> list[dict]:
        tasks = list(self._tasks.values())
        if state:
            tasks = [t for t in tasks if t.state == state]
        return [
            {
                "taskId": t.task_id,
                "method": t.method,
                "state": t.state.value,
                "progress": t.progress,
                "createdAt": t.created_at,
                "completedAt": t.completed_at,
            }
            for t in sorted(tasks, key=lambda t: t.created_at, reverse=True)
        ]


# ─── Memory Service Core ─────────────────────────────────────────────────────

class MemoryServiceCore:
    """Trinity 记忆核心能力封装。

    暴露 search/retrieve/store/update/delete/consolidate 六大操作，
    供 MCP Server 作为 tools 调用。
    """

    def __init__(self):
        self._memory = {}           # 简化内存存储，生产环境对接 Trinity 引擎
        self._operations_log: list[dict] = []

    def search(self, query: str, top_k: int = 10, filters: Optional[dict] = None) -> list[dict]:
        """语义搜索记忆。"""
        results = []
        for mem_id, mem in self._memory.items():
            score = self._simple_score(query, mem)
            if score > 0:
                results.append({
                    "id": mem_id,
                    "score": score,
                    "content": mem.get("content", ""),
                    "metadata": mem.get("metadata", {}),
                })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def retrieve(self, memory_id: str) -> Optional[dict]:
        """按 ID 检索单条记忆。"""
        mem = self._memory.get(memory_id)
        if mem:
            return {"id": memory_id, "content": mem.get("content", ""),
                    "metadata": mem.get("metadata", {})}
        return None

    def store(self, content: str, metadata: Optional[dict] = None,
              memory_id: Optional[str] = None) -> dict:
        """存储新记忆。"""
        mid = memory_id or str(uuid.uuid4())[:12]
        record = {
            "content": content,
            "metadata": metadata or {},
            "created_at": time.time(),
            "updated_at": time.time(),
            "version": 1,
        }
        self._memory[mid] = record
        self._log("store", mid, record)
        return {"id": mid, "content": content, "metadata": metadata or {}}

    def update(self, memory_id: str, content: Optional[str] = None,
               metadata: Optional[dict] = None) -> Optional[dict]:
        """更新已有记忆（CRDT 兼容）。"""
        mem = self._memory.get(memory_id)
        if not mem:
            return None
        if content is not None:
            mem["content"] = content
        if metadata is not None:
            mem["metadata"] = {**mem.get("metadata", {}), **metadata}
        mem["updated_at"] = time.time()
        mem["version"] = mem.get("version", 1) + 1
        self._log("update", memory_id, mem)
        return {"id": memory_id, "content": mem["content"], "metadata": mem["metadata"]}

    def delete(self, memory_id: str) -> bool:
        """软删除记忆。"""
        mem = self._memory.get(memory_id)
        if not mem:
            return False
        mem["deleted"] = True
        mem["deleted_at"] = time.time()
        self._log("delete", memory_id, mem)
        return True

    def consolidate(self, memory_ids: list[str],
                    strategy: str = "summarize") -> dict:
        """整合多条记忆为一条。"""
        contents = []
        for mid in memory_ids:
            mem = self._memory.get(mid)
            if mem and not mem.get("deleted"):
                contents.append(mem["content"])

        merged_content = "\n\n".join(contents) if strategy == "concat" else (
            f"[Consolidated {len(contents)} memories] " + " | ".join(contents[:3])
        )
        new_id = str(uuid.uuid4())[:12]
        result = self.store(
            content=merged_content,
            metadata={"consolidated_from": memory_ids, "strategy": strategy},
            memory_id=new_id,
        )
        self._log("consolidate", new_id, result)
        return {"id": new_id, "content": merged_content,
                "source_count": len(contents), "strategy": strategy}

    def stats(self) -> dict:
        active = sum(1 for m in self._memory.values() if not m.get("deleted"))
        deleted = sum(1 for m in self._memory.values() if m.get("deleted"))
        return {"total": len(self._memory), "active": active, "deleted": deleted}

    def _simple_score(self, query: str, memory: dict) -> float:
        """简化评分：关键词命中。"""
        content = memory.get("content", "")
        if not content:
            return 0.0
        ql = query.lower()
        cl = content.lower()
        if ql in cl:
            return 0.8
        words = ql.split()
        hits = sum(1 for w in words if w in cl)
        return hits / max(len(words), 1) * 0.6

    def _log(self, operation: str, memory_id: str, data: dict):
        self._operations_log.append({
            "operation": operation,
            "memory_id": memory_id,
            "timestamp": time.time(),
        })


# ─── MCP Memory Server ──────────────────────────────────────────────────────

class McpMemoryServer:
    """MCP 2026-07-28 无状态记忆服务。

    实现：
    - 无状态请求/响应（无 session/handshake）
    - Header 路由（Mcp-Method / Mcp-Name）
    - 可缓存工具目录（带 cache hints + 确定性排序）
    - MRTR 多轮请求支持
    - Tasks 异步记忆操作扩展
    """

    def __init__(self, name: str = "Trinity MCP Memory Server",
                 version: str = "2.0.0"):
        self.name = name
        self.version = version
        self.memory = MemoryServiceCore()
        self.tool_registry = ToolRegistry()
        self.task_manager = TaskManager()

        self._register_default_tools()

    def _register_default_tools(self):
        """注册核心记忆能力为 MCP tools。"""
        # 1. memory_search
        self.tool_registry.register(ToolDescriptor(
            name="memory_search",
            description="Semantic search over Trinity memory store",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "top_k": {"type": "integer", "default": 10},
                    "filters": {"type": "object", "description": "Optional metadata filters"},
                },
                "required": ["query"],
            },
            handler=lambda params: self.memory.search(
                query=params["query"],
                top_k=params.get("top_k", 10),
                filters=params.get("filters"),
            ),
            cacheable=False,
            category="memory",
        ))

        # 2. memory_retrieve
        self.tool_registry.register(ToolDescriptor(
            name="memory_retrieve",
            description="Retrieve a single memory by ID",
            input_schema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                },
                "required": ["memory_id"],
            },
            handler=lambda params: self.memory.retrieve(params["memory_id"]),
            cacheable=True,
            cache_ttl=300,
            category="memory",
        ))

        # 3. memory_store
        self.tool_registry.register(ToolDescriptor(
            name="memory_store",
            description="Store a new memory entry",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "metadata": {"type": "object"},
                    "memory_id": {"type": "string"},
                },
                "required": ["content"],
            },
            handler=lambda params: self.memory.store(
                content=params["content"],
                metadata=params.get("metadata"),
                memory_id=params.get("memory_id"),
            ),
            cacheable=False,
            category="memory",
        ))

        # 4. memory_update
        self.tool_registry.register(ToolDescriptor(
            name="memory_update",
            description="Update an existing memory entry (CRDT-compatible)",
            input_schema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "content": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["memory_id"],
            },
            handler=lambda params: self.memory.update(
                memory_id=params["memory_id"],
                content=params.get("content"),
                metadata=params.get("metadata"),
            ),
            cacheable=False,
            category="memory",
        ))

        # 5. memory_delete
        self.tool_registry.register(ToolDescriptor(
            name="memory_delete",
            description="Soft-delete a memory entry",
            input_schema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                },
                "required": ["memory_id"],
            },
            handler=lambda params: {"deleted": self.memory.delete(params["memory_id"])},
            cacheable=False,
            category="memory",
        ))

        # 6. memory_consolidate
        self.tool_registry.register(ToolDescriptor(
            name="memory_consolidate",
            description="Consolidate multiple memories into one",
            input_schema={
                "type": "object",
                "properties": {
                    "memory_ids": {"type": "array", "items": {"type": "string"}},
                    "strategy": {"type": "string", "enum": ["summarize", "concat"]},
                },
                "required": ["memory_ids"],
            },
            handler=lambda params: self.memory.consolidate(
                memory_ids=params["memory_ids"],
                strategy=params.get("strategy", "summarize"),
            ),
            cacheable=False,
            category="memory",
        ))

        # 7. memory_stats (诊断用)
        self.tool_registry.register(ToolDescriptor(
            name="memory_stats",
            description="Get memory store statistics",
            input_schema={"type": "object", "properties": {}},
            handler=lambda _params: self.memory.stats(),
            cacheable=True,
            cache_ttl=60,
            category="diagnostic",
        ))

    # ── Request Handling ──

    def handle_request(self, request: McpRequest) -> McpResponse:
        """处理无状态 MCP 请求。

        根据 Mcp-Method header 路由到对应处理器。
        """
        try:
            method = request.method

            if method == "initialize":
                return self._handle_initialize(request)
            elif method == "ping":
                return self._handle_ping(request)
            elif method == "tools/list":
                return self._handle_tools_list(request)
            elif method == "tools/call":
                return self._handle_tools_call(request)
            elif method == "resources/list":
                return self._handle_resources_list(request)
            elif method == "resources/read":
                return self._handle_resources_read(request)
            elif method == "tasks/create":
                return self._handle_tasks_create(request)
            elif method == "tasks/get":
                return self._handle_tasks_get(request)
            elif method == "tasks/cancel":
                return self._handle_tasks_cancel(request)
            elif method == "tasks/list":
                return self._handle_tasks_list(request)
            elif method == "prompts/list":
                return self._handle_prompts_list(request)
            elif method == "prompts/get":
                return self._handle_prompts_get(request)
            else:
                return McpResponse(
                    request_id=request.request_id,
                    success=False,
                    error=f"Unknown method: {method}",
                )
        except Exception as e:
            logger.exception("Error handling MCP request")
            return McpResponse(
                request_id=request.request_id,
                success=False,
                error=str(e),
            )

    # ── Method Handlers ──

    def _handle_initialize(self, req: McpRequest) -> McpResponse:
        return McpResponse(
            request_id=req.request_id,
            success=True,
            data={
                "protocolVersion": "2026-07-28",
                "serverInfo": {"name": self.name, "version": self.version},
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                    "tasks": {"supported": True},
                    "mrt": {"supported": True},
                },
            },
        )

    def _handle_ping(self, req: McpRequest) -> McpResponse:
        return McpResponse(request_id=req.request_id, success=True, data={"pong": True})

    def _handle_tools_list(self, req: McpRequest) -> McpResponse:
        category = req.params.get("category")
        tools = self.tool_registry.list_tools(category=category)
        etag = self.tool_registry.get_etag()

        # 检查客户端 ETag
        client_etag = req.headers.get("If-None-Match", "")
        if client_etag and client_etag == etag:
            return McpResponse(
                request_id=req.request_id,
                success=True,
                data={"tools": [], "notModified": True},
                cache_hint=CacheDirective.CACHE_24H.value,
                etag=etag,
            )

        return McpResponse(
            request_id=req.request_id,
            success=True,
            data={
                "tools": tools,
                "total": len(tools),
                "cacheHint": CacheDirective.CACHE_24H.value,
            },
            cache_hint=CacheDirective.CACHE_24H.value,
            etag=etag,
        )

    def _handle_tools_call(self, req: McpRequest) -> McpResponse:
        tool_name = req.tool_name or req.params.get("name", "")
        tool = self.tool_registry.get_tool(tool_name)
        if not tool:
            return McpResponse(
                request_id=req.request_id,
                success=False,
                error=f"Tool not found: {tool_name}",
            )

        try:
            result = tool.handler(req.params.get("arguments", {}))
            return McpResponse(
                request_id=req.request_id,
                success=True,
                data={"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]},
            )
        except Exception as e:
            return McpResponse(
                request_id=req.request_id,
                success=False,
                error=str(e),
            )

    def _handle_resources_list(self, req: McpRequest) -> McpResponse:
        resources = [
            {
                "uri": "trinity://stats",
                "name": "Memory Statistics",
                "description": "Trinity memory system statistics",
                "mimeType": "application/json",
            },
            {
                "uri": "trinity://health",
                "name": "Health Check",
                "description": "System health check",
                "mimeType": "application/json",
            },
        ]
        return McpResponse(request_id=req.request_id, success=True,
                           data={"resources": resources})

    def _handle_resources_read(self, req: McpRequest) -> McpResponse:
        uri = req.params.get("uri", "")
        if uri == "trinity://stats":
            data = self.memory.stats()
        elif uri == "trinity://health":
            data = {"status": "healthy", "uptime": "unknown"}
        else:
            return McpResponse(request_id=req.request_id, success=False,
                               error=f"Unknown resource: {uri}")

        return McpResponse(request_id=req.request_id, success=True, data={
            "contents": [{"uri": uri, "mimeType": "application/json",
                          "text": json.dumps(data, ensure_ascii=False)}],
        })

    # ── Tasks Handlers ──

    def _handle_tasks_create(self, req: McpRequest) -> McpResponse:
        """创建异步记忆操作 Task。"""
        method = req.params.get("method", "")
        task_params = req.params.get("params", {})
        task = self.task_manager.create_task(method, task_params)
        self.task_manager.update_task(task.task_id, state=TaskState.PENDING)

        return McpResponse(request_id=req.request_id, success=True, data={
            "taskId": task.task_id,
            "state": task.state.value,
            "createdAt": task.created_at,
        })

    def _handle_tasks_get(self, req: McpRequest) -> McpResponse:
        task_id = req.params.get("task_id", "")
        task = self.task_manager.get_task(task_id)
        if not task:
            return McpResponse(request_id=req.request_id, success=False,
                               error=f"Task not found: {task_id}")

        data = {"taskId": task.task_id, "state": task.state.value,
                "progress": task.progress, "createdAt": task.created_at}
        if task.state == TaskState.COMPLETED:
            data["result"] = task.result
        if task.error:
            data["error"] = task.error

        return McpResponse(request_id=req.request_id, success=True, data=data)

    def _handle_tasks_cancel(self, req: McpRequest) -> McpResponse:
        task_id = req.params.get("task_id", "")
        ok = self.task_manager.cancel_task(task_id)
        return McpResponse(request_id=req.request_id, success=True,
                           data={"taskId": task_id, "cancelled": ok})

    def _handle_tasks_list(self, req: McpRequest) -> McpResponse:
        state_str = req.params.get("state")
        state = TaskState(state_str) if state_str else None
        tasks = self.task_manager.list_tasks(state=state)
        return McpResponse(request_id=req.request_id, success=True,
                           data={"tasks": tasks, "total": len(tasks)})

    # ── Prompts Handlers ──

    def _handle_prompts_list(self, req: McpRequest) -> McpResponse:
        prompts = [
            {"name": "memory_search_prompt", "description": "Template for memory search queries"},
        ]
        return McpResponse(request_id=req.request_id, success=True,
                           data={"prompts": prompts})

    def _handle_prompts_get(self, req: McpRequest) -> McpResponse:
        name = req.params.get("name", "")
        return McpResponse(request_id=req.request_id, success=True, data={
            "messages": [{"role": "user", "content": {"type": "text", "text": f"Prompt: {name}"}}],
        })

    # ── MRTR Support ──

    def handle_mrtr(self, request: McpRequest, previous_response: McpResponse) -> McpResponse:
        """处理 MRTR 多轮请求（Server → Client → Server 往返）。"""
        if request.mrt_round == 0:
            # 首轮：处理请求并标记需要继续
            response = self.handle_request(request)
            if response.success and self._needs_elicitation(response):
                response.mrt_continue = True
                response.mrt_next_prompt = self._generate_elicitation_prompt(response)
            return response
        else:
            # 后续轮：客户端已提供补充信息
            return self.handle_request(request)

    def _needs_elicitation(self, response: McpResponse) -> bool:
        return False  # 默认不需要征询

    def _generate_elicitation_prompt(self, response: McpResponse) -> str:
        return "Please provide additional context or clarification."


# ─── Convenience API ─────────────────────────────────────────────────────────

def create_mcp_memory_server(name: str = "Trinity MCP Memory Server",
                              version: str = "2.0.0") -> McpMemoryServer:
    """创建 MCP 无状态记忆服务实例。"""
    return McpMemoryServer(name=name, version=version)


def parse_mcp_request(json_body: str,
                       method: Optional[str] = None,
                       tool_name: Optional[str] = None,
                       headers: Optional[dict] = None) -> McpRequest:
    """从 JSON 请求体解析 McpRequest。

    Args:
        json_body: JSON 格式的请求体
        method: Mcp-Method header 值（如果通过 HTTP header 传递）
        tool_name: Mcp-Name header 值
        headers: 其他 HTTP headers
    """
    params = json.loads(json_body)
    req_method = method or params.pop("method", "ping")
    return McpRequest(
        method=req_method,
        tool_name=tool_name or params.pop("name", None),
        params=params,
        headers=headers or {},
    )
