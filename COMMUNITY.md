---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 1ed0c9b5065b1819decccf0a8be25f33_29f2324a93f011f1bcfc525400e6dd8f
    ReservedCode1: bH3fgB3IA/TONV1r4E5fR/deHB53SGT14WP0KDUphqqXKVGeiUaazRGWyZaLaek37P6LorrGCPnAWwjLBazyDBRMMbkTgEF+Ay+uaZLGi8/BwIwlVPN/VQew3/BvUGKvT1b5YnFa7uC412gnDPfVe/PuLKlFIHZIPU2i4woH0NBqSFzCkw11bne/AuU=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 1ed0c9b5065b1819decccf0a8be25f33_29f2324a93f011f1bcfc525400e6dd8f
    ReservedCode2: bH3fgB3IA/TONV1r4E5fR/deHB53SGT14WP0KDUphqqXKVGeiUaazRGWyZaLaek37P6LorrGCPnAWwjLBazyDBRMMbkTgEF+Ay+uaZLGi8/BwIwlVPN/VQew3/BvUGKvT1b5YnFa7uC412gnDPfVe/PuLKlFIHZIPU2i4woH0NBqSFzCkw11bne/AuU=
---

# Trinity Community

## License

Trinity is released under the **MIT License**. See [LICENSE](LICENSE) for full terms.

**Summary**: Trinity is free for commercial and non-commercial use. You may copy, modify, distribute, and sublicense the software, provided the original copyright notice and license text are included.

---

## Contributing Guide

We welcome contributions of all kinds: code, documentation, bug reports, feature requests, and module proposals.

### How to Contribute

1. **Fork** the repository
2. **Create a branch**: `git checkout -b feat/my-feature`
3. **Write code**: follow the project conventions below
4. **Add tests**: place in `trinity/tests/` matching the package structure
5. **Run validation**: `python -m pytest trinity/tests/`
6. **Submit a PR**: fill out the PR template with description, rationale, and test results

### Coding Conventions

| Convention | Standard |
|:-----------|:---------|
| Python version | 3.10+ |
| Type annotations | Complete; `from __future__ import annotations` |
| Docstrings | Google-style with `>>>` examples |
| Linting | ruff (line-length 120) |
| Module header | Triple-quoted docstring with paper reference |
| Thread safety | `threading.RLock` for shared state |
| Logging | `logging.getLogger(__name__)` |

### Issue Templates

- **Bug Report** → [`.github/ISSUE_TEMPLATE/bug_report.md`](.github/ISSUE_TEMPLATE/bug_report.md)
- **Feature Request** → [`.github/ISSUE_TEMPLATE/feature_request.md`](.github/ISSUE_TEMPLATE/feature_request.md)
- **Module Proposal** → [`.github/ISSUE_TEMPLATE/module_proposal.md`](.github/ISSUE_TEMPLATE/module_proposal.md)
- **Benchmark Submission** → [`.github/ISSUE_TEMPLATE/benchmark.md`](.github/ISSUE_TEMPLATE/benchmark.md)

### Pull Request Process

1. PR title follows: `[Type] Brief description` (e.g., `[Feat] Add Hebbian memory graph CB72`)
2. Include `closes #issue-number` if addressing an issue
3. All CI checks must pass (lint / type-check / test)
4. At least one maintainer review required
5. After merge, the branch is automatically deleted

---

## Code of Conduct

### Our Pledge

We pledge to make participation in Trinity a harassment-free experience for everyone, regardless of age, body size, disability, ethnicity, gender identity, experience level, nationality, personal appearance, race, religion, or sexual identity and orientation.

### Standards

**Positive behavior**:
- Using welcoming and inclusive language
- Being respectful of differing viewpoints
- Gracefully accepting constructive criticism
- Focusing on what is best for the community
- Showing empathy towards other community members

**Unacceptable behavior**:
- Trolling, insulting/derogatory comments, and personal attacks
- Publishing others' private information without consent
- Sexualized language or imagery and unwelcome advances
- Other conduct which could reasonably be considered inappropriate

### Enforcement

Violations may be reported to the project team at **trinity-conduct@googlegroups.com**. All complaints will be reviewed and investigated promptly and fairly. The project team is obligated to maintain confidentiality.

---

## Version & Release Process

Trinity follows **Semantic Versioning (SemVer)** `MAJOR.MINOR.PATCH`.

### Release Cadence

| Type | Trigger | Example |
|:-----|:--------|:--------|
| **Patch** (X.Y.Z+1) | Bug fixes, minor improvements | 6.37.0 → 6.37.1 |
| **Minor** (X.Y+1.0) | New modules, backward-compatible features | 6.37.0 → 6.38.0 |
| **Major** (X+1.0.0) | Breaking API changes, architecture overhaul | 6.x → 7.0.0 |

### Release Checklist

1. Update `trinity/__init__.py` `__version__`
2. Update `pyproject.toml` `version` field
3. Add entry to `CHANGELOG.md` with date, changes, and paper references
4. Run full test suite: `python -m pytest trinity/tests/ -x`
5. Run diagnostics: `python -m trinity diagnostics`
6. Build distribution: `python -m build`
7. Publish to PyPI: `python -m twine upload dist/*`
8. Tag release: `git tag vMAJOR.MINOR.PATCH && git push --tags`

### Module Versioning

Each second_brain module encodes its own version in the file header comment (e.g., `# Module: CB72-HebbianMemoryGraph v1.0`). The aggregate version in `trinity/__init__.py` reflects the latest module addition.

---

## Governance

Trinity is maintained by the **Trinity Tick** team with input from the open-source community.

- **Maintainers**: Review PRs, manage releases, triage issues
- **Contributors**: Submit code, docs, and bug reports
- **Community**: Use Trinity, share feedback, participate in discussions

### Decision Making

Decisions are made through [RFC (Request for Comments)](docs/rfc/) process for significant changes. Lazy consensus applies to minor changes: if no objections within 72 hours, the change is accepted.

---

## Contact

- **GitHub Issues**: [github.com/trinity-tick/trinity/issues](https://github.com/trinity-tick/trinity/issues)
- **Discussions**: [github.com/trinity-tick/trinity/discussions](https://github.com/trinity-tick/trinity/discussions)
- **Documentation**: [trinity-tick.github.io/trinity](https://trinity-tick.github.io/trinity)
- **PyPI**: [pypi.org/project/trinity-memory](https://pypi.org/project/trinity-memory/)
*（内容由AI生成，仅供参考）*
