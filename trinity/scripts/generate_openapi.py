#!/usr/bin/env python3
"""
Trinity OpenAPI Spec Generator (v8.0.0)
========================================
启动 Trinity API Server → 请求 /openapi.json → 写入 output 目录。

用法:
    python trinity/scripts/generate_openapi.py [--port PORT] [--output-dir OUTPUT_DIR]
"""

import argparse
import json
import os
import sys
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Trinity OpenAPI Spec Generator")
    parser.add_argument("--port", type=int, default=8001, help="Server port (default: 8001)")
    parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--output-dir", default=None, help="Output directory for generated files")
    parser.add_argument("--timeout", type=int, default=30, help="Max wait time for server startup (seconds)")
    args = parser.parse_args()

    # Resolve project root and output directory
    project_root = Path(__file__).resolve().parent.parent
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    else:
        output_dir = project_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    base_url = f"http://{args.host}:{args.port}"

    # Start server in background thread
    print(f"[*] Starting Trinity API Server on {base_url}...")
    import uvicorn
    server_thread = threading.Thread(
        target=lambda: uvicorn.run(
            "trinity.api.server:app",
            host=args.host,
            port=args.port,
            log_level="warning",
        ),
        daemon=True,
    )
    server_thread.start()

    # Wait for server to be ready
    openapi_url = f"{base_url}/openapi.json"
    deadline = time.time() + args.timeout
    openapi_data = None

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(openapi_url, timeout=5) as resp:
                if resp.status == 200:
                    openapi_data = json.loads(resp.read().decode("utf-8"))
                    print(f"[✓] Server is ready, OpenAPI spec fetched ({openapi_url})")
                    break
        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            time.sleep(0.5)

    if openapi_data is None:
        print(f"[✗] Failed to fetch OpenAPI spec within {args.timeout}s", file=sys.stderr)
        sys.exit(1)

    # Write openapi.json
    json_path = output_dir / "trinity_openapi.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(openapi_data, f, indent=2, ensure_ascii=False)
    print(f"[✓] OpenAPI JSON written to: {json_path}")

    # Generate HTML docs via ReDoc-style standalone HTML
    html_path = output_dir / "trinity_api_docs.html"
    generate_html_docs(openapi_data, html_path)
    print(f"[✓] API docs HTML written to: {html_path}")

    print(f"\n[DONE] Files generated:")
    print(f"  JSON: {json_path}")
    print(f"  HTML: {html_path}")


def generate_html_docs(openapi_spec: dict, output_path: Path):
    """Generate a standalone HTML API documentation page using ReDoc."""
    import json as _json
    spec_json = _json.dumps(openapi_spec, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Trinity Memory OS v8.0.0 — API Documentation</title>
    <style>
        body {{ margin: 0; padding: 0; }}
        redoc {{ display: block; }}
    </style>
    <script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
</head>
<body>
    <div id="redoc-container"></div>
    <script>
        Redoc.init(
            {spec_json},
            {{
                nativeScrollbars: true,
                sortPropsAlphabetically: true,
                sortOperationsAlphabetically: true,
                disableSearch: false,
                expandResponses: "200",
                hideDownloadButton: false,
                hideLoading: false,
                showExtensions: true,
                theme: {{
                    colors: {{
                        primary: {{ main: '#2563eb' }},
                    }},
                    typography: {{
                        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
                        headings: {{ fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif' }},
                    }},
                }},
            }},
            document.getElementById('redoc-container')
        );
    </script>
</body>
</html>"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
