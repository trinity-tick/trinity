import io, subprocess, os

BS = chr(92)
D = "D:" + BS + "smartcos-wms" + BS + "backend" + BS + "internal" + BS + "gateway"
os.chdir("D:" + BS + "smartcos-wms" + BS + "backend")

b = subprocess.run(["go", "build", "./internal/gateway/"], capture_output=True, text=True)
print("rc=", b.returncode)
errs = (b.stderr or "") + (b.stdout or "")
for line in errs.splitlines()[:8]:
    print("RAW:", repr(line[:150]))
