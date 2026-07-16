# Trinity 架构

## 整体设计
Trinity 是一个**记忆系统**，提供：
- 多层记忆（工作记忆、情景记忆、语义记忆）
- 嵌入/检索
- 知识图谱
- MCP 集成
- 多租户

## 运行时
- Python 同步进程 + FastAPI
- 每个请求同步阻塞
- 单进程模型

## 存储层
- 默认 SQLite（单文件）
- 可选 PostgreSQL
- JSON 文件用于会话日志/演化状态
- ChromaDB 用于向量

## 集成
- MCP Server 模式（提供工具/资源给客户端）
- LangChain Adapter
- Ollama 本地嵌入