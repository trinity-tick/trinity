import io, re, subprocess, os

P = r"D:\\smartcos-wms\\backend\\internal\\gateway\\handler.go"
D = r"D:\smartcos-wms\\backend\\internal\\gateway"
os.chdir(r"D:\\smartcos-wms\\backend")

for rnd in range(12):
    b = subprocess.run(["go", "build", "./internal/gateway/"], capture_output=True, text=True)
    if b.returncode == 0:
        print("BUILD OK round", rnd)
        break
    errs = (b.stderr or "") + (b.stdout or "")
    fixed = 0
    # unused imports (handler.go + 4 new files)
    for m in re.finditer(r'internal\\\\gateway\\\\(\w+\.go):(\d+):\d+: (.*) imported and not used', errs):
        f, ln, spec = m.group(1), int(m.group(2)), m.group(3)
        mm = re.search(r'imported as (\w+) and not used|imported and not used: "(.*)"', spec)
        path = (mm.group(2) if mm and mm.group(2) else (mm.group(1) if mm else None))
        if not path:
            continue
        fp = os.path.join(D, f)
        lines = io.open(fp, encoding="utf-8", newline="").readlines()
        tgt = '"' + path + '"'
        for i, l in enumerate(lines):
            if tgt in l and l.strip().startswith('"'):
                del lines[i]
                fixed += 1
                io.open(fp, "w", encoding="utf-8", newline="").writelines(lines)
                break
    if fixed == 0:
        print("STUCK round", rnd, ":", errs[:600])
        break
