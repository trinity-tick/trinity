# -*- coding: utf-8 -*-
"""网络感知通道（EXECUTION 158）——Trinity 的"网络感官"。

RSS 订阅源定时抓取 → 提取标题/摘要 → /memory/perceive 感知入记忆
（channel=web，高显著 web 通道）。与日志/文件/视觉感知同构。

幂等：URL 哈希指纹存 state 文件（~/.trinity/web_state.json），跳过已感知。
零第三方依赖（urllib + xml.etree 标准库）。

用法: python scripts/web_perception.py [--max N] [--dry-run]
"""
import os, sys, json, hashlib, urllib.request, time

API = "http://127.0.0.1:8001"
STATE_FILE = os.path.expanduser("~/.trinity/web_state.json")

# RSS 订阅源（可达性已测，2026-08-30）
RSS_FEEDS = [
    "https://www.oschina.net/news/rss",
    "https://www.cnblogs.com/rss",
    "https://blog.jetbrains.com/feed/",
    "https://www.infoq.cn/feed",
    "https://hnrss.org/frontpage",
]
TIMEOUT = 15


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 TrinityMemory/8.2"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_rss(xml_text):
    """提取 (title, link) 列表（RSS/Atom 兼容）。"""
    items = []
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        # RSS 2.0: channel/item；Atom: feed/entry
        for item in root.iter("item") or []:
            title = item.findtext("title", "") or ""
            link = item.findtext("link", "") or ""
            if title and link:
                items.append((title.strip(), link.strip()))
        for entry in root.iter("entry"):
            title = entry.findtext("title", "") or ""
            link = ""
            l = entry.find("link")
            if l is not None:
                link = l.get("href", "") or ""
            if title and link:
                items.append((title.strip(), link.strip()))
    except Exception:
        pass
    return items


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
        payload = {"channel": "web", "signal": signal, "importance": 0.6}
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
    _max = 20
    for _a in sys.argv:
        if _a.startswith("--max="):
            try:
                _max = int(_a.split("=")[1])
            except Exception:
                pass
    state = _load_state()
    new_state = set(state)
    perceived = 0
    for feed in RSS_FEEDS:
        try:
            xml_text = _fetch(feed)
            items = _parse_rss(xml_text)
            for title, link in items[:8]:  # 每源最多 8 条
                sig = "w:" + hashlib.sha256(link.encode("utf-8")).hexdigest()[:20]
                if sig in state:
                    continue
                if dry:
                    print("DRY-WEB:", title[:60])
                    new_state.add(sig)
                    continue
                signal = f"[web:{os.path.basename(feed)[:20]}] {title[:150]} | {link[:100]}"
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
    if not dry:
        _save_state(new_state)
    print(json.dumps({"perceived": perceived, "feeds": len(RSS_FEEDS),
                      "state_size": len(new_state)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
