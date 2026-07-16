# Trinity v6.37 快速运行指南

# ============================================================
# 方式一：CLI 命令行 (最常用)
# ============================================================

# 1. 打开 CMD / PowerShell
cd C:\Users\Administrator\trinity

# 2. 查看所有可用命令
python -m trinity --help

# 3. 全系统诊断（验证一切正常）
python -m trinity diagnostics

# 4. 语义嵌入（测试 bge-m3 真实语义）
python -m trinity embed --text "Alice likes hiking" --compare "mountain climbing"
# → 输出 cosine_similarity: 0.9173 (高度相关！)

# 5. 写入记忆
python -m trinity ingest --content "彩棠重品放第一层" --category warehouse --importance 0.9

# 6. 检索记忆
python -m trinity search --query "彩棠货位规则" --top-k 5

# 7. 向量搜索（bge-m3 + numpy 向量索引）
python -m trinity vector search --query "货架布局设计" --top-k 10


# ============================================================
# 方式二：Python 交互式 (最灵活)
# ============================================================

python
>>> import sys; sys.path.insert(0, r"C:\Users\Administrator\trinity")
>>> 
>>> # 嵌入引擎
>>> from trinity.embeddings import create_engine
>>> eng = create_engine(backend="auto", use_cache=True)
>>> v1 = eng.embed("Alice likes hiking")
>>> v2 = eng.embed("Bob works at Google")
>>> eng.cosine_similarity(v1, v2)
0.4313  # 不同主题 → 低分
>>>
>>> # 向量索引
>>> from trinity.vector_index import create_index
>>> import numpy as np
>>> idx = create_index(backend="numpy", dim=1024)
>>> idx.add("m1", v1, {"content": "Alice likes hiking"})
>>> idx.add("m2", v2, {"content": "Bob works at Google"})
>>> q = eng.embed("outdoor activities")
>>> results = idx.search(q, top_k=2)
>>> for r in results: print(r.metadata)
>>>
>>> # 进化引擎
>>> from trinity.evolution import MetaEvolution
>>> evo = MetaEvolution()
>>> evo.tick({"session": "当前工作"})  # Observe 阶段
>>> evo.tick()  # Analyze
>>> evo.tick()  # Plan
>>> evo.tick()  # Execute
>>> evo.tick()  # Certify → 完成一个进化周期！
>>>
>>> # 懒加载 SecondBrain
>>> from trinity.modules.second_brain.loader import SecondBrainLoader
>>> from trinity.modules.second_brain.registry import reset_registry
>>> reset_registry()
>>> loader = SecondBrainLoader(lazy=True)
>>> loader.guardian_chain.total  # 50
>>> loader.retrieval.total      # 47


# ============================================================
# 方式三：MCP 服务器 (供其他Agent调用 — Goose/Claude/Coze)
# ============================================================

# 终端1: 启动 MCP 服务器 (SSE 模式)
cd C:\Users\Administrator\trinity
python -m trinity mcp --mode sse --port 8000

# 其他 Agent 通过 MCP 协议连接后可用工具:
#   - trinity_search
#   - trinity_ingest
#   - trinity_diagnostics
#   - trinity_detect_contradiction
#   - + evolution 工具（若 mcp_adapter 已启用）
#     evolution_tick, evolution_diagnostics, evolution_save_state

# 或者 stdio 模式 (嵌入到 Goose/Claude 配置中):
python -m trinity mcp --mode stdio
# 在 goose_config.yaml 中:
# extensions:
#   trinity:
#     command: python -m trinity mcp --mode stdio
#     type: stdio


# ============================================================
# 方式四：REST API (Web 服务)
# ============================================================

# 终端1: 启动 REST API
cd C:\Users\Administrator\trinity
python trinity\api\server.py --port 8001

# 终端2: 调用 API
curl http://localhost:8001/health
curl http://localhost:8001/diagnostics

# 语义嵌入
curl -X POST http://localhost:8001/embeddings ^
  -H "Content-Type: application/json" ^
  -d '{"text": "Alice likes hiking", "backend": "auto"}'

# 向量搜索
curl -X POST http://localhost:8001/vector/search ^
  -H "Content-Type: application/json" ^
  -d '{"query": "hiking preferences", "top_k": 5}'

# 索引记忆到向量库
curl -X POST http://localhost:8001/vector/index ^
  -H "Content-Type: application/json" ^
  -d '{"backend": "auto"}'

# 写入记忆
curl -X POST http://localhost:8001/memories ^
  -H "Content-Type: application/json" ^
  -d '{"content": "彩棠重品放第一层", "category": "warehouse", "importance": 0.9}'

# 开放 API 文档
# http://localhost:8001/docs


# ============================================================
# 方式五：自我进化 (自动运行)
# ============================================================

# Trinity 自我进化引擎在你每次工作时自动运行:
python
>>> from trinity.evolution import MetaEvolution, CrossPlatformAdapter
>>> evo = MetaEvolution()
>>> 
>>> # 每次工作时运行 tick
>>> evo.tick({"session": "设计彩棠货架", "type": "warehouse"})
>>> 
>>> # 跨窗口交接 — 生成 handoff 文件
>>> cpa = CrossPlatformAdapter()
>>> hp = cpa.prepare_handoff(evo.diagnostics())
>>> print(f"另一个窗口读取: {hp}")
>>>
>>> # 保存状态
>>> evo.save_state()


# ============================================================
# 一键启动 (如果想全部跑起来)
# ============================================================

# 在 CMD 中:
start "Trinity API" python trinity\api\server.py --port 8001
start "Trinity MCP" python -m trinity mcp --mode sse --port 8000

# 然后打开浏览器:
#   Dashboard:  http://localhost:8001/
#   API Documentation: http://localhost:8001/docs
#   MCP: ws://localhost:8000 (SSE模式)
