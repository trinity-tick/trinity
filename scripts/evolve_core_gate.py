#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evolve_core_gate.py — core self-evolution Phase-3 pilot (EXECUTION 458)."""
import ast, json, os, subprocess, sys, time
ROOT = "C:/Users/Administrator/trinity"
REL = "trinity/security/crypto.py"
TARGET = os.path.join(ROOT, REL.replace("/", os.sep))
EVIDENCE = os.path.expanduser("~/.trinity/state/evolve_core_pilot.json")
GOAL = ("add module-level function is_encrypted(content) -> bool to crypto.py: "
        "returns True only for str starting with enc:v1: prefix, else False. "
        "Output ONLY that function code. No imports, no classes, no other changes.")

def llm_propose():
    if not os.environ.get("TRINITY_LLM_API_KEY"):
        try:
            for line in open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig"):
                if line.strip().startswith("DEEPSEEK_API_KEY"):
                    os.environ["TRINITY_LLM_API_KEY"] = line.split(":", 1)[1].strip().strip("'\"")
                    break
        except Exception:
            pass
    from trinity.llm.client import chat_completion
    sys_p = "You are a meticulous Python engineer. Produce ONLY the code block. No prose."
    resp = chat_completion({"model": "deepseek-chat",
                            "messages": [{"role": "system", "content": sys_p},
                                         {"role": "user", "content": GOAL}],
                            "temperature": 0.1, "max_tokens": 300})
    out = str(resp.get("content", "")).strip()
    if out.startswith("```"):
        out = out.split("```", 2)[1] if out.count("```") >= 2 else out
        if out.startswith("python"):
            out = out[len("python"):].lstrip()
        out = out.rstrip().rstrip("```").rstrip()
    return out

def validate(code):
    tree = ast.parse(code)
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert names == ["is_encrypted"], "single def is_encrypted expected: " + str(names)
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.ClassDef)):
            raise ValueError("imports/classes not allowed")
    return code

def gates_ok():
    # behavior gate
    bcode = [
        "import sys; sys.path.insert(0, \"C:/Users/Administrator/trinity\")",
        "from trinity.security.crypto import is_encrypted",
        "assert is_encrypted(\"enc:v1:abc\") is True",
        "assert is_encrypted(\"plain\") is False",
        "assert is_encrypted(None) is False",
        "print(\"BEHAVIOR_OK\")",
    ]
    r1 = subprocess.run([sys.executable, "-c", "; ".join(bcode)], capture_output=True, text=True)
    if r1.returncode != 0 or "BEHAVIOR_OK" not in r1.stdout:
        return False, "behavior: " + r1.stderr[-200:]
    # canonical pytest gate
    r2 = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=line"],
                       cwd=ROOT, capture_output=True, text=True, errors="replace", timeout=900)
    tail = (r2.stdout or "").strip().splitlines()
    ok = bool(tail) and "passed" in tail[-1] and "failed" not in tail[-1]
    return ok, (tail[-1] if tail else r2.stderr[-200:])

def main():
    ev = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "target": REL}
    src = open(TARGET, encoding="utf-8").read()
    code = llm_propose()
    print("proposal:", code[:200])
    try:
        code = validate(code)
    except Exception as e:
        ev["stage"] = "rejected"; ev["reason"] = str(e)
        json.dump(ev, open(EVIDENCE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("REJECTED:", e); return 1
    if code.strip() in src:
        print("ALREADY_PRESENT"); return 0
    open(TARGET, "a", encoding="utf-8").write("\n\n" + code + "\n")
    ev["stage"] = "applied"
    ok, tail = gates_ok()
    print("gates:", ok, "|", tail)
    if not ok:
        open(TARGET, "w", encoding="utf-8").write(src)
        ev["stage"] = "rejected"; ev["reason"] = "gate failed, reverted: " + tail
        json.dump(ev, open(EVIDENCE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return 1
    ev["stage"] = "gate_pass"; ev["pytest"] = tail
    if len(sys.argv) > 1 and sys.argv[1] == "--apply":
        subprocess.run(["git", "-C", ROOT, "add", REL], capture_output=True)
        cm = subprocess.run(["git", "-C", ROOT, "commit", "-m", "feat(EXECUTION 458): core-evolve pilot is_encrypted (gate PASS)"], capture_output=True)
        ev["commit"] = cm.returncode == 0
    json.dump(ev, open(EVIDENCE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("EVIDENCE ->", EVIDENCE)
    return 0

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())