# Trinity MCP 上架速填表（2026-08-24 核验版）

> 用途：注册 mcp.so / Smithery 时照抄本表。所有值已在本机核验
> （PyPI 8.2.0 已发布、trinity-mcp 在 PATH、三形态验证全 PASS）。
> 唯一注意：**npm 版 `@trinity/mcp` 不存在（registry 404）**——安装命令
> 只写 pip，不要写 "npx -y @trinity/mcp"。

---

## 一、mcp.so（https://mcp.so — 免费最快，GitHub OAuth 注册）

| 表单项 | 填写值（照抄） |
|---|---|
| 名称 | trinity-memory |
| 类型 / Category | Memory & Knowledge |
| 描述 (Description) | Cross-session long-term memory OS for AI agents (CRDT+audit chain, hybrid retrieval, knowledge graph) |
| 安装命令 (Install) | `pip install trinity-memory` |
| 启动命令 / 入口 | `trinity-mcp --mode stdio` |
| 运行方式 | stdio（本地）／远程 `https://<host>:8003/mcp`（streamable-http, Bearer） |
| 环境变量 (Env Vars) | TRINITY_MCP_API_KEY / TRINITY_API_KEY / GATEWAY_API_KEY / TRINITY_STORE |
| 仓库链接 (Repository) | https://github.com/trinity-tick/trinity |
| 标签 (Tags) | memory, rag, agent, knowledge-graph, sqlite |
| 许可证 (License) | MIT |


### mcp.so 发布方式（2026-08-24 实查 submit 页）

| 选项 | 费用 | 权益 | 建议 |
|---|---|---|---|
| 免费（审核制） | $0 | 提交后人工审核，通过后上架 | ✅ **先走这个**——技术侧已验证，审核通过只是时间问题 |
| 付费提交（Premium） | **$39 一次性** | 免审核立即上线 + Verified 标识 + 精选优先展示 + dofollow 项目链接 | 想要标识/SEO 外链（DR 72 域名）再付 |

> 表单字段：类型（MCP Server / Remote Server / MCP Client / AI Agent）→ Repository URL* → Name →
> 发布速度选择（免费审核 vs $39 付费）。提交按钮旁可开 ticket 咨询。

> 建议路径：先免费提交（0 成本）→ 若想要精选位/验证标识/SEO dofollow 外链，再补 $39。

### mcp.so 可选补充（从 README 提炼）
- 工具：memory_search / memory_write / memory_update / memory_delete / audit_query / memory_tag_search / trinity_diagnostics / memory_chronicle（9 个）
- 中文检索：BM25 + jieba 分词 + 向量 + 知识图谱多通道融合
- 可证明性：SHA-256 审计链 + CRDT 版本链，audit_query 可溯源

---

## 二、Smithery（https://smithery.ai — 第二大市场，GitHub 登录）

| 表单项 | 填写值（照抄） |
|---|---|
| 名称 | trinity-memory |
| 描述 | Cross-session long-term memory OS for AI agents (47-channel framework, CRDT+audit, hybrid retrieval) |
| 形态 | stdio（Python 包 / Docker 托管均可） |
| 安装命令 | `pip install trinity-memory` |
| 标签 | memory, rag, agent, knowledge-graph, sqlite |
| 仓库 | https://github.com/trinity-tick/trinity |
| 许可 | MIT |

### Smithery 托管注意事项
- 选 "Deploy a Server" → Python 包 `trinity-memory`；或用 Dockerfile（Smithery 托管容器，适合远程部署）。
- 环境变量按需填：TRINITY_MCP_API_KEY（强烈建议设置，否则远程无鉴权）、TRINITY_STORE。

---

## 三、上架前验证结果（2026-08-24 实测，全部 PASS）

| 形态 | 命令 | 结果 |
|---|---|---|
| stdio | `python scripts/verify_mcp_server.py --transport stdio` | PASS — initialize + 9 tools |
| SSE | `python scripts/verify_mcp_server.py --transport sse --port 8000` | PASS — initialize + 9 tools |
| streamable-http | `python scripts/verify_mcp_server.py --transport streamable-http --port 8003 --key <GATEWAY_API_KEY>` | PASS — well-known 200 / 无 token 401 / initialize 200 |

> 注意：:8003 鉴权默认开启。用 GATEWAY_API_KEY 验证通过；
> 无 key 时 server 自动降级无鉴权（仅限本机）。上架给公网用务必设置 key。

---

## 四、PyPI 状态（已核验，无需重新发布）

- PyPI 最新版：**8.2.0**（2026-08-24 查询 pypi.org/pypi/trinity-memory/json）
- 本地 pyproject.toml：**8.2.0** — 一致，**不需要再 build/upload**。
- 若未来发新版：`python -m build` → `twine upload dist/*`（需 PyPI API token，只有你能做）。

---


## 六、Tier-1 Official MCP Registry（2026-08-24 已备料）

> mcp.so 只是 Tier-3（无客户端集成/无使用数据，攻略建议 skip）。真正的高价值位是
> **Official MCP Registry**（VS Code 原生渲染 @mcp）、**Context7**（周 114 万 npm 下载）、
> **Anthropic Connectors**（Claude 产品内展示）。mcp.so 审核期间并行做这些。

### 已完成的备料（本机实测）

| 项 | 状态 | 说明 |
|---|---|---|
| `README.md` mcp-name 标记 | ✅ 已加 | `<!-- mcp-name: io.github.trinity-tick/trinity-memory -->`（PyPI 所有权验证必需） |
| `pyproject.toml` 入口别名 | ✅ 已加 | 新增 `trinity-memory = "trinity.mcp.server:main"`（registry 按包名找命令） |
| `server.json` | ✅ 已建并校验 | name/描述 92字/包声明/仓库 id=1302776755，全部符合官方 schema |
| mcp-publisher CLI | ⚠️ 已下载但本机崩溃 | Go 二进制 0xC0000005；可用 GitHub Actions 方式替代 |

### server.json 内容（docs 留存）

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.trinity-tick/trinity-memory",
  "title": "Trinity Memory OS",
  "description": "Cross-session long-term memory for AI agents: hybrid retrieval, CRDT+audit, knowledge graph.",
  "websiteUrl": "https://trinity-tick.github.io/trinity/",
  "repository": { "url": "https://github.com/trinity-tick/trinity", "source": "github", "id": "1302776755" },
  "version": "8.2.0",
  "packages": [
    {
      "registryType": "pypi",
      "registryBaseUrl": "https://pypi.org",
      "identifier": "trinity-memory",
      "version": "8.2.0",
      "transport": { "type": "stdio" },
      "packageArguments": [ { "type": "named", "name": "--mode", "value": "stdio" } ]
    }
  ]
}
```

### 关键约束

1. **必须重新发 PyPI 才能提交**——所有权验证检查的是 PyPI 上的 README，
   当前 8.2.0 的 README 无 mcp-name 标记。需要：`python -m build` + `twine upload`（你的 token）
   （版本可保持 8.2.0 重发，或升 8.2.1）；
2. **远程端点暂不申报**：`mcp.trinity-tick.dev` 域名不存在（DNS 解析失败），
   server.json 只含已验证的 PyPI stdio 包；远程 remotes 待部署后补；
3. **发布方式二选一**：
   - a) `mcp-publisher login github` + `mcp-publisher publish`（本机 CLI 崩溃，可用 GitHub Actions 跑）
   - b) GitHub Actions 自动化（registry 官方支持 OIDC，见 docs/github-actions）

### 仍需你（人类）

1. **PyPI 重新发布**（含新 README 标记 + trinity-memory 别名）——需要你的 API token；
2. **GitHub 授权**：`mcp-publisher login github` 设备码授权，或批准 CI 发布 Action；
3. **Context7 提交**：https://context7.com/add-library 提交 docs 站点
   （https://trinity-tick.github.io/trinity/，200 已验证；填 5-10 个目标查询）。

## 五、仍需你（人类）操作的三件事

1. **注册 mcp.so**（GitHub OAuth 登录即可）→ "Submit Server" → 照抄第一节；
2. **注册 Smithery**（GitHub 登录）→ "Deploy a Server" → 照抄第二节；
3. **（仅当发新版时）PyPI token 发布** —— 当前 8.2.0 已是最新，跳过。

> mcp.so 会自动对 stdio 做 smoke test；Smithery 会起容器验证。
> 两者都验证通过即上架。本表即"一页速填表"，可直接对照填写。