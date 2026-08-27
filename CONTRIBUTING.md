# Contributing to Trinity

We welcome contributions! Here's how to get started.

## Development Setup

```bash
git clone https://github.com/trinity-memory/trinity.git
cd trinity
pip install -e ".[dev,test]"
```

## Code Structure

```
trinity/
├── __init__.py          # Package entry, exports Trinity
├── __main__.py          # python -m trinity
├── cli.py              # CLI interface
├── core/
│   ├── client.py       # Trinity unified client
│   └── bridge.py       # Legacy bridge for Marvis
├── modules/
│   ├── second_brain/   # Engine (122 modules)
│   └── chromadb/       # ChromaDB integration
├── daemon/             # Auto daemon (50-tier guard)
├── mcp/                # MCP server
│   ├── server.py
│   ├── tools/
│   ├── resources/
│   └── prompts/
└── benchmark/          # Benchmark suite
```

## Pull Request Process

1. Fork the repository
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Run tests (`pytest`)
4. Submit PR with description of changes

## Coding Standards

- Follow PEP 8
- Type hint all public APIs
- Document with Google-style docstrings
- Keep modules focused (one class per file preferred)

## Release Process

Trinity follows semver: `MAJOR.MINOR.PATCH`

- `MAJOR`: Architecture-level changes
- `MINOR`: New modules / capabilities
- `PATCH`: Bug fixes, performance improvements

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

## 工程纪律（2026-08-27 开源就绪追加）

1. **每个改动可回滚**：分逻辑提交（feat/test/docs/chore），改动记录进 `dsh-ops/EXECUTION.md`（含回滚方式）；
2. **ps1 文件保持 UTF-8 BOM + CRLF**（PS 5.1 无 BOM 按 ANSI 读会吞换行破坏语法）；改 .ps1 走 git diff/checkout，勿反复脚本补丁（历史教训：定义丢失）；
3. **temp/ 用完即删**（补丁/诊断脚本不进仓库）；
4. **评测必须带 manifest**：结果保存自动生成 `<name>.manifest.json`（code_hash/env/dataset 哈希/params）——可复现、防口径漂移；
5. **生成侧提示词改动必须全量 A/B**（小样本无区分度——v6 负优化教训：MS 0.237→0.037 后回滚）；
6. 数据全本地、无遥测；勿提交：`trinity.yaml`、`.credentials*`、`benchmark/private_holdout*.json`、`~/.trinity/**`；
7. 新增功能 Checklist：默认关闭可回滚 → 单元测试 → eval 断言/基准（带 manifest）→ EXECUTION.md → Trinity 记忆沉淀。
