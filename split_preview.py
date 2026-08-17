import ast
SRC = r"trinity/adapters/sqlite.py"
src = open(SRC, encoding="utf-8").read()
tree = ast.parse(src)
mod_names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
print("module-level defs:", mod_names)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SQLiteAdapter")
methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
print("method count:", len(methods))
for m in methods:
    deco = ", ".join(ast.unparse(d) for d in m.decorator_list) or "-"
    print(f"{m.lineno:5d}-{m.end_lineno:5d}  {m.name:45s} deco={deco}")
