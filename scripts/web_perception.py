# -*- coding: utf-8 -*-
"""网络感知通道 v2（EXECUTION 159）——质料质量优化。

v1（158）: 标题+链接。v2 增强：
  1. 正文抓取：html.parser 提取正文文本（前 N 字符）
  2. 主题过滤：从 session_context 兴趣词（last_query 关键词）偏好——
     Trinity 关注领域的新闻优先感知（自我模型驱动信息获取）
  3. 标题归一化去重（同新闻多源只留一次）
  4. LLM 摘要（可选 TRINITY_WEB_SUMMARIZE=1；失败降级标题）

用法: python scripts/web_perception.py [--max N] [--dry-run]
"""
import os, sys, json, hashlib, re, urllib.request, time

API = "http://127.0.0.1:8001"
STATE_FILE = os.path.expanduser("~/.trinity/web_state.json")

RSS_FEEDS = [
    "https://www.oschina.net/news/rss",
    "https://www.cnblogs.com/rss",
    "https://blog.jetbrains.com/feed/",
    "https://www.infoq.cn/feed",
    "https://hnrss.org/frontpage",
    "https://www.ithome.com/rss/",
    "https://www.36kr.com/feed",
]
TIMEOUT = 15
BODY_LIMIT = 500  # 正文截断


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 TrinityMemory/8.2"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_rss(xml_text):
    items = []
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        for item in root.iter("item"):
            title = (item.findtext("title", "") or "").strip()
            link = (item.findtext("link", "") or "").strip()
            if title and link:
                items.append((title, link))
        for entry in root.iter("entry"):
            title = (entry.findtext("title", "") or "").strip()
            link = ""
            l = entry.find("link")
            if l is not None:
                link = (l.get("href", "") or "").strip()
            if title and link:
                items.append((title, link))
    except Exception:
        pass
    return items


def _extract_body(html_text, limit=BODY_LIMIT):
    """html.parser 提取正文文本（去标签/脚本/样式）。"""
    try:
        from html.parser import HTMLParser

        class _T(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
                self.skip = 0

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "nav", "footer", "header"):
                    self.skip += 1

            def handle_endtag(self, tag):
                if tag in ("script", "style", "nav", "footer", "header") and self.skip:
                    self.skip -= 1

            def handle_data(self, data):
                if not self.skip:
                    t = data.strip()
                    if t and len(t) > 2:
                        self.parts.append(t)

        p = _T()
        try:
            p.feed(html_text)
        except Exception:
            pass
        text = " ".join(p.parts)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]
    except Exception:
        return ""


def _normalize_title(title):
    """标题归一化（去标点/小写）用于相似去重。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", title.lower())[:30]


def _interest_words():
    """从会话上下文提取兴趣词（自我模型驱动的信息偏好）。"""
    words = []
    try:
        sys.path.insert(0, r"D:\trinity-code")
        from trinity.adapters.postgresql import PostgreSQLAdapter
        a = PostgreSQLAdapter(auto_connect=True)
        a.connect()
        try:
            import psycopg2
            conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                    user="trinity", password="trinity")
            cur = conn.cursor()
            cur.execute("SELECT last_query FROM session_context ORDER BY updated_at DESC LIMIT 5")
            for (q,) in cur.fetchall():
                for w in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{4,}", str(q or "")):
                    words.append(w.lower())
            conn.close()
        finally:
            a.disconnect()
    except Exception:
        pass
    return set(words)


def _summarize(title, body):
    """LLM 一句话摘要（EXECUTION 160）——高价值质料智能摘要。

    用 llm_chat（DeepSeek API，本地降级链）；失败返回 None（调用方
    降级用原标题）。TRINITY_WEB_SUMMARIZE=0 关闭。
    """
    if os.environ.get("TRINITY_WEB_SUMMARIZE", "1") != "1":
        return None
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.value_encoder import llm_chat
        _mat = (str(body or "")[:300] or str(title))
        _prompt = ("用一句话中文总结这条技术新闻（不超过30字）：" + str(title)[:100] + "。内容：" + _mat[:250])
        _r = llm_chat(_prompt, max_tokens=60, timeout=30)
        if _r and len(_r.strip()) > 5:
            return _r.strip()[:80]
        return None
    except Exception:
        return None


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
    _max = 15
    for _a in sys.argv:
        if _a.startswith("--max="):
            try:
                _max = int(_a.split("=")[1])
            except Exception:
                pass
    state = _load_state()
    new_state = set(state)
    seen_titles = set()
    perceived = 0
    interests = _interest_words()
    for feed in RSS_FEEDS:
        try:
            xml_text = _fetch(feed)
            items = _parse_rss(xml_text)
            for title, link in items[:10]:
                # 相似去重（多源同新闻）
                nt = _normalize_title(title)
                if nt and nt in seen_titles:
                    continue
                if nt:
                    seen_titles.add(nt)
                sig = "w:" + hashlib.sha256(link.encode("utf-8")).hexdigest()[:20]
                if sig in state:
                    continue
                if dry:
                    print("DRY-WEB:", title[:60])
                    new_state.add(sig)
                    continue
                # 主题偏好：兴趣词命中则提升 importance（软偏好，不硬过滤）
                _imp = 0.6
                hit = any(w in title.lower() for w in interests) if interests else True
                if hit and interests:
                    _imp = 0.8  # 兴趣命中 → 更高显著
                signal = f"[web:{os.path.basename(feed)[:16]}] {title[:150]} | {link[:90]}"
                # 正文抓取（失败降级纯标题）
                _body_txt = ""
                try:
                    body = _extract_body(_fetch(link))
                    if body:
                        _body_txt = body[:BODY_LIMIT]
                        signal += " | " + _body_txt
                except Exception:
                    pass
                # LLM 摘要（全部质料——成本可控；失败降级）
                if True:
                    try:
                        _sum = _summarize(title, _body_txt)
                        if _sum:
                            signal = f"[web-sum] {_sum} || " + signal[:200]
                    except Exception:
                        pass
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
    print(json.dumps({"perceived": perceived, "interests": len(interests),
                      "state_size": len(new_state)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
