# Trinity Documentation

## A Persistent Memory Layer for AI Agents

Trinity 是一个三位一体长程记忆系统，为 AI Agent 提供持久化记忆能力。

### 快速安装

```bash
pip install trinity-memory
```

### 快速使用

```python
from trinity import Trinity

mem = Trinity()
mem.ingest("用户偏好暗色模式", tags=["preference", "ui"])
results = mem.search("用户偏好")
print(results)
```

### 核心能力

| 功能 | 说明 |
|:-----|:------|
| **长程记忆** | 50 级守护链，自动遗忘防护与压缩审计 |
| **多模态支持** | 文本、图像、音频统一记忆 |
| **多租户隔离** | persona_id / session_id / tenant_id |
| **47 路检索** | 语义、图谱、精确、混合渐进级联 |
| **MCP 集成** | 标准 Model Context Protocol 接口 |
| **自演化** | Auto-curricula / Engram Memory / Consolidation Sleep |

### 链接

- [GitHub](https://github.com/trinity-tick/trinity)
- [PyPI](https://pypi.org/project/trinity-memory/)
- [API 参考](api-reference.md)
- [架构说明](architecture.md)
