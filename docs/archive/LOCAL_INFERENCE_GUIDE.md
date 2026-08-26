# 本地推理降级指南（2026-08-24）

> Trinity 默认用 DeepSeek API 做 QA/提取/压缩（DEEPSEEK_API_KEY）。
> 本指南说明如何切换到 Ollama 本地推理——用于**批量/离线/隐私敏感**场景
> （合规记忆提取、无外网环境、数据不出域）。
> 实测：Ollama qwen3:8b 本地提取可用（~69s/条，慢——实时 QA 不建议）。

---

## 一、快速切换

```bash
# 环境变量（服务/脚本继承）
set TRINITY_LLM_BASE_URL=http://127.0.0.1:11434/v1
set TRINITY_LLM_MODEL=qwen3:8b-iq3m-8k
set DEEPSEEK_API_KEY=local-only  # 占位（本地不需要真实 key）

# 或写入 ~/.dsh/.credentials.yaml（maintenance/supervisor 注入）
# TRINITY_LLM_BASE_URL: http://127.0.0.1:11434/v1
# TRINITY_LLM_MODEL: qwen3:8b-iq3m-8k
```

生效范围：RouteReasoner（QA）、proposition_extractor（命题提取）、
memory_compressor（压缩摘要）、offload（卸载摘要）——所有走
`trinity/llm/client.py` 的路径统一切换。

## 二、适用场景（实测依据）

| 场景 | 建议 |
|---|---|
| **批量/离线提取**（无外网、合规隔离） | ✅ 本地（慢但可用，69s/条） |
| **隐私敏感压缩**（数据不出域） | ✅ 本地 |
| **实时 QA**（用户交互） | ❌ 保持 DeepSeek API（本地 69s 不可接受） |
| **混合模式** | 写路径（提取/压缩）可本地异步；读路径（QA）保持 API |

## 三、验证

```bash
# 本地 LLM 连通性
curl http://127.0.0.1:11434/api/chat -d '{"model":"qwen3:8b-iq3m-8k","messages":[{"role":"user","content":"hi"}],"stream":false}'

# Trinity 走本地（LLM 适配层自动用 TRINITY_LLM_BASE_URL）
python -m trinity search --query "测试" --top-k 3
```

## 四、注意

- **性能**：qwen3:8b 本地提取 ~69s/条（CPU）；GPU 或更小模型（qwen3:4b）
  更快但质量降；
- **embedding 已本地**：bge-m3 本就在 Ollama（1024 维）——本地推理切换
  后全链路不出域；
- **缓存/前缀管理**：本地模型无 prompt cache 折扣——长前缀场景成本不降
  （本地本来无 API 计费，仅延迟换成本）；
- **回滚**：删除/注释 TRINITY_LLM_BASE_URL 即恢复 DeepSeek API。
