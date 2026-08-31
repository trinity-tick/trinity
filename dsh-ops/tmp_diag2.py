import io, subprocess, os
BS = chr(92)
D = "D:" + BS + "smartcos-wms" + BS + "backend" + BS + "internal" + BS + "gateway"
os.chdir("D:" + BS + "smartcos-wms" + BS + "backend")
b = subprocess.run(["go", "build", "./internal/gateway/"], capture_output=True, text=True)
errs = (b.stderr or "") + (b.stdout or "")
cnt = 0
for line in errs.splitlines():
    if "imported and not used" not in line:
        continue
    cnt += 1
    if cnt > 2:
        break
    loc = line.split(":")[0].strip()
    fname = loc.split(BS)[-1] if BS in loc else loc.split("/")[-1]
    spec = line.split(": ", 2)[2] if line.count(": ") >= 2 else line
    q = chr(34)
    path = spec.split(q)[1] if q in spec else None
    fp = os.path.join(D, fname)
    print("fname=", fname, "| path=", path, "| exists=", os.path.exists(fp))
    if os.path.exists(fp):
        lines = io.open(fp, encoding="utf-8", newline="").readlines()
        tgt = q + path + q
        hits = [i for i, l in enumerate(lines) if l.strip().endswith(tgt)]
        print("  target lines:", hits[:3])
