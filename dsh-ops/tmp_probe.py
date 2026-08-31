import io
q = chr(34)
BS = chr(92)

def patch(fp, old, new, tag, add_log=True):
    c = io.open(fp, encoding="utf-8", newline="").read()
    if old not in c:
        print(tag, "MISS")
        return
    c = c.replace(old, new, 1)
    if add_log and q + "log" + q not in c and 'log.Printf' in new:
        c = c.replace("import (\n", "import (\n\t"log"\n", 1) if "import (\n" in c else c
        # 简化: 在 package 行后插 import（若无 import 块）
    io.open(fp, "w", encoding="utf-8", newline="").write(c)
    print(tag, "patched")
