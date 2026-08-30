# -*- coding: utf-8 -*-
"""embed 保活（EXECUTION 144）——每 5 分钟 ping bge-m3 保持常驻。

消除 30m 无使用后模型卸载导致的 6s 首查冷载。
幂等安全：仅调用 embed API（keep_alive 由 OLLAMA_KEEP_ALIVE 环境管理）。
"""
import urllib.request, json, sys

def main():
    try:
        payload = json.dumps({"model": "bge-m3", "input": ["keepalive"]}).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:11434/api/embed",
                                     data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ok = bool(body.get("embeddings"))
        print(json.dumps({"keepalive": ok}))
        return 0 if ok else 1
    except Exception as e:
        print(json.dumps({"keepalive": False, "error": str(e)[:100]}))
        return 1

if __name__ == "__main__":
    sys.exit(main())
