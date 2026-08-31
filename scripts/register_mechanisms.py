# -*- coding: utf-8 -*-
"""register_mechanisms.py — 机制注册/索引自动化（EXECUTION 383）。
用法：
  python register_mechanisms.py check   # 检查未注册/未索引模块
  python register_mechanisms.py sync    # 自动补齐注册表+索引
"""
import os, re, glob, sys, io

BRAIN_DIR = r"D:\trinity-code\trinity\brain"
ADV_PATH = r"D:\trinity-code\trinity\core\client\_advanced.py"
MODULES_MD = os.path.join(BRAIN_DIR, "MODULES.md")


def all_modules():
    return sorted([os.path.basename(f)[:-3] for f in glob.glob(os.path.join(BRAIN_DIR, "*.py"))
                  if not os.path.basename(f).startswith("__")])


def check():
    """检查未注册/未索引模块。"""
    c = io.open(ADV_PATH, encoding="utf-8").read()
    registered = set(re.findall(r'trinity\.brain\.([a-z_]+)', c))
    md = io.open(MODULES_MD, encoding="utf-8").read()
    mods = all_modules()
    unreg = [m for m in mods if m not in registered]
    unidx = [m for m in mods if m not in md]
    print("模块总数:", len(mods))
    print("未注册:", len(unreg), unreg)
    print("未索引:", len(unidx), unidx)
    return unreg, unidx


def sync():
    """自动补齐注册表（正确锚点——防误插）+ 索引。"""
    unreg, unidx = check()
    if unreg:
        c = io.open(ADV_PATH, encoding="utf-8").read()
        anchor = '            ("adaptive_plasticity", "trinity.brain.adaptive_plasticity"),'
        ai = c.find(anchor)
        if ai < 0:
            print("锚点缺失——需人工检查")
            return
        entries = []
        for m in unreg:
            entries.append('            ("' + m + '", "trinity.brain.' + m + '"),')
        add_block = "\n".join(entries)
        c = c[:ai + len(anchor)] + "\n" + add_block + "\n" + c[ai + len(anchor):]
        io.open(ADV_PATH, "w", encoding="utf-8", newline="\n").write(c)
        print("注册表已补齐:", len(unreg), "个")
    if unidx:
        md = io.open(MODULES_MD, encoding="utf-8").read()
        md += "\n## 自动索引（EXECUTION 383 起）\n"
        for m in unidx:
            md += "- " + m + "（自动分类待人工复核）\n"
        io.open(MODULES_MD, "w", encoding="utf-8", newline="\n").write(md)
        print("索引已补齐:", len(unidx), "个")
    if not unreg and not unidx:
        print("全部同步——无需操作")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "sync":
        sync()
    else:
        check()
