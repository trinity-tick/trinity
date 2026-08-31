# -*- coding: utf-8 -*-
"""wms_refactored_system.py — SmartCos WMS 完美重构参考实现（EXECUTION 412）

全部 10 优化组件 → HTTP API 化 + 实时控制台（零外部依赖——纯 stdlib）。
运行: python wms_refactored_system.py [port]   默认 8020
文档: docs/wms_perfect_blueprint.md（七大域蓝图）
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wms_optimization_full import (
    AntiOversell, WavePlanner, ReplenishOptimizer, DynamicLocation,
    PromotionPlan, BillingEngine, LMSOptimizer, MultiWarehouse,
    Reconciliation, SecurityAudit)

# ---------- 系统单例（完美态初始数据） ----------
ao = AntiOversell()
for sku, qty in [("SKU-A001", 120), ("SKU-B002", 8), ("SKU-C003", 10)]:
    ao.init_stock(sku, qty)
wp = WavePlanner()
ro = ReplenishOptimizer()
dl = DynamicLocation()
pp = PromotionPlan()
be = BillingEngine()
for name, price in [("inbound", 0.5), ("pick", 0.8), ("outbound", 0.6), ("storage", 0.1)]:
    be.set_rule(name, price)
lo = LMSOptimizer()
mw = MultiWarehouse()
mw.set_stock("WH1", "SKU-X", 5)
mw.set_stock("WH2", "SKU-X", 3)
rc = Reconciliation()
sa = SecurityAudit()

CONSOLE_HTML = r"D:\trinity-code\docs\wms_live_console.html"


def dashboard():
    stock = [{"sku": s, "available": q} for s, q in ao._stock.items()]
    return {"system": "SmartCos WMS 完美重构参考实现",
            "version": "EXECUTION 412",
            "components": 10, "stock": stock,
            "replenish": ro.suggest(),
            "audit_events": len(sa.audit_log),
            "status": "ok"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        try:
            if p == "/":
                with open(CONSOLE_HTML, "rb") as f:
                    self._send(f.read(), ctype="text/html")
            elif p == "/health":
                self._send({"status": "ok", "system": "wms-perfect", "components": 10})
            elif p == "/api/dashboard":
                self._send(dashboard())
            elif p == "/api/replenish/suggest":
                self._send(ro.suggest())
            else:
                self._send({"error": "not found", "path": p}, code=404)
        except Exception as e:
            self._send({"error": str(e)}, code=500)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            if p == "/api/inventory/reserve":
                self._send(ao.reserve(d.get("sku", ""), int(d.get("qty", 1))))
            elif p == "/api/wave/plan":
                self._send(wp.plan(d.get("orders", []), d.get("strategy", "carrier")))
            elif p == "/api/wave/path":
                self._send(wp.s_path(d.get("aisles", []), d.get("picks", {})))
            elif p == "/api/location/classify":
                self._send(dl.classify(d.get("items", [])))
            elif p == "/api/promotion/capacity":
                self._send(pp.capacity_check(int(d.get("peak_orders", 10000))))
            elif p == "/api/billing/calc":
                self._send(be.calculate(d.get("ops", [])))
            elif p == "/api/lms/predict":
                self._send(lo.predict_workload(int(d.get("orders", 5000)), int(d.get("per", 300))))
            elif p == "/api/multiwh/fulfill":
                self._send(mw.fulfill_nearest(d.get("sku", ""), d.get("warehouses", []),
                                              d.get("distance", {})))
            elif p == "/api/reconcile":
                self._send(rc.reconcile(d.get("wms_flows", []), d.get("erp_ledger", {})))
            elif p == "/api/security/check":
                ok = sa.check_permission(d.get("role", ""), d.get("action", ""))
                sa.log(d.get("role", ""), d.get("action", ""), "api")
                self._send({"allowed": ok})
            else:
                self._send({"error": "not found", "path": p}, code=404)
        except Exception as e:
            self._send({"error": str(e)}, code=500)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8020
    print(f"SmartCos WMS 完美重构参考实现 → http://127.0.0.1:{port}（10 组件 API + 控制台）")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
