# -*- coding: utf-8 -*-
"""能力自检（EXECUTION 179）——覆盖闭环审计未涵盖的能力。
视觉/感知引擎/图谱/工作记忆/市场/MCP/DCPM/网络/情绪/认知管线。
"""
import sys, os, json, io, base64, urllib.request
sys.path.insert(0, r"D:\trinity-code")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PGHOST", "127.0.0.1"); os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "trinity"); os.environ.setdefault("PGUSER", "trinity")
os.environ.setdefault("PGPASSWORD", "trinity")
os.environ.setdefault("TRINITY_STORAGE_BACKEND", "postgresql")

report = {"ok": True, "checks": {}}

def _pg():
    import psycopg2
    return psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity", user="trinity", password="trinity")

def run():
    # 1) 视觉（b64 版本）
    try:
        from trinity.vision import describe_image_b64
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (200, 60), "white")
        d = ImageDraw.Draw(img)
        d.text((10, 20), "Trinity Test", fill="black")
        buf = io.BytesIO(); img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        desc = describe_image_b64(b64)
        report["checks"]["vision"] = {"ok": bool(desc and len(desc) > 10), "desc": str(desc)[:60]}
    except Exception as e:
        report["checks"]["vision"] = {"ok": False, "error": str(e)[:80]}
    # 2) 感知引擎
    try:
        from trinity.brain.perception import PerceptionEngine
        pe = PerceptionEngine()
        ev = pe.evaluate("web", "[self-check] 能力自检信号")
        report["checks"]["perception_engine"] = {"ok": ev.get("salience", 0) > 0.5, "salience": ev.get("salience")}
    except Exception as e:
        report["checks"]["perception_engine"] = {"ok": False, "error": str(e)[:80]}
    # 3) 图谱
    try:
        from trinity.adapters.postgresql import PostgreSQLAdapter
        a = PostgreSQLAdapter(auto_connect=True); a.connect()
        try:
            snap = a.sage_load_snapshot()
            report["checks"]["sage_graph"] = {"ok": True, "snapshot": bool(snap)}
        finally:
            a.disconnect()
    except Exception as e:
        report["checks"]["sage_graph"] = {"ok": False, "error": str(e)[:80]}
    # 4) 工作记忆
    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("SELECT count(*) FROM session_context WHERE wm IS NOT NULL")
        wm_n = cur.fetchone()[0]; conn.close()
        report["checks"]["working_memory"] = {"ok": wm_n > 0, "with_wm": wm_n}
    except Exception as e:
        report["checks"]["working_memory"] = {"ok": False, "error": str(e)[:80]}
    # 5) 市场
    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/market/orderbook", timeout=20) as resp:
            ob = json.loads(resp.read().decode())
        report["checks"]["market"] = {"ok": True, "orders": ob.get("count", 0)}
    except Exception as e:
        report["checks"]["market"] = {"ok": False, "error": str(e)[:80]}
    # 6) MCP
    try:
        import socket
        ok_ports = []
        for p in (8000, 8003):
            s = socket.socket(); s.settimeout(2)
            try:
                s.connect(("127.0.0.1", p)); ok_ports.append(p)
            except Exception:
                pass
            finally:
                s.close()
        report["checks"]["mcp"] = {"ok": len(ok_ports) == 2, "ports": ok_ports}
    except Exception as e:
        report["checks"]["mcp"] = {"ok": False, "error": str(e)[:60]}
    # 7) DCPM
    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("SELECT count(*) FROM dcpm_beliefs")
        dcpm = cur.fetchone()[0]; conn.close()
        report["checks"]["dcpm"] = {"ok": dcpm > 50, "beliefs": dcpm}
    except Exception as e:
        report["checks"]["dcpm"] = {"ok": False, "error": str(e)[:80]}
    # 8) 网络通道
    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE category='perception' AND content LIKE '%[web%'")
        web_n = cur.fetchone()[0]; conn.close()
        report["checks"]["web_channel"] = {"ok": web_n > 0, "web_signals": web_n}
    except Exception as e:
        report["checks"]["web_channel"] = {"ok": False, "error": str(e)[:80]}
    # 9) 情绪
    try:
        from trinity.brain.affect_state import update_state
        s = update_state(None, {"valence": 0.5, "arousal": 0.2, "polarity": "pos"})
        report["checks"]["affect_state"] = {"ok": s.get("valence") > 0, "state": s.get("polarity")}
    except Exception as e:
        report["checks"]["affect_state"] = {"ok": False, "error": str(e)[:80]}
    # 10) 认知管线
    try:
        from trinity.brain.cognition_pipeline import run_pipeline, STAGES
        class M:
            _last_query = "x"
        r = run_pipeline(M(), "q", [{"a": 1}], {s: True for s in STAGES})
        report["checks"]["cognition_pipeline"] = {"ok": r["results"] == 1 and r["active"] >= 1, "active": r["active"]}
    except Exception as e:
        report["checks"]["cognition_pipeline"] = {"ok": False, "error": str(e)[:80]}

    for k, v in report["checks"].items():
        if isinstance(v, dict) and v.get("ok") is False:
            report["ok"] = False
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1

if __name__ == "__main__":
    sys.exit(run())
