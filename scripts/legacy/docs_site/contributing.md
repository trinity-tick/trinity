# Contributing

We welcome contributions from the community! Whether you're fixing bugs, adding features, improving documentation, or writing benchmarks — all contributions help make Trinity better.

---

## Code of Conduct

This project adheres to the [Contributor Covenant Code of Conduct](https://www.contributor-covenant.org/version/2/1/code_of_conduct/). By participating, you are expected to uphold this code. Please report unacceptable behavior to the project maintainers.

---

## Getting Started

### Prerequisites

- Python 3.10 or later
- Git
- PostgreSQL 16+ (for integration tests)
- Poetry or uv for dependency management

### Development Setup

```bash
# Clone the repository
git clone https://github.com/agentic-ai/trinity.git
cd trinity

# Install development dependencies
pip install -e ".[dev,test,postgres,multimodal]"

# Or using uv
uv sync --dev
```

### Verify Setup

```bash
# Run the test suite
pytest tests/

# Run linting
ruff check trinity/
ruff format --check trinity/

# Run type checking
mypy trinity/
```

---

## Development Workflow

### Branch Naming

- `feature/description` — New features
- `fix/description` — Bug fixes
- `docs/description` — Documentation changes
- `benchmark/description` — Benchmark additions
- `refactor/description` — Code refactoring

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(core): add batch memory retrieval endpoint

Implement a new batch retrieval endpoint that accepts multiple
query IDs in a single request. Reduces network overhead for
multi-query scenarios.

Closes #142
```

### Pull Request Process

1. **Create an issue** — Discuss your proposed changes before implementation.
2. **Fork the repository** — Create your own fork on GitHub.
3. **Create a feature branch** — Branch from `main`.
4. **Write tests** — Ensure your changes are covered by tests.
5. **Pass CI** — All checks must pass before review.
6. **Submit a PR** — Reference the issue number in your PR description.

---

## Coding Standards

### Python Style

Trinity follows [PEP 8](https://peps.python.org/pep-0008/) with [Ruff](https://docs.astral.sh/ruff/) enforcement:

```bash
# Check style
ruff check trinity/

# Auto-fix
ruff check --fix trinity/

# Format
ruff format trinity/
```

Key conventions:

- Use type hints for all function signatures
- Maximum line length: 100 characters
- Use `snake_case` for variables and functions
- Use `UPPER_CASE` for constants
- Use `PascalCase` for classes
- Use descriptive docstrings (Google style)

### Type Annotations

All public APIs must have complete type annotations:

```python
from typing import Optional, List, Dict, Any
from datetime import datetime

def store_memory(
    self,
    user_id: str,
    content: str,
    memory_type: str = "general",
    tenant_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Store a memory in the database.

    Args:
        user_id: Unique identifier for the user.
        content: The memory content to store.
        memory_type: Classification of the memory.
        tenant_id: Tenant identifier for isolation.
        metadata: Arbitrary key-value metadata.

    Returns:
        The unique ID of the stored memory.

    Raises:
        ValueError: If content is empty or exceeds max length.
        ConnectionError: If the database is unreachable.
    """
    ...
```

### Testing

- All new features must include tests
- Aim for 90%+ code coverage
- Use pytest with fixtures for database setup

```bash
# Run specific test file
pytest tests/test_engine.py -v

# Run with coverage
pytest --cov=trinity tests/

# Run integration tests
pytest tests/integration/ -v
```

---

## Project Structure

```
trinity/
├── trinity/                    # Main source code
│   ├── __init__.py            # Package init, version
│   ├── __main__.py            # CLI entry point
│   ├── cli.py                 # Command-line interface
│   ├── engine.py              # Core orchestration engine
│   ├── adapters/              # Storage backends
│   │   ├── base.py            # Abstract base adapter
│   │   ├── postgresql.py      # PostgreSQL + pgvector
│   │   └── sqlite.py          # SQLite (development)
│   ├── api/                   # REST API
│   │   ├── server.py          # HTTP server
│   │   └── ...
│   ├── core/                  # Core components
│   │   ├── bridge.py          # Bridge manager
│   │   └── client.py          # High-level client
│   ├── daemon/                # Background processes
│   │   ├── anti_forgetting_guard.py
│   │   └── prompt_compression_auditor.py
│   ├── mcp/                   # MCP implementation
│   │   ├── server.py          # MCP server
│   │   ├── langchain_adapter.py
│   │   └── run_server.py
│   └── benchmark/             # Benchmark suite
│       ├── runner.py
│       ├── latency_report.py
│       └── ...
├── tests/                     # Test suite
├── docs/                      # Documentation source
├── docker/                    # Docker configuration
├── examples/                  # Usage examples
├── pyproject.toml             # Project configuration
└── mkdocs.yml                 # Documentation config
```

---

## Adding a New Storage Backend

1. **Create a new adapter** in `trinity/adapters/`:

```python
# trinity/adapters/chromadb.py
from .base import BaseAdapter
from typing import List, Dict, Any, Optional

class ChromaDBAdapter(BaseAdapter):
    """Adapter for ChromaDB vector database."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Initialize ChromaDB client

    def store(self, ...) -> str:
        # Implement storage logic
        ...

    def retrieve(self, ...) -> List[Dict[str, Any]]:
        # Implement retrieval logic
        ...
```

2. **Register the backend** in `trinity/adapters/__init__.py`.
3. **Add tests** in `tests/adapters/test_chromadb.py`.
4. **Update documentation** in `docs/api-reference.md`.

---

## Adding a New Encoder

1. **Implement the encoder interface**:

```python
# trinity/encoders/my_encoder.py
from typing import List

class MyEncoder:
    """Custom encoder for a new modality."""

    model_name: str = "my-encoder"
    dimensions: int = 512

    def encode(self, data: bytes) -> List[float]:
        """Encode raw data into an embedding vector."""
        ...
```

2. **Register the encoder** in the configuration system.
3. **Add tests** for encoding and retrieval.

---

## Documentation

We use [MkDocs](https://www.mkdocs.org/) with the Material theme.

```bash
# Serve documentation locally
mkdocs serve

# Build static site
mkdocs build

# Deploy to GitHub Pages
mkdocs gh-deploy
```

### Documentation Guidelines

- Write in clear, professional English
- Use code examples for all APIs
- Include Mermaid diagrams for architecture
- Keep examples runnable and tested
- Cross-reference related pages

---

## Benchmark Contributions

We welcome new benchmark scenarios and evaluation scripts.

### Adding a Benchmark

1. Create a scenario file in `benchmark/scenarios/`:

```json
{
  "name": "custom_scenario",
  "description": "Description of what this benchmark measures",
  "config": {
    "dataset_size": 100000,
    "memory_types": ["fact", "preference"],
    "query_count": 1000
  },
  "metrics": ["latency_p50", "latency_p99", "throughput"]
}
```

2. Add the evaluator logic in `trinity/benchmark/`.
3. Run and document results.

---

## Release Process

1. **Version bump** — Update `trinity/__init__.py` and `pyproject.toml`.
2. **Changelog** — Update `CHANGELOG.md`.
3. **Tag release** — `git tag v1.2.0 && git push --tags`.
4. **Build package** — `python -m build`.
5. **Publish to PyPI** — `twine upload dist/*`.

---

## Getting Help

- **GitHub Issues** — Bug reports and feature requests
- **Discussions** — Q&A and community discussion
- **Pull Requests** — Code contributions

---

## Thank You

Every contribution, no matter how small, makes Trinity better. Thank you for contributing! 🎉
