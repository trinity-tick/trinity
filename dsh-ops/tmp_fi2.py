import io, subprocess, os

D = r"D:\\smartcos-wms\\backend\\internal\\gateway"
os.chdir(r"D:\\smartcos-wms\\backend")

for rnd in range(15):
    b = subprocess.run(["go", "build", "./internal/gateway/"], capture_output=True, text=True)
    if b.returncode == 0:
        print("BUILD OK round", rnd)
        break
    errs = (b.stderr or "") + (b.stdout or "")
    fixed = 0
    for line in errs.splitlines():
        if "imported and not used" not in line:
            continue
        try:
            loc = line.split(":")[0].strip()           # internal\gateway\handler.go
            fname = loc.replace("internal", "").replace("gateway", "").replace("/", "").replace("\\", "")
            spec = line.split(": ", 2)[2] if ": " in line else line
            path = spec.split('"')[1]                   # crypto/sha256
        except Exception:
            continue
        fp = os.path.join(D, fname)
        if not os.path.exists(fp):
            continue
        lines = io.open(fp, encoding="utf-8", newline="").readlines()
        tgt = chr(34) + path + chr(34)
        for i, l in enumerate(lines):
            if tgt in l and l.strip().startswith(tgt):
                del lines[i]
                io.open(fp, "w", encoding="utf-8", newline="").writelines(lines)
                fixed += 1
                break
    if fixed == 0:
        print("STUCK round", rnd)
        for line in errs.splitlines()[:6]:
            print("  ", line)
        break
