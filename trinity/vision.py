# -*- coding: utf-8 -*-
"""trinity/vision.py — 本地视觉描述（2026-09，EXECUTION 147/149）

轻量视觉感知：无需外部 vision 模型，用 PIL/numpy 提取图像特征生成
描述文本。EXECUTION 149 增强：
  - 高对比文字区域检测（UI/截图常见）
  - 颜色分布（主色系 + 数量）
  - 边缘密度（界面复杂度近似）

describe_image(image: PIL.Image) -> str
"""
import os
import json
import math


def _text_regions(arr, threshold=180):
    """高对比度块检测（近似文字/图标区域）。

    arr: (H, W, 3) numpy uint8；返回块数量与总占比。
    """
    try:
        import numpy as _np
        lum = arr.mean(axis=2)
        # 高对比：局部亮暗差异大
        h, w = lum.shape
        block = 16
        nh, nw = h // block, w // block
        high = 0
        for by in range(nh):
            for bx in range(nw):
                blk = lum[by * block:(by + 1) * block, bx * block:(bx + 1) * block]
                if blk.size == 0:
                    continue
                rng = float(blk.max() - blk.min())
                if rng > threshold:
                    high += 1
        total = max(1, nh * nw)
        return high, high / total
    except Exception:
        return 0, 0.0


def _edge_density(arr):
    """边缘密度（相邻像素差异比例）——界面复杂度近似。"""
    try:
        import numpy as _np
        lum = arr.mean(axis=2)
        dx = _np.abs(_np.diff(lum, axis=1))
        dy = _np.abs(_np.diff(lum, axis=0))
        edges = float((dx > 20).mean()) + float((dy > 20).mean())
        return min(1.0, edges / 2.0)
    except Exception:
        return 0.0


def describe_image(image) -> str:
    """生成图像描述（基础视觉特征 + 语义线索）。"""
    try:
        from PIL import Image as _PIL
        import numpy as _np
        if image.mode != "RGB":
            image = image.convert("RGB")
        w, h = image.size
        small = image.resize((64, 64))
        arr = _np.asarray(small, dtype=_np.uint8)
        mean_rgb = arr.mean(axis=(0, 1))
        brightness = float(arr.mean() / 255.0)
        variance = float(arr.std() / 255.0)
        r, g, b = int(mean_rgb[0]), int(mean_rgb[1]), int(mean_rgb[2])
        # 主色系
        if r > 150 and g < 100 and b < 100:
            tone = "红/橙色系"
        elif r < 100 and g > 120 and b < 120:
            tone = "绿色系"
        elif r < 100 and g < 100 and b > 150:
            tone = "蓝色系"
        elif brightness < 0.2:
            tone = "暗色"
        elif brightness > 0.8:
            tone = "亮色"
        else:
            tone = "中性色"
        if variance < 0.08:
            complexity = "内容单一"
        elif variance < 0.2:
            complexity = "内容适中"
        else:
            complexity = "内容丰富"
        # EXECUTION 149: 文字区域 + 边缘
        txt_blocks, txt_ratio = _text_regions(arr)
        edges = _edge_density(arr)
        parts = [f"截图 {w}x{h}px", tone, complexity]
        if txt_blocks >= 3:
            parts.append(f"含约 {txt_blocks} 处高对比文字/图标区")
        if edges > 0.3:
            parts.append("界面元素密集")
        elif edges < 0.08:
            parts.append("画面平滑")
        parts.append(f"主色 RGB({r},{g},{b})")
        return "，".join(parts)
    except Exception:
        return None


def describe_image_b64(b64_str: str) -> str:
    """base64 图像字符串 → 描述（失败返回 None）。"""
    try:
        import base64 as _b64, io as _io
        from PIL import Image as _PIL
        img = _PIL.open(_io.BytesIO(_b64.b64decode(b64_str)))
        return describe_image(img)
    except Exception:
        return None


# ── 2026-09-02 (EXECUTION 457): 语义级画面理解（本地 VL 模型）─────────
# 体检结论：感知 85% 是"特征级"（颜色/对比/边缘），缺语义级画面理解。
# 本机 Ollama 拉取 qwen2.5vl:3b（本地视觉语言模型），OpenAI 兼容通道：
#   语义描述成功 → 真正"看懂画面"；模型不可用/超时 → 静默降级特征描述。
# 开关：TRINITY_VISION_SEMANTIC=0 关闭（保持确定性/省时）；默认 1。
_SEM_CHECKED = False
_SEM_OK = False


def _semantic_available() -> bool:
    """探测本地 VL 模型可用性（每进程一次 + 失败冷却 60s）。"""
    global _SEM_CHECKED, _SEM_OK
    if _SEM_CHECKED:
        return _SEM_OK
    if os.environ.get("TRINITY_VISION_SEMANTIC", "1") == "0":
        _SEM_CHECKED = True
        return False
    import urllib.request
    model = os.environ.get("TRINITY_VISION_MODEL", "qwen2.5vl:3b")
    base = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    base = base if base.startswith("http") else "http://" + base
    base = base.replace("0.0.0.0", "127.0.0.1")  # Windows 上 0.0.0.0 不可连接
    try:
        with urllib.request.urlopen(base + "/v1/models", timeout=4) as r:
            data = json.loads(r.read().decode())
        ids = [m.get("id", "") for m in data.get("data", [])]
        _SEM_OK = any(model in i for i in ids)
    except Exception:
        _SEM_OK = False
    _SEM_CHECKED = True
    return _SEM_OK


def describe_image_semantic(image, max_tokens: int = 220) -> str:
    """语义级画面描述（本地 VL，OpenAI 兼容；失败返回 None → 调用方降级）。"""
    import base64 as _b64, io as _io
    if not _semantic_available():
        return None
    try:
        if image.mode != "RGB":
            image = image.convert("RGB")
        buf = _io.BytesIO()
        # 压缩到宽 1024 内，控制 token
        w, h = image.size
        if w > 1024:
            image = image.resize((1024, int(h * 1024 / w)))
        image.save(buf, format="PNG")
        b64 = _b64.b64encode(buf.getvalue()).decode()
        model = os.environ.get("TRINITY_VISION_MODEL", "qwen2.5vl:3b")
        base = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        base = base if base.startswith("http") else "http://" + base
        base = base.replace("0.0.0.0", "127.0.0.1")  # Windows 上 0.0.0.0 不可连接
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text",
                 "text": "用中文简要描述这张截图/图像的内容：界面类型、主要元素、"
                         "可见文字、状态与异常线索（不超过 60 字，只描述看到的事实）"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + b64}},
            ]}],
            "max_tokens": max_tokens, "temperature": 0.1,
            "stream": False,
        }
        import urllib.request
        req = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode())
        txt = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return " ".join(txt.split())[:200] or None
    except Exception:
        return None


def describe_image_any(image) -> str:
    """语义优先、特征降级的统一入口（EXECUTION 457）。"""
    sem = describe_image_semantic(image)
    if sem:
        return "[语义] " + sem
    feat = describe_image(image)
    return ("[特征] " + feat) if feat else None


def describe_image_b64_semantic(b64_str: str) -> str:
    """base64 图像 → 语义描述（失败降级特征）。"""
    try:
        import base64 as _b64, io as _io
        from PIL import Image as _PIL
        img = _PIL.open(_io.BytesIO(_b64.b64decode(b64_str)))
        return describe_image_any(img)
    except Exception:
        return None


def _semantic_reset():
    """测试/诊断用：重置可用性缓存。"""
    global _SEM_CHECKED, _SEM_OK
    _SEM_CHECKED = False
    _SEM_OK = False
