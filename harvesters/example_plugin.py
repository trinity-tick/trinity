# -*- coding: utf-8 -*-
"""C2 Harvester 示例插件 — 演示规范插件的采集与写入。

用法:
    python harvesters/example_plugin.py --dry-run
    python harvesters/example_plugin.py --config '{"url":"https://example.com/article"}'
"""
import argparse
import json
import sys
import time
import requests

PLUGIN = {
    "id": "example-harvester",
    "name": "示例采集器",
    "version": "1.0.0",
    "source": "example",
    "capabilities": ["text"],
}

API = "http://127.0.0.1:8001"
H = {"X-Agent-ID": PLUGIN["id"], "X-Agent-Role": "admin"}


def harvest(config: dict) -> list:
    """模拟从 config['url'] 采集内容并结构化。"""
    url = config.get("url", "https://example.com")
    content = (
        f"从 {url} 采集的示例内容（{time.strftime('%Y-%m-%d %H:%M')}）："
        "Trinity harvester 插件规范 v0 演示。"
    )
    return [{
        "content": content,
        "category": "web_harvested",
        "tags": [PLUGIN["id"], url],
        "importance": 0.6,
        "metadata": {"url": url, "plugin": PLUGIN["id"], "harvested_at": time.time()},
    }]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default="{}")
    args = ap.parse_args()
    config = json.loads(args.config)

    items = harvest(config)
    print(f"插件: {PLUGIN['id']} v{PLUGIN['version']} | 采集 {len(items)} 条")
    if args.dry_run:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return

    for item in items:
        r = requests.post(f"{API}/memories", json=item, headers=H, timeout=30)
        print(f"write -> {r.status_code} {r.json().get('memory_id', '')}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
