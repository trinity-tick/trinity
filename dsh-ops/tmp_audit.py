# -*- coding: utf-8 -*-
"""依赖审计：brain 模块被引用的真实情况（重构风险评估基础）"""
import os, re, glob, collections

brain_dir = r"D:\trinity-code\trinity\brain"
code_dirs = [r"D:\trinity-code\trinity\core", r"D:\trinity-code\trinity\modules",
             r"D:\trinity-code\trinity\brain", r"D:\trinity-code\scripts",
             r"D:\trinity-code\trinity\engine", r"D:\trinity-code\trinity\utils"]
if not os.path.isdir(r"D:\trinity-code\scripts"):
    code_dirs.remove(r"D:\trinity-code\scripts")

# 1) 收集所有代码文件
all_files = []
for d in code_dirs:
    if os.path.isdir(d):
        all_files.extend(glob.glob(os.path.join(d, "**", "*.py"), recursive=True))
print("代码文件:", len(all_files))

# 2) 每个 brain 模块被引用次数（import / from ... import）
modules = [os.path.basename(f)[:-3] for f in glob.glob(os.path.join(brain_dir, "*.py"))
           if not os.path.basename(f).startswith("__")]
refs = collections.defaultdict(int)
ref_files = collections.defaultdict(set)
for f in all_files:
    try:
        content = open(f, encoding="utf-8", errors="ignore").read()
    except Exception:
        continue
    for m in modules:
        # import trinity.brain.<m> 或 from trinity.brain.<m> import 或 brain.<m>
        pat = re.compile(r"(?:import|from)\s+trinity\.brain\.?" + re.escape(m) + r"|brain\.?" + re.escape(m))
        if pat.search(content):
            refs[m] += 1
            ref_files[m].add(os.path.basename(f))

# 3) 核心引用（core/engine 目录）
core_refs = collections.defaultdict(int)
for f in all_files:
    if "core" in f or "engine" in f or "utils" in f:
        try:
            content = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for m in modules:
            pat = re.compile(r"(?:import|from)\s+trinity\.brain\.?" + re.escape(m) + r"|brain\.?" + re.escape(m))
            if pat.search(content):
                core_refs[m] += 1

# 4) 结果
print("=== 被核心代码引用的模块（重构高风险——不能移动/改名） ===")
high_risk = [(m, c) for m, c in core_refs.items() if c > 0]
high_risk.sort(key=lambda x: -x[1])
for m, c in high_risk[:20]:
    print(f"  {m}: {c} 处核心引用")

print("=== 被引用 >=3 处的模块（中风险） ===")
mid_risk = [(m, c) for m, c in refs.items() if c >= 3 and core_refs.get(m, 0) == 0]
mid_risk.sort(key=lambda x: -x[1])
for m, c in mid_risk[:15]:
    print(f"  {m}: {c} 处引用")

print("=== 零引用模块（低风险——物理移动安全） ===")
zero = [m for m in modules if refs[m] == 0]
print(f"  零引用: {len(zero)} 个:", zero[:15])

# 5) 模块间互引（brain 内部依赖——子目录化风险）
internal = collections.defaultdict(int)
for f in all_files:
    if "brain" in f and os.path.basename(f) != "__init__.py":
        try:
            content = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for m in modules:
            if m == os.path.basename(f)[:-3]:
                continue
            pat = re.compile(r"(?:from|import)\s+.*brain\.?" + re.escape(m))
            if pat.search(content):
                internal[m] += 1
print("=== 被其他 brain 模块引用的模块（子目录化需同步改 import） ===")
internal_sorted = sorted(internal.items(), key=lambda x: -x[1])
for m, c in internal_sorted[:15]:
    print(f"  {m}: {c} 个 brain 模块引用")
