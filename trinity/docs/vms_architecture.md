# Trinity VMS Architecture

**版本**: v8.2.0  
**定位**: Agent 操作系统的标准化可插拔记忆基础设施

---

## 1. 架构概览

```
 ┌──────────────────────────────────────────────────────────┐
 │                    Agent Frameworks                       │
 │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
 │  │LangChain │  │ CrewAI   │  │ AutoGen  │  │  Marvis  │ │
 │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘ │
 │       │ Adapter      │ Adapter     │ Adapter     │ Adapter│
 └───────┼──────────────┼─────────────┼─────────────┼───────┘
         │              │             │             │
 ┌───────▼──────────────▼─────────────▼─────────────▼───────┐
 │                  VMS (Core Facade)                       │
 │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐  │
 │  │ Registry │ │ Config   │ │ Shutdown │ │ Hot-Swap    │  │
 │  └──────────┘ └──────────┘ └──────────┘ └────────────┘  │
 └───────┬──────────────┬─────────────┬─────────────────────┘
         │              │             │
 ┌───────▼───────┐ ┌───▼──────┐ ┌───▼────────────┐
 │   Protocols   │ │Backends  │ │    Adapters    │
 │ (interfaces)  │ │          │ │                │
 │               │ │ SQLite   │ │ LangChain      │
 │ MemoryStore   │ │ Postgres │ │ CrewAI         │
 │ IdentityProv  │ │ In-Memory│ │ AutoGen        │
 │ Auditor       │ │          │ │                │
 │ TaskBroker    │ │          │ │                │
 │ SearchEngine  │ │          │ │                │
 │ Comp Engine   │ │          │ │                │
 └───────────────┘ └──────────┘ └────────────────┘
```

---

## 2. 核心接口（Protocol 类）

所有接口均为 `runtime_checkable` Protocol 类，任何实现相同方法签名的对象均可通过 duck-typing 接入，无需显式继承。

### 2.1 MemoryStore — 记忆存储协议

```python
class MemoryStore(Protocol):
    def add(content, agent_id, persona_id, session_id, tenant_id,
            role, importance, tags, category) -> Dict[str, Any]: ...
    def get(memory_id: str) -> Optional[Dict[str, Any]]: ...
    def search(query, top_k, agent_id, persona_id, tenant_id) -> List[Dict[str, Any]]: ...
    def delete(memory_id: str, soft: bool = True) -> bool: ...
    def count(agent_id=None, tenant_id=None) -> int: ...
```

**已实现后端**: SQLiteVMSBackend · PostgresBackend · InMemoryBackend

### 2.2 IdentityProvider — 身份协议

```python
class IdentityProvider(Protocol):
    def register(agent_id, anchors, metadata) -> Dict[str, Any]: ...
    def get_profile(agent_id) -> IdentityProfile: ...
    def detect_drift(agent_id) -> DriftReport: ...
    def rebuild(agent_id, from_bundle) -> IdentityBundle: ...
```

### 2.3 Auditor — 审计协议

```python
class Auditor(Protocol):
    def audit(action, context) -> AuditResult: ...
    def get_violations(agent_id=None, limit=50) -> List[Violation]: ...
    def get_trust_score(agent_id) -> float: ...
```

### 2.4 TaskBroker — 任务调度协议

```python
class TaskBroker(Protocol):
    def create_task(description, from_agent, to_agent, payload) -> TrinityTask: ...
    def query_task(task_id) -> Optional[TrinityTask]: ...
    def cancel_task(task_id) -> bool: ...
    def list_tasks(status=None, limit=50) -> List[TrinityTask]: ...
```

### 2.5 SearchEngine — 检索引擎协议

```python
class SearchEngine(Protocol):
    def search(query, top_k, filters) -> SearchResult: ...
    def hybrid_search(query, top_k, strategy) -> SearchResult: ...
    def cross_modal_search(query, query_type, top_k) -> SearchResult: ...
```

### 2.6 CompressionEngine — 压缩协议

```python
class CompressionEngine(Protocol):
    def compress(agent_id, memories) -> CompressedContext: ...
    def restore(agent_id, trimmed_ids) -> Dict[str, Any]: ...
    def get_stats() -> Dict[str, Any]: ...
```

---

## 3. 全局注册表（VMRegistry）

线程安全的注册表支持运行时热切换：

```python
from trinity.vms import get_registry

reg = get_registry()

# 注册后端
reg.register("my_store", "memory_store", MyCustomBackend())

# 获取默认后端
store = reg.get("memory_store")

# 列出所有已注册实现
reg.list_backends("memory_store")

# 热切换到指定后端
reg.switch_backend("memory_store", "postgres")
```

---

## 4. 框架适配器

每个适配器实现 `FrameworkAdapter` 抽象基类：

| 适配器 | 格式转换 | 额外能力 |
|---|---|---|
| **LangChainAdapter** | AgentAction ↔ TrinityTask | BaseChatMemory→Trinity 记忆导入 |
| **CrewAIAdapter** | Crew Task ↔ TrinityTask | Crew 级共享记忆池 |
| **AutoGenAdapter** | AutoGen Message ↔ TrinityTask | GroupChat 多 Agent 记忆协调 |

### 开发新适配器

```python
from trinity.vms import FrameworkAdapter, TrinityTask, TrinityResult

class MyFrameworkAdapter(FrameworkAdapter):
    framework_name = "my_framework"

    def to_trinity_format(self, agent_name, framework_task) -> TrinityTask:
        ...
    def from_trinity_format(self, trinity_result) -> dict:
        ...
```

---

## 5. 后端切换

### 配置文件方式

```python
from trinity.vms import VMS

vms = VMS.from_config("vms_config.yaml")
```

### 代码方式

```python
vms = VMS.from_defaults()
vms.use_memory(backend="postgres")     # 切换到 PostgreSQL
vms.use_search(backend="cross_modal")  # 切换到跨模态检索
```

### 环境变量方式

```bash
export DATABASE_URL=postgresql://user:pass@localhost:5432/trinity
export TRINITY_VMS_SEARCH_ENGINE=hybrid
```

---

## 6. 快速开始

```python
from trinity import Trinity

mem = Trinity()

# 通过 VMS 接口
vms = mem.vms
store = vms.memory_store
store.add("User prefers dark mode", agent_id="assistant-1")

# 挂载 LangChain 适配器
adapter = vms.connect_adapter(framework="langchain")

# 搜索
results = store.search("dark mode", top_k=5)
```
