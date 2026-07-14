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
