# -*- coding: utf-8 -*-
"""网络搜索通道（EXECUTION 161）——Trinity 的"主动搜索"能力。

按查询从 Bing HTML 搜索（可达、零依赖）→ 解析结果（标题+链接+摘要）
→ 感知入记忆（channel=websearch）。与 RSS 订阅（被动）互补：
主动搜索 = 按需获取（基于兴趣词/显式查询）。

幂等：查询+URL 指纹；失败静默。

用法: python scripts/web_search.py --query "PostgreSQL 优化" [--max N]
      python scripts/web_search.py --auto   (用会话兴趣词自动搜索)
"""
import os, sys, json, hashlib, re, urllib.request, time

API = "http://127.0.0.1:8001"
STATE_FILE = os.path.expanduser("~/.trinity/websearch_state.json")
BING = "https://www.bing.com/search?q="
TIMEOUT = 15


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 TrinityMemory/8.2"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _bing_search(query):
    """Bing HTML 搜索 → [(title, link, snippet)]（宽松解析）。"""
    html = _fetch(BING + urllib.request.quote(query))
    results = []
    for m in re.finditer(r'<h2[^>]*>.*?</h2>', html, re.S):
        blk = m.group(0)
        href = ''
        for q in (chr(34), chr(39)):
            i = blk.find('href=' + q)
            if i >= 0:
                j = blk.find(q, i + 6)
                if j > i:
                    href = blk[i + 6:j]
                    break
        t2 = __import__('re').sub(r'<[^>]+>', '', blk).strip()
        if t2 and href.startswith('http'):
            results.append((t2, href, ''))
            if len(results) >= 10:
                break
    return results


def _interest_queries():
    """从会话上下文提取搜索查询（兴趣词 → 搜索词）。"""
    queries = []
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT last_query FROM session_context ORDER BY updated_at DESC LIMIT 3")
        for (q,) in cur.fetchall():
            q = str(q or "").strip()
            if len(q) >= 2 and q not in queries:
                queries.append(q[:50])
        conn.close()
    except Exception:
        pass
    return queries or ["Trinity memory system"]


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
    try:
        payload = {"channel": "websearch", "signal": signal, "importance": 0.6}
        req = urllib.request.Request(API + "/memory/perceive",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body.get("encoded", False)
    except Exception:
        return False


def main():
    sys.path.insert(0, r"D:\trinity-code")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    _max = 10
    _query = ""
    for _a in sys.argv:
        if _a.startswith("--query="):
            _query = _a.split("=", 1)[1]
        if _a.startswith("--max="):
            try:
                _max = int(_a.split("=")[1])
            except Exception:
                pass
    state = _load_state()
    new_state = set(state)
    perceived = 0
    queries = [_query] if _query else _interest_queries()
    for q in queries[:3]:
        try:
            results = _bing_search(q)
            for title, link, snip in results:
                sig = "s:" + hashlib.sha256((q + "|" + link).encode("utf-8")).hexdigest()[:20]
                if sig in state:
                    continue
                signal = f"[websearch:{q[:20]}] {title[:120]} | {link[:90]}"
                if snip:
                    signal += " | " + snip[:180]
                ok = _perceive(signal)
                new_state.add(sig)
                if ok:
                    perceived += 1
                    if perceived >= _max:
                        break
        except Exception:
            continue
        if perceived >= _max:
            break
    _save_state(new_state)
    print(json.dumps({"queries": len(queries), "perceived": perceived,
                      "state_size": len(new_state)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
