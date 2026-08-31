import io
q = chr(34)
BS = chr(92)
D = "D:" + BS + "smartcos-wms" + BS + "backend" + BS + "internal" + BS + "gateway"
raw = io.open("D:" + BS + "trinity-code" + BS + "dsh-ops" + BS + "errs2.txt", encoding="utf-8-sig").read()
targets = {}
noise = ("go : ", "所在位置", "+ ", "CategoryInfo", "FullyQualifiedId", "# github")
for line in raw.splitlines():
    s = line.strip()
    if not s or any(s.startswith(n) for n in noise):
        continue
    if ("imported and not used" not in s) and ("imported as" not in s):
        continue
    loc = s.split(":")[0]
    fname = loc.split(BS)[-1]
    path = s.split(q)[1] if q in s else None
    if path and fname.endswith(".go"):
        targets.setdefault(fname, set()).add(path)
for fname, paths in targets.items():
    fp = D + BS + fname
    lines = io.open(fp, encoding="utf-8", newline="").readlines()
    out = []
    removed = 0
    for l in lines:
        st = l.strip()
        if any(st == q + p + q or st.endswith(q + p + q) for p in paths):
            removed += 1
            continue
        out.append(l)
    io.open(fp, "w", encoding="utf-8", newline="").writelines(out)
    print(fname, "removed", removed)
