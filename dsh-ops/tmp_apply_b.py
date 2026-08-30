import io, json
sp = json.load(io.open(r"D:\trinity-code\dsh-ops\tmp_p.json", encoding="utf-8"))
p = r"D:\trinity-code\dsh-ops\trinity-dsh-maintenance.ps1"
c = io.open(p, encoding="utf-8").read()
assert sp["old"] in c, "anchor missing"
c = c.replace(sp["old"], sp["new"], 1)
io.open(p, "w", encoding="utf-8", newline="\n").write(c)
print("definition applied")
