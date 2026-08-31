import io
q = chr(34)
BS = chr(92)
D = "D:" + BS + "smartcos-wms" + BS + "backend" + BS + "internal" + BS + "gateway"
# 从错误清单提取 (文件, 路径)
raw = io.open("D:" + BS + "trinity-code" + BS + "dsh-ops" + BS + "errs_full.txt", encoding="utf-8-sig").read()
targets = {}
for line in raw.splitlines():
    if "imported and not used" not in line:
        continue
    loc = line.strip().split(":")[0]
    fname = loc.split(BS)[-1]
    spec = line.split(": ", 2)[2] if line.count(": ") >= 2 else line
    path = spec.split(q)[1] if q in spec else None
    if path:
        targets.setdefault(fname, set()).add(path)
print("targets:", {k: len(v) for k, v in targets.items()})
for fname, paths in targets.items():
    fp = D + BS + fname
    lines = io.open(fp, encoding="utf-8", newline="").readlines()
    out = []
    removed = 0
    for l in lines:
        s = l.strip()
        if any(s == q + p + q or s.endswith(q + p + q) for p in paths):
            removed += 1
            continue
        out.append(l)
    io.open(fp, "w", encoding="utf-8", newline="").writelines(out)
    print(fname, "removed", removed)
