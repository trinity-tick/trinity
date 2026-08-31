import io, re, subprocess, os

P = r"D:\\smartcos-wms\\backend\\internal\\gateway\\handler.go"
D = r"D:\smartcos-wms\\backend\\internal\\gateway"
raw = io.open(P, encoding="utf-8", newline="").read()
lines = raw.splitlines(keepends=True)
n = len(lines)

# 1) 找 func 起始行（0-based）与所属域
func_re = re.compile(r"^func (?:\([^)]*\) )?(\w+)")
starts = []  # (line_idx, name, domain)
domain_of = lambda name: (
    "ai" if re.match(r"^(AI|Trinity|LLM|ai|trinity|llm)", name) else
    "openapi" if re.match(r"^(OpenAPI|GetOpenAPI)", name) else
    "auth" if re.match(r"^(Login|Logout|GetCurrentUser|hashPassword|extractBearerToken|requireRole)", name) else
    "helpers" if re.match(r"^(parsePagination|respondList|respondError|respondOK)", name) else None)

for i, l in enumerate(lines):
    m = func_re.match(l)
    if m:
        d = domain_of(m.group(1))
        if d:
            starts.append((i, m.group(1), d))

# 2) 每个目标 func 的块起始（向上吞连续注释行）
blocks = []  # (start0, end0_exclusive, domain, name)
for idx, (i, name, d) in enumerate(starts):
    end = starts[idx + 1][0] if idx + 1 < len(starts) else None
    if end is None:
        # 文件内最后一个目标函数——找其后第一个非空/非}行? 简化: 到下一个任意 func 行
        nxt = None
        for j in range(i + 1, n):
            if func_re.match(lines[j]):
                nxt = j
                break
        end = nxt if nxt else n
    s = i
    while s - 1 >= 0 and lines[s - 1].lstrip().startswith("//"):
        s -= 1
    blocks.append([s, end, d, name])

# 不重叠校正（按 start 排序后 end = min(end, next_start)）
blocks.sort()
for k in range(len(blocks) - 1):
    if blocks[k][1] > blocks[k + 1][0]:
        blocks[k][1] = blocks[k + 1][0]

# 3) import 块（handler.go 的 import ( ... ) 整块）
imp_s = next(i for i, l in enumerate(lines) if l.startswith("import ("))
imp_e = next(i for i, l in enumerate(lines[imp_s:], imp_s) if l.startswith(")")) + 1
import_block = "".join(lines[imp_s:imp_e])

# 4) 提取块到域文件
moved = set()
for s, e, d, name in blocks:
    moved.update(range(s, e))
groups = {}
for s, e, d, name in blocks:
    groups.setdefault(d, []).append("".join(lines[s:e]))
for d, chunks in groups.items():
    fp = os.path.join(D, "handler_" + d + ".go")
    io.open(fp, "w", encoding="utf-8", newline="").write("package gateway\n\n" + import_block + "\n" + "".join(chunks))
    print("wrote handler_" + d + ".go:", len(chunks), "funcs")

# 5) 原 handler.go 删除已移块
keep = [l for i, l in enumerate(lines) if i not in moved]
io.open(P, "w", encoding="utf-8", newline="").write("".join(keep))
print("handler.go now:", len(keep), "lines (was", n, ")")

# 6) 迭代修 unused import（最多 6 轮）
os.chdir(r"D:\smartcos-wms\\backend")
for rnd in range(6):
    b = subprocess.run(["go", "build", "./internal/gateway/"], capture_output=True, text=True)
    if b.returncode == 0:
        print("BUILD OK round", rnd)
        break
    errs = b.stderr + b.stdout
    fixed = False
    for f in ["handler_ai.go", "handler_openapi.go", "handler_auth.go", "handler_helpers.go", "handler.go"]:
        fp = os.path.join(D, f)
        c = io.open(fp, encoding="utf-8", newline="").read()
        for m in re.finditer(r"imported and not used: \"([^\"]+)\"", errs):
            imp = m.group(1)
            line = "\t"" + imp + ""\n"
            if line in c:
                c = c.replace(line, "", 1)
                fixed = True
                io.open(fp, "w", encoding="utf-8", newline="").write(c)
    if not fixed:
        print("UNFIXABLE:", errs[:500])
        break
