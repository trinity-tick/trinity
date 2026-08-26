#!/usr/bin/env python3
"""pull_bge_m3_onnx.py — 下载 bge-m3 ONNX 量化模型（内镶用，2026-08-25）

从 hf-mirror.com 下载 hooman650/bge-m3-onnx-o4（量化版 ~1.08GB）到
~/.trinity/models/bge-m3-onnx/，供 OnnxEmbeddingEngine 进程内推理。

支持断点续传（Range）+ 大小校验 + 重试。

用法：
    python scripts/pull_bge_m3_onnx.py              # 下载全部文件
    python scripts/pull_bge_m3_onnx.py --check      # 只校验已下载
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request

BASE = "https://hf-mirror.com/hooman650/bge-m3-onnx-o4/resolve/main"
OUT_DIR = os.path.expanduser("~/.trinity/models/bge-m3-onnx")

FILES = [
    ("model_optimized.onnx", 0),       # 图文件（小）
    ("model_optimized.onnx.data", 1081 * 1024 * 1024),  # 权重（~1081MB）
    ("sentencepiece.bpe.model", 5 * 1024 * 1024),       # tokenizer
    ("tokenizer_config.json", 2 * 1024 * 1024),
    ("special_tokens_map.json", 1 * 1024 * 1024),
    ("config.json", 1 * 1024 * 1024),
]


class _RangeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向时保留 Range 头（hf-mirror 302 到 CDN——默认丢弃 Range 导致重复下载）。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and "Range" in req.headers:
            new_req.add_unredirected_header("Range", req.headers["Range"])
        return new_req


def _download(url: str, path: str, expected: int) -> bool:
    """带断点续传的下载（重定向保留 Range）。"""
    tmp = path + ".part"
    existing = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    if existing >= expected > 0:
        os.replace(tmp, path)
        print(f"  {os.path.basename(path)}: 已完成（{existing//1024//1024}MB）")
        return True
    headers = {"User-Agent": "trinity-bge-m3-puller/1.0"}
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
    opener = urllib.request.build_opener(_RangeRedirectHandler())
    retries = 5
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=180) as resp:
                status = getattr(resp, "status", 200)
                mode = "ab" if existing > 0 and status == 206 else "wb"
                with open(tmp, mode) as f:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                        f.flush()
            final = os.path.getsize(tmp)
            if expected and final < expected:
                print(f"  {os.path.basename(path)}: {final//1024//1024}MB "
                      f"(expected {expected//1024//1024}MB, 继续...)")
                existing = final
                continue
            os.replace(tmp, path)
            print(f"  {os.path.basename(path)}: {final//1024//1024}MB OK")
            return True
        except Exception as exc:
            print(f"  {os.path.basename(path)} attempt {attempt+1} err: {str(exc)[:80]}")
            if attempt < retries - 1:
                time.sleep(2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验不下载")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    ok = True
    for fname, size in FILES:
        path = os.path.join(OUT_DIR, fname)
        if os.path.exists(path):
            sz = os.path.getsize(path)
            if size == 0 or sz >= size * 0.9:  # 允许 10% 误差
                print(f"  {fname}: {sz//1024//1024}MB 已存在 OK")
                continue
            else:
                print(f"  {fname}: 不完整 {sz//1024//1024}MB, 续传")
        if args.check:
            ok = False
            continue
        print(f"  下载 {fname} ...")
        if not _download(f"{BASE}/{fname}", path, size):
            ok = False
    total = sum(os.path.getsize(os.path.join(OUT_DIR, f))
                for f, _ in FILES if os.path.exists(os.path.join(OUT_DIR, f)))
    print(f"=== 完成: {total//1024//1024}MB @ {OUT_DIR} ===" if ok
          else "=== 有失败项，请重试 ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
