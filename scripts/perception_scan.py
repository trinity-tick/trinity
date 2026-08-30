# -*- coding: utf-8 -*-
"""环境感知流（EXECUTION 136）——感知具身第一步。

扫描 Trinity 运行日志（supervisor/maintenance/API err）中的告警/错误，
通过 /memory/perceive 自动感知入记忆（显著性=error 通道高显著 + 习惯化）。

幂等：记录已感知行的 (file, line_no) 指纹到 state 文件（~/.trinity/perception_state.json），
跳过已感知内容；失败静默。

用法: python scripts/perception_scan.py [--dry-run]
"""
import os, sys, json, hashlib, urllib.request, time

LOGS_DIR = os.path.expanduser("~/.trinity/logs")
STATE_FILE = os.path.expanduser("~/.trinity/perception_state.json")
API = "http://127.0.0.1:8001"
# 告警模式（行级匹配）
ALERT_PATTERNS = [
    "ERROR", "WARN", "FAILED", "FAIL", "Traceback", "Exception",
    "告警", "失败", "超时", "崩溃", "错误", "SKIP",
]
# 忽略噪音（SKIP 是租约正常行为等）
IGNORE_SUBSTR = ["with_lease: SKIP", "lease held", "SKIP (reason=error)"]


def _fingerprint(fname, lineno, line):
    raw = f"{fname}:{lineno}:{line.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(state), f)
    except Exception:
        pass


def _perceive(signal):
    """调感知通道（error 通道高显著）。"""
    try:
        payload = {"channel": "error", "signal": signal, "importance": 0.7}
        req = urllib.request.Request(API + "/memory/perceive",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body.get("encoded", False)
    except Exception:
        return False


def main():
    dry = "--dry-run" in sys.argv
    _max = 30
    for _a in sys.argv:
        if _a.startswith("--max="):
            try:
                _max = int(_a.split("=")[1])
            except Exception:
                pass
    state = _load_state()
    new_state = set(state)
    perceived = 0
    skipped = 0
    files = []
    for name in os.listdir(LOGS_DIR):
        if name.endswith(".log") or name.endswith(".err.log"):
            files.append(os.path.join(LOGS_DIR, name))
    files.sort()
    now = time.time()
    # 只扫最近 24h 修改的日志
    for fp in files:
        try:
            if now - os.path.getmtime(fp) > 86400:
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    if not any(p in line.upper() for p in ALERT_PATTERNS):
                        continue
                    if any(ig in line for ig in IGNORE_SUBSTR):
                        continue
                    fp_ = os.path.basename(fp)
                    sig = _fingerprint(fp_, lineno, line)
                    if sig in state:
                        skipped += 1
                        continue
                    if dry:
                        print("DRY:", fp_, "->", line[:100])
                        new_state.add(sig)
                        continue
                    signal = f"[log:{fp_}] {line[:200]}"
                    ok = _perceive(signal)
                    if ok:
                        new_state.add(sig)
                        perceived += 1
                        if perceived >= _max:
                            break
                    else:
                        # 感知失败仍记状态防重试风暴
                        new_state.add(sig)
                        skipped += 1
        except Exception:
            continue
    if not dry:
        _save_state(new_state)
    print(json.dumps({"perceived": perceived, "skipped": skipped,
                      "state_size": len(new_state)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
