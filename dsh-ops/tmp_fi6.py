import io, subprocess, os
BS = chr(92)
D = "D:" + BS + "smartcos-wms" + BS + "backend" + BS + "internal" + BS + "gateway"
BE = "D:" + BS + "trinity-code" + BS + "dsh-ops" + BS + "be4.txt"
CWD = "D:" + BS + "smartcos-wms" + BS + "backend"
q = chr(34)
os.chdir(CWD)

for rnd in range(30):
    r = subprocess.run(["cmd", "/c", "go build ./internal/gateway/ > be4tmp.txt 2>&1"], capture_output=True, text=True, cwd=CWD)
    errs = io.open(os.path.join(CWD, "be4tmp.txt"), encoding="utf-8", errors="ignore").read()
    if "imported and not used" not in errs:
        print("BUILD OK round", rnd)
        break
    fixed = 0
    for line in errs.splitlines():
        if "imported and not used" not in line:
            continue
        loc = line.split(":")[0].strip()
        fname = loc.split(BS)[-1]
        spec = line.split(": ", 2)[2] if line.count(": ") >= 2 else line
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
    print("round", rnd, "fixed", fixed)
    if fixed == 0:
        print("STUCK; sample:", [l[:130] for l in errs.splitlines()[1:4]])
        break
os.remove(os.path.join(CWD, "be4tmp.txt")) if os.path.exists(os.path.join(CWD, "be4tmp.txt")) else None
