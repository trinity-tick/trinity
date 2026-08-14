"""
Trinity Code Analyzer — lightweight code semantic analysis for code-modality memories.

Extracts function names, class names, import statements, and line counts
to auto-populate metadata during code memory ingestion.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List


# ── Language-specific patterns ──────────────────────────────────────

_PYTHON_FUNC_PATTERN = re.compile(
    r'^\s*(?:async\s+)?def\s+(\w+)\s*\(', re.MULTILINE
)
_PYTHON_CLASS_PATTERN = re.compile(
    r'^\s*class\s+(\w+)\s*[(:]', re.MULTILINE
)
_PYTHON_IMPORT_PATTERN = re.compile(
    r'^\s*(?:from\s+(\S+)\s+)?import\s+(.+)$', re.MULTILINE
)

_JS_FUNC_PATTERN = re.compile(
    r'(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\()',
    re.MULTILINE
)
_JS_CLASS_PATTERN = re.compile(r'^\s*class\s+(\w+)', re.MULTILINE)
_JS_IMPORT_PATTERN = re.compile(
    r'(?:import\s+.*?\s+from\s+[\'"]([^\'"]+)[\'"]|require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\))',
    re.MULTILINE
)

_GO_FUNC_PATTERN = re.compile(r'^\s*func\s+(\w+)\s*\(', re.MULTILINE)
_GO_TYPE_PATTERN = re.compile(r'^\s*type\s+(\w+)\s+struct', re.MULTILINE)
_GO_IMPORT_PATTERN = re.compile(r'"([^"]+)"', re.MULTILINE)

_RUST_FN_PATTERN = re.compile(r'^\s*(?:pub\s+)?fn\s+(\w+)\s*[<(]', re.MULTILINE)
_RUST_STRUCT_PATTERN = re.compile(r'^\s*(?:pub\s+)?struct\s+(\w+)', re.MULTILINE)
_RUST_IMPL_PATTERN = re.compile(r'^\s*impl\s+(\w+)', re.MULTILINE)
_RUST_USE_PATTERN = re.compile(r'^\s*use\s+(.+);', re.MULTILINE)


def analyze_code(content: str, language: str = "python") -> Dict[str, Any]:
    """Analyze a code snippet and extract structured metadata.

    Args:
        content: Source code text.
        language: Programming language identifier.

    Returns:
        Dict with keys: functions, classes, imports, loc.
    """
    lines = content.splitlines()
    loc = len(lines)

    handlers = {
        "python": _analyze_python,
        "py": _analyze_python,
        "javascript": _analyze_javascript,
        "js": _analyze_javascript,
        "typescript": _analyze_javascript,
        "ts": _analyze_javascript,
        "go": _analyze_go,
        "golang": _analyze_go,
        "rust": _analyze_rust,
        "rs": _analyze_rust,
    }

    handler = handlers.get(language.lower(), _analyze_generic)
    result = handler(content)
    result["loc"] = loc
    return result


def _analyze_python(content: str) -> Dict[str, Any]:
    functions = [m.group(1) for m in _PYTHON_FUNC_PATTERN.finditer(content)]
    classes = [m.group(1) for m in _PYTHON_CLASS_PATTERN.finditer(content)]
    imports = _extract_python_imports(content)
    return {"functions": list(dict.fromkeys(functions)), "classes": list(dict.fromkeys(classes)), "imports": imports}


def _extract_python_imports(content: str) -> List[str]:
    results = []
    for m in _PYTHON_IMPORT_PATTERN.finditer(content):
        module = m.group(1)
        names = m.group(2)
        if module:
            # 'from X import Y' → module name is X
            results.append(module)
        else:
            # 'import X, Y' → split items
            for item in names.split(","):
                cleaned = item.strip().split(" as ")[0].strip().split(".")[0]
                if cleaned:
                    results.append(cleaned)
    return list(dict.fromkeys(results))


def _analyze_javascript(content: str) -> Dict[str, Any]:
    functions = []
    for m in _JS_FUNC_PATTERN.finditer(content):
        name = m.group(1) or m.group(2)
        if name:
            functions.append(name)
    classes = [m.group(1) for m in _JS_CLASS_PATTERN.finditer(content)]
    imports: List[str] = []
    for m in _JS_IMPORT_PATTERN.finditer(content):
        pkg = m.group(1) or m.group(2)
        if pkg:
            imports.append(pkg)
    return {"functions": list(dict.fromkeys(functions)), "classes": list(dict.fromkeys(classes)), "imports": list(dict.fromkeys(imports))}


def _analyze_go(content: str) -> Dict[str, Any]:
    functions = [m.group(1) for m in _GO_FUNC_PATTERN.finditer(content)]
    types = [m.group(1) for m in _GO_TYPE_PATTERN.finditer(content)]

    # Extract import strings
    in_import_block = False
    imports: List[str] = []
    for line in content.splitlines():
        if line.strip() == "import (":
            in_import_block = True
            continue
        if in_import_block:
            if line.strip() == ")":
                in_import_block = False
                continue
            m = _GO_IMPORT_PATTERN.search(line)
            if m:
                imports.append(m.group(1))

    return {"functions": list(dict.fromkeys(functions)), "classes": list(dict.fromkeys(types)), "imports": list(dict.fromkeys(imports))}


def _analyze_rust(content: str) -> Dict[str, Any]:
    functions = [m.group(1) for m in _RUST_FN_PATTERN.finditer(content)]
    structs = [m.group(1) for m in _RUST_STRUCT_PATTERN.finditer(content)]
    impls = [m.group(1) for m in _RUST_IMPL_PATTERN.finditer(content)]
    uses = []
    for m in _RUST_USE_PATTERN.finditer(content):
        uses.append(m.group(1).strip().rstrip(";"))

    return {
        "functions": list(dict.fromkeys(functions)),
        "classes": list(dict.fromkeys(structs + impls)),
        "imports": list(dict.fromkeys(uses)),
    }


def _analyze_generic(content: str) -> Dict[str, Any]:
    """Fallback: basic line count, no semantic extraction."""
    return {"functions": [], "classes": [], "imports": []}
