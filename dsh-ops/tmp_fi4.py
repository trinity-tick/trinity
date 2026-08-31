import io, subprocess, os

BS = chr(92)
D = "D:" + BS + "smartcos-wms" + BS + "backend" + BS + "internal" + BS + "gateway"
os.chdir("D:" + BS + "smartcos-wms" + BS + "backend")

for rnd in range(25):
    b = subprocess.run(["go", "build", "./internal/gateway/"], capture_output=True, text=True)
    if b.returncode == 0:
        print("BUILD OK round", rnd)
        break
    errs = (b.stderr or "") + (b.stdout or "")
    fixed = 0
    for line in errs.splitlines():
        if "imported and not used" not in line:
            continue
        loc = line.split(":")[0].strip()
        fname = loc.split(BS)[-1] if BS in loc else loc.split("/")[-1]
        spec = line.split(": ", 2)[2] if line.count(": ") >= 2 else line
        q = chr(34)
        path = spec.split(q)[1] if q in spec else None
        if not path:
            continue
        fp = os.path.join(D, fname)
        if not os.path.exists(fp):
            continue
        lines = io.open(fp, encoding="utf-8", newline="").readlines()
        tgt = q + path + q
        for i, l in enumerate(lines):
            if l.strip().endswith(tgt):
                del lines[i]
                io.open(fp, "w", encoding="utf-8", newline="").writelines(lines)
                fixed += 1
                break
    if fixed == 0:
        print("STUCK round", rnd)
        break
