# -*- coding: utf-8 -*-
"""C1 记忆市场 demo — 上架/搜索/下单/信誉 全流程。

用法:
    python market/demo.py list --owner agent-wms --content "..." --price 10
    python market/demo.py search --q "库位"
    python market/demo.py buy --buyer agent-buyer --asset <id> --offer 10
    python market/demo.py reputation --agent agent-wms
    python market/demo.py report
"""
import argparse
import json
import sys
import requests

API = "http://127.0.0.1:8001"
H = {"X-Agent-ID": "market-demo", "X-Agent-Role": "admin"}


def post(path, **payload):
    r = requests.post(f"{API}{path}", json=payload, headers=H, timeout=30)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:200]


def get(path, **params):
    r = requests.get(f"{API}{path}", params=params, headers=H, timeout=30)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:200]


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("list"); p1.add_argument("--owner", required=True); p1.add_argument("--content", required=True); p1.add_argument("--price", type=float, default=5.0)
    p2 = sub.add_parser("search"); p2.add_argument("--q", required=True)
    p3 = sub.add_parser("buy"); p3.add_argument("--buyer", required=True); p3.add_argument("--asset", required=True); p3.add_argument("--offer", type=float, default=0.0)
    p4 = sub.add_parser("reputation"); p4.add_argument("--agent", required=True)
    p5 = sub.add_parser("report")
    args = ap.parse_args()

    if args.cmd == "list":
        s, d = post("/market/list", memory={"content": args.content, "tags": ["market-demo"], "category": "knowledge"},
                    owner=args.owner, price=args.price, license="CC-BY", currency="trust_score")
        print(s, json.dumps(d, ensure_ascii=False)[:400])
    elif args.cmd == "search":
        r = requests.get(f"{API}/market/search", params={"query": args.q}, headers=H, timeout=30)
        try:
            d = r.json()
        except Exception:
            d = r.text[:200]
        print(r.status_code, json.dumps(d, ensure_ascii=False)[:600])
    elif args.cmd == "buy":
        s, d = post("/market/buy", buyer_agent=args.buyer, asset_id=args.asset, offer_price=args.offer, currency="trust_score")
        print(s, json.dumps(d, ensure_ascii=False)[:400])
    elif args.cmd == "reputation":
        s, d = get(f"/market/reputation/{args.agent}")
        print(s, json.dumps(d, ensure_ascii=False)[:400])
    elif args.cmd == "report":
        s, d = post("/market/report", from_agent="market-demo", to_agent="market-demo", reason="demo")
        print(s, json.dumps(d, ensure_ascii=False)[:600])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
