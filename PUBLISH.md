# Trinity 发布指南

## 前置条件

1. GitHub 账号: https://github.com
2. PyPI 账号: https://pypi.org

---

## 第一步：创建 GitHub 仓库

在浏览器中打开 https://github.com/new

- **Repository name**: `trinity`
- **Description**: `Trinity — A Triune Architecture for AGI Long-Term Memory (122 modules, 50-tier guardian chain, 47 retrieval channels)`
- **Visibility**: Public
- **Do NOT** initialize with README, .gitignore, or license

点击 **Create repository**。

---

## 第二步：推送代码

```bash
cd C:\Users\Administrator\trinity

# 设置远程仓库
git remote add origin https://github.com/YOUR_USERNAME/trinity.git

# 推送到 main 分支
git branch -M main
git push -u origin main
```

> 将 `YOUR_USERNAME` 替换为你的 GitHub 用户名。

---

## 第三步：创建 GitHub Release

在浏览器中打开 https://github.com/YOUR_USERNAME/trinity/releases/new

- **Tag**: `v6.37.0`
- **Title**: `Trinity v6.37.0 — Initial Release`
- **Description**:

```markdown
Trinity v6.37.0 — 第一个正式发布版本。

### 核心能力
- 122 模块内存引擎（Exabase, Hindsight, Zikkaron, SelfMem）
- 50 级守护链（L1-L50，含推理漂移检测）
- 47 路检索通道（渐进级联：0.05ms P50）
- 多租户隔离（persona_id/session_id/tenant_id）

### 接口
- Python API: `pip install trinity-memory` → `from trinity import Trinity`
- CLI: `python -m trinity search/ingest/diagnostics/mcp/bench`
- MCP Server (stdio + SSE)
- FastAPI REST API (8 endpoints)
- Web Dashboard (HTML/JS)

### 部署
- Docker: `docker compose up -d`
- 多模态: 图像/音频/文本统一记忆
- PostgreSQL 多租户支持

### 质量
- 45 单元测试全部通过
- 检索延迟 P50=21ms（比 Mem0 快 5-12x）
- 开放域推理（BeliefNetwork 证据/推理分离）
- API Key 认证（Bearer token）
```

---

## 第四步：发布到 PyPI（可选）

```bash
# 安装构建工具
pip install build twine

# 构建 trinity-memory 包
python -m build

# 上传到 PyPI
python -m twine upload dist/*

# 构建 auto-daemon 包
cd auto-daemon
python -m build
python -m twine upload dist/*
```

---

## 第五步：启用 GitHub Pages 文档站

在浏览器中打开 https://github.com/YOUR_USERNAME/trinity/settings/pages

- **Source**: GitHub Actions
- 然后 `git push` 会触发 mkdocs.yml 自动构建

或者手动构建：

```bash
pip install mkdocs-material
cd C:\Users\Administrator\trinity
mkdocs build
# 将 site/ 目录部署到 gh-pages 分支
```

---

## 第六步：验证

```bash
# 安装
pip install trinity-memory

# 测试
python -c "from trinity import Trinity; print(Trinity().diagnostics().get('trinity_version'))"

# CLI
python -m trinity diagnostics
python -m trinity search --query "hello" --top-k 3
python -m trinity bench --name mock
```
